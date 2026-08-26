"""Validation helpers for structured, chat-first task deliverables.

Kanban artifacts remain useful audit evidence, but a human should not need to
open a Markdown file to learn the result of a status or inventory request.
Workers can attach ``metadata.user_facing_report`` to ``kanban_complete``;
the gateway then gives Grace a bounded, structured table to render inline.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any


COMMERCE_GROUP_REPORT_KIND = "commerce_group_status"
CONTENT_PACKAGE_REPORT_KIND = "content_package"
INLINE_ONLY_DELIVERY = "inline_only"
SECONDHAND_COMMERCE_RECONCILIATION_SUBJECT_KEYS = frozenset({
    "carimali-armonia-soft-plus",
    "kolin-kd291m06",
    "celestron-130eq",
})

COMMERCE_GROUP_STATUSES = frozenset({
    "public",
    "pending_approval",
    "rejected",
    "not_found",
    "ambiguous_after_submit",
    "not_posted",
    "unknown",
})
UNRESOLVED_COMMERCE_GROUP_STATUSES = frozenset({
    "ambiguous_after_submit",
    "unknown",
})
MAX_REPORT_JSON_CHARS = 12_000
MAX_CONTENT_PACKAGE_JSON_CHARS = 80_000
MAX_FUTURE_SKEW_SECONDS = 300


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"metadata.user_facing_report {field} must be non-empty")
    return text


def _unix_seconds(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > int(time.time()) + MAX_FUTURE_SKEW_SECONDS
    ):
        raise ValueError(
            f"metadata.user_facing_report {field} must be a plausible "
            "Unix-seconds timestamp"
        )
    return value


def _normalize_commerce_group_report(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated, JSON-safe user-facing report.

    The first supported report kind is deliberately narrow: a durable
    product-to-Facebook-group status ledger.  It supplies the exact fields
    Grace needs for an inline Telegram table and the coverage facts needed to
    prevent a partial reconstruction from being closed as a complete answer.
    """
    kind = _required_text(raw.get("kind"), "kind")
    if kind != COMMERCE_GROUP_REPORT_KIND:
        raise ValueError(
            "metadata.user_facing_report kind must be commerce_group_status"
        )
    delivery = str(raw.get("delivery") or INLINE_ONLY_DELIVERY).strip()
    if delivery not in {INLINE_ONLY_DELIVERY, "inline_with_attachment"}:
        raise ValueError(
            "metadata.user_facing_report delivery must be inline_only or "
            "inline_with_attachment"
        )
    if not isinstance(raw.get("complete"), bool):
        raise ValueError("metadata.user_facing_report complete must be a boolean")
    as_of = _required_text(raw.get("as_of"), "as_of")
    report_observed_at = _unix_seconds(raw.get("observed_at"), "observed_at")

    raw_rows = raw.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("metadata.user_facing_report rows must be a list")
    if len(raw_rows) > 250:
        raise ValueError("metadata.user_facing_report rows exceeds 250 entries")

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, Mapping):
            raise ValueError(
                f"metadata.user_facing_report rows[{index}] must be an object"
            )
        subject_key = _required_text(
            raw_row.get("subject_key"), f"rows[{index}].subject_key"
        )
        destination_id = _required_text(
            raw_row.get("destination_id"), f"rows[{index}].destination_id"
        )
        if re.fullmatch(r"[1-9][0-9]*", destination_id) is None:
            raise ValueError(
                f"metadata.user_facing_report rows[{index}].destination_id "
                "must be canonical ASCII digits without leading zeros"
            )
        identity = (subject_key, destination_id)
        if identity in seen:
            raise ValueError(
                "metadata.user_facing_report contains a duplicate "
                f"subject/destination row: {subject_key}/{destination_id}"
            )
        seen.add(identity)
        status = _required_text(raw_row.get("status"), f"rows[{index}].status").lower()
        if status not in COMMERCE_GROUP_STATUSES:
            raise ValueError(
                f"metadata.user_facing_report rows[{index}].status is unsupported"
            )
        row_observed_at = _unix_seconds(
            raw_row.get("observed_at"), f"rows[{index}].observed_at"
        )
        row = {
            "subject_key": subject_key,
            "subject_label": _required_text(
                raw_row.get("subject_label"), f"rows[{index}].subject_label"
            ),
            "destination_id": destination_id,
            "destination_name": _required_text(
                raw_row.get("destination_name"),
                f"rows[{index}].destination_name",
            ),
            "status": status,
            "status_label": _required_text(
                raw_row.get("status_label"), f"rows[{index}].status_label"
            ),
            "observed_at": row_observed_at,
            "verified_at": _required_text(
                raw_row.get("verified_at"), f"rows[{index}].verified_at"
            ),
            "evidence": _required_text(
                raw_row.get("evidence"), f"rows[{index}].evidence"
            ),
            "source_listing_id": str(raw_row.get("source_listing_id") or "").strip(),
            "source_task_id": str(raw_row.get("source_task_id") or "").strip(),
        }
        rows.append(row)

    raw_coverage = raw.get("coverage")
    if not isinstance(raw_coverage, list) or not raw_coverage:
        raise ValueError(
            "metadata.user_facing_report coverage must be a non-empty list"
        )
    coverage: list[dict[str, Any]] = []
    coverage_keys: set[str] = set()
    for index, raw_item in enumerate(raw_coverage):
        if not isinstance(raw_item, Mapping):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}] must be an object"
            )
        subject_key = _required_text(
            raw_item.get("subject_key"), f"coverage[{index}].subject_key"
        )
        if subject_key in coverage_keys:
            raise ValueError(
                "metadata.user_facing_report contains duplicate coverage for "
                f"{subject_key}"
            )
        coverage_keys.add(subject_key)
        complete = raw_item.get("complete")
        if not isinstance(complete, bool):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}].complete "
                "must be a boolean"
            )
        named_count = raw_item.get("named_count")
        gap_count = raw_item.get("gap_count")
        expected_total = raw_item.get("expected_total")
        if (
            isinstance(named_count, bool)
            or not isinstance(named_count, int)
            or named_count < 0
        ):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}].named_count "
                "must be a non-negative integer"
            )
        if gap_count is not None and (
            isinstance(gap_count, bool)
            or not isinstance(gap_count, int)
            or gap_count < 0
        ):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}].gap_count "
                "must be null or a non-negative integer"
            )
        if expected_total is not None and (
            isinstance(expected_total, bool)
            or not isinstance(expected_total, int)
            or expected_total < 0
        ):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}].expected_total "
                "must be null or a non-negative integer"
            )
        if (expected_total is None) != (gap_count is None):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}] must provide "
                "both expected_total and gap_count, or neither"
            )
        if (
            expected_total is not None
            and named_count + gap_count != expected_total
        ):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}] named_count "
                "+ gap_count must equal expected_total"
            )
        coverage.append({
            "subject_key": subject_key,
            "subject_label": _required_text(
                raw_item.get("subject_label"),
                f"coverage[{index}].subject_label",
            ),
            "complete": complete,
            "named_count": named_count,
            "gap_count": gap_count,
            "expected_total": expected_total,
            "expected_total_label": str(
                raw_item.get("expected_total_label") or ""
            ).strip(),
            "note": _required_text(raw_item.get("note"), f"coverage[{index}].note"),
        })

    row_subjects = {row["subject_key"] for row in rows}
    if not row_subjects <= coverage_keys:
        raise ValueError(
            "metadata.user_facing_report every row subject must have coverage"
        )
    row_counts: dict[str, int] = {}
    for row in rows:
        row_counts[row["subject_key"]] = row_counts.get(row["subject_key"], 0) + 1
    for item in coverage:
        if item["named_count"] != row_counts.get(item["subject_key"], 0):
            raise ValueError(
                "metadata.user_facing_report coverage named_count must match "
                f"the number of rows for {item['subject_key']}"
            )
    unresolved_subjects = {
        row["subject_key"]
        for row in rows
        if row["status"] in UNRESOLVED_COMMERCE_GROUP_STATUSES
    }
    calculated_complete = all(
        item["complete"]
        and item["expected_total"] is not None
        and item["gap_count"] == 0
        and item["named_count"] == item["expected_total"]
        and item["subject_key"] not in unresolved_subjects
        for item in coverage
    )
    if bool(raw["complete"]) != calculated_complete:
        raise ValueError(
            "metadata.user_facing_report complete must match coverage completeness"
        )

    normalized = {
        "kind": kind,
        "delivery": delivery,
        "complete": calculated_complete,
        "as_of": as_of,
        "observed_at": report_observed_at,
        "rows": rows,
        "coverage": coverage,
    }
    if len(json.dumps(normalized, ensure_ascii=False, sort_keys=True)) > (
        MAX_REPORT_JSON_CHARS
    ):
        raise ValueError(
            "metadata.user_facing_report exceeds the inline delivery size limit; "
            "shorten evidence and notes without dropping rows"
        )
    return normalized


def _normalize_content_package_report(raw: Mapping[str, Any]) -> dict[str, Any]:
    delivery = str(raw.get("delivery") or "inline_with_attachment").strip()
    if delivery != "inline_with_attachment":
        raise ValueError(
            "metadata.user_facing_report content_package delivery must be "
            "inline_with_attachment"
        )
    if raw.get("complete") is not True:
        raise ValueError(
            "metadata.user_facing_report content_package complete must be true"
        )
    title = _required_text(raw.get("title"), "title")
    body = _required_text(raw.get("body"), "body")
    observed_at = _unix_seconds(raw.get("observed_at"), "observed_at")
    raw_assets = raw.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ValueError(
            "metadata.user_facing_report content_package assets must be a "
            "non-empty list"
        )
    assets: list[dict[str, str]] = []
    seen_filenames: set[str] = set()
    for index, raw_asset in enumerate(raw_assets):
        if not isinstance(raw_asset, Mapping):
            raise ValueError(
                f"metadata.user_facing_report assets[{index}] must be an object"
            )
        filename = _required_text(
            raw_asset.get("filename"), f"assets[{index}].filename"
        )
        if filename in seen_filenames:
            raise ValueError(
                "metadata.user_facing_report contains duplicate asset filename: "
                + filename
            )
        seen_filenames.add(filename)
        path = _required_text(raw_asset.get("path"), f"assets[{index}].path")
        digest = _required_text(
            raw_asset.get("sha256"), f"assets[{index}].sha256"
        ).lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(
                f"metadata.user_facing_report assets[{index}].sha256 must be "
                "64 lowercase hexadecimal characters"
            )
        assets.append({
            "filename": filename,
            "label": _required_text(
                raw_asset.get("label"), f"assets[{index}].label"
            ),
            "path": path,
            "sha256": digest,
        })
    normalized = {
        "kind": CONTENT_PACKAGE_REPORT_KIND,
        "delivery": delivery,
        "complete": True,
        "title": title,
        "body": body,
        "observed_at": observed_at,
        "assets": assets,
    }
    if len(json.dumps(normalized, ensure_ascii=False, sort_keys=True)) > (
        MAX_CONTENT_PACKAGE_JSON_CHARS
    ):
        raise ValueError(
            "metadata.user_facing_report content_package exceeds the inline "
            "delivery size limit"
        )
    return normalized


def normalize_user_facing_report(raw: Any) -> dict[str, Any]:
    """Return a validated, JSON-safe user-facing report."""
    if not isinstance(raw, Mapping):
        raise ValueError("metadata.user_facing_report must be an object")
    kind = _required_text(raw.get("kind"), "kind")
    if kind == COMMERCE_GROUP_REPORT_KIND:
        return _normalize_commerce_group_report(raw)
    if kind == CONTENT_PACKAGE_REPORT_KIND:
        return _normalize_content_package_report(raw)
    raise ValueError(
        "metadata.user_facing_report kind must be commerce_group_status or "
        "content_package"
    )


def report_satisfies_user_facing_delivery(
    report: Any,
    delivery_contract: Any,
) -> bool:
    """Return whether a report is complete for one exact delivery contract."""
    if not isinstance(delivery_contract, Mapping):
        return False
    try:
        normalized = normalize_user_facing_report(report)
    except ValueError:
        return False
    if normalized["kind"] == CONTENT_PACKAGE_REPORT_KIND:
        requested_assets = delivery_contract.get("asset_filenames")
        return (
            delivery_contract.get("required") is True
            and delivery_contract.get("kind") == CONTENT_PACKAGE_REPORT_KIND
            and delivery_contract.get("delivery") == normalized["delivery"]
            and isinstance(requested_assets, list)
            and bool(requested_assets)
            and set(requested_assets)
            == {asset["filename"] for asset in normalized["assets"]}
            and bool(normalized["complete"])
        )
    requested_subjects = delivery_contract.get("subject_keys")
    if (
        delivery_contract.get("required") is not True
        or delivery_contract.get("kind") != normalized["kind"]
        or delivery_contract.get("delivery") != normalized["delivery"]
        or not isinstance(requested_subjects, list)
        or not requested_subjects
        or any(
            not isinstance(subject, str) or not subject.strip()
            for subject in requested_subjects
        )
    ):
        return False
    report_subjects = {
        item["subject_key"] for item in normalized["coverage"]
    }
    return (
        report_subjects == {subject.strip() for subject in requested_subjects}
        and bool(normalized["complete"])
    )


def report_matches_user_facing_delivery(
    report: Any,
    delivery_contract: Any,
) -> bool:
    """Return whether report identity matches a contract, complete or not."""
    if not isinstance(delivery_contract, Mapping):
        return False
    try:
        normalized = normalize_user_facing_report(report)
    except ValueError:
        return False
    if normalized["kind"] == CONTENT_PACKAGE_REPORT_KIND:
        requested_assets = delivery_contract.get("asset_filenames")
        return (
            delivery_contract.get("required") is True
            and delivery_contract.get("kind") == CONTENT_PACKAGE_REPORT_KIND
            and delivery_contract.get("delivery") == normalized["delivery"]
            and isinstance(requested_assets, list)
            and bool(requested_assets)
            and set(requested_assets)
            == {asset["filename"] for asset in normalized["assets"]}
        )
    requested_subjects = delivery_contract.get("subject_keys")
    if (
        delivery_contract.get("required") is not True
        or delivery_contract.get("kind") != normalized["kind"]
        or delivery_contract.get("delivery") != normalized["delivery"]
        or not isinstance(requested_subjects, list)
        or not requested_subjects
        or any(
            not isinstance(subject, str) or not subject.strip()
            for subject in requested_subjects
        )
    ):
        return False
    return {
        item["subject_key"] for item in normalized["coverage"]
    } == {subject.strip() for subject in requested_subjects}


def report_allows_closed_outcome(report: Any) -> bool:
    """Return whether a validated report satisfies the complete user outcome."""
    try:
        normalized = normalize_user_facing_report(report)
    except ValueError:
        return False
    return bool(normalized["complete"])


def report_is_inline_only(report: Any) -> bool:
    """Return whether artifacts are audit-only for this user-facing report."""
    try:
        normalized = normalize_user_facing_report(report)
    except ValueError:
        return False
    return normalized["delivery"] == INLINE_ONLY_DELIVERY


def user_facing_report_digest(report: Any) -> str:
    """Return the canonical digest bound to a successful chat delivery."""
    normalized = normalize_user_facing_report(report)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_user_facing_report_chunks(
    report: Any,
    *,
    max_chars: int = 3500,
) -> list[str]:
    """Render every report row and coverage gap into bounded chat chunks."""
    normalized = normalize_user_facing_report(report)
    if normalized["kind"] == CONTENT_PACKAGE_REPORT_KIND:
        text = f"{normalized['title']}\n\n{normalized['body']}"
        return [
            text[offset:offset + max_chars]
            for offset in range(0, len(text), max_chars)
        ]
    rows_by_subject: dict[str, list[dict[str, Any]]] = {}
    for row in normalized["rows"]:
        rows_by_subject.setdefault(row["subject_key"], []).append(row)
    sections = [f"社團刊登狀態（截至 {normalized['as_of']}）"]
    for coverage in normalized["coverage"]:
        subject_rows = rows_by_subject.get(coverage["subject_key"], [])
        lines = [f"\n{coverage['subject_label']}"]
        if subject_rows:
            for row in subject_rows:
                lines.append(
                    f"- {row['destination_name']}：{row['status_label']}"
                    f"（{row['verified_at']}）\n  {row['evidence']}"
                )
        else:
            lines.append("- 目前沒有可具名的社團紀錄")
        coverage_state = "清單完整" if coverage["complete"] else "清單仍有缺口"
        total = (
            f"；已知總數 {coverage['expected_total']}、具名 "
            f"{coverage['named_count']}、缺口 {coverage['gap_count']}"
            if coverage["expected_total"] is not None
            else f"；具名 {coverage['named_count']}、總數未知"
        )
        lines.append(
            f"{coverage_state}{total}。{coverage['note']}"
        )
        sections.append("\n".join(lines))
    text = "\n".join(sections)
    return [
        text[offset:offset + max_chars]
        for offset in range(0, len(text), max_chars)
    ]
