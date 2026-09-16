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
from urllib.parse import urlsplit


COMMERCE_GROUP_REPORT_KIND = "commerce_group_status"
CONTENT_PACKAGE_REPORT_KIND = "content_package"
FULL_PUBLICATION_PACKAGE_KIND = "full_publication_package"
INLINE_ONLY_DELIVERY = "inline_only"
FULL_PUBLICATION_SECTION_FIELDS = (
    "facebook_page_post",
    "facebook_group_post",
    "gemini_notebook_prompt",
    "podcast_title",
    "podcast_description",
)
_FULL_PUBLICATION_SECTION_HEADINGS = {
    "facebook_page_post": (
        "facebook page 貼文", "facebook page 內文", "facebook page 正文",
        "facebook page post",
    ),
    "facebook_group_post": ("facebook group 討論附文", "facebook group 附文", "facebook group post"),
    "gemini_notebook_prompt": (
        "gemini notebook audio generation prompt",
        "gemini notebook audio prompt",
        "gemini notebook prompt",
    ),
    "podcast_title": (
        "podcast title", "podcast 標題", "podcast／spotify title",
    ),
    "podcast_description": (
        "podcast description", "podcast 說明", "podcast／spotify description",
    ),
}
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
COMMERCE_COVERAGE_COUNTERS = (
    "destination_target", "verified_published_count", "unknown_count",
    "remaining_verified_publication_gap", "unnamed_gap",
    "candidate_count", "joined_count", "selectable_count", "submitted_count",
    "pending_count", "rejected_count", "not_submitted_count",
    "ready_count", "conditional_count", "excluded_count",
)
COMMERCE_DERIVED_COUNTERS = frozenset({
    "destination_target", "verified_published_count", "unknown_count",
    "remaining_verified_publication_gap", "unnamed_gap",
})
MAX_REPORT_JSON_CHARS = 12_000
MAX_CONTENT_PACKAGE_JSON_CHARS = 80_000
MAX_FUTURE_SKEW_SECONDS = 300
MIN_PLAUSIBLE_UNIX_SECONDS = 946_684_800  # 2000-01-01T00:00:00Z


def promote_full_publication_package(
    report: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
    expected_asset_filenames: Any,
    policy_receipts: Any,
    external_effects: Any,
) -> dict[str, Any]:
    """Promote an older generic report only from an exact complete evidence set."""
    if report.get("package_kind") or not (
        report.get("kind") == CONTENT_PACKAGE_REPORT_KIND
        and report.get("delivery") == "inline_with_attachment"
    ):
        return dict(report)
    sections = evidence.get("sections")
    body = report.get("body")
    if not isinstance(sections, Mapping) and isinstance(body, str):
        sections = _full_publication_sections_from_body(body)
    assets = report.get("assets")
    if not (
        isinstance(sections, Mapping)
        and all(
            isinstance(sections.get(name), str) and sections[name].strip()
            for name in FULL_PUBLICATION_SECTION_FIELDS
        )
        and isinstance(body, str)
        and all(sections[name] in body for name in FULL_PUBLICATION_SECTION_FIELDS)
        and isinstance(assets, list)
        and isinstance(expected_asset_filenames, list)
        and len(assets) == len(expected_asset_filenames) == 2
        and isinstance(policy_receipts, list)
        and external_effects == []
    ):
        return dict(report)
    if any(not isinstance(item, Mapping) for item in assets):
        return dict(report)
    report_by_name = {str(item.get("filename") or ""): item for item in assets}
    if (
        list(report_by_name) != expected_asset_filenames
        or len(report_by_name) != 2
    ):
        return dict(report)
    manifests: dict[tuple[tuple[Any, ...], ...], list[dict[str, Any]]] = {}
    for value in evidence.values():
        if not isinstance(value, list) or len(value) != 2 or any(
            not isinstance(item, Mapping) for item in value
        ):
            continue
        by_name = {str(item.get("filename") or ""): item for item in value}
        if len(by_name) != 2 or set(by_name) != set(expected_asset_filenames):
            continue
        promoted_assets = []
        identity = []
        for filename in expected_asset_filenames:
            raw_asset = report_by_name[filename]
            asset_evidence = by_name[filename]
            family = asset_evidence.get("asset_family")
            width = asset_evidence.get("width")
            height = asset_evidence.get("height")
            dimensions = asset_evidence.get("dimensions")
            if isinstance(dimensions, Mapping):
                width = dimensions.get("width", width)
                height = dimensions.get("height", height)
                dimensions = None
            dimension_match = (
                re.fullmatch(
                    r"\s*([1-9][0-9]*)\s*[x×]\s*([1-9][0-9]*)\s*",
                    dimensions,
                    flags=re.IGNORECASE,
                )
                if isinstance(dimensions, str)
                else None
            )
            if dimension_match is not None and width is None and height is None:
                width, height = map(int, dimension_match.groups())
            digest = str(asset_evidence.get("sha256") or "").strip().lower()
            if not (
                family in {"page_hero", "audio_brief"}
                and type(width) is int and width > 0
                and type(height) is int and height > 0
                and re.fullmatch(r"[0-9a-f]{64}", digest)
                and str(raw_asset.get("sha256") or "").strip().lower() == digest
                and all(
                    key not in raw_asset or raw_asset[key] == expected
                    for key, expected in (
                        ("asset_family", family), ("width", width), ("height", height)
                    )
                )
            ):
                break
            identity.append((filename, family, width, height, digest))
            promoted_assets.append({
                **raw_asset,
                "asset_family": family,
                "width": width,
                "height": height,
                "dimensions": f"{width}x{height}",
            })
        if len(promoted_assets) == 2 and {
            asset["asset_family"] for asset in promoted_assets
        } == {"page_hero", "audio_brief"}:
            manifests[tuple(identity)] = promoted_assets
    if len(manifests) != 1:
        return dict(report)
    promoted_assets = next(iter(manifests.values()))
    return {
        **report,
        "package_kind": FULL_PUBLICATION_PACKAGE_KIND,
        "sections": {
            name: sections[name] for name in FULL_PUBLICATION_SECTION_FIELDS
        },
        "assets": promoted_assets,
        "policy_receipts": [dict(receipt) for receipt in policy_receipts],
        "external_effects": [],
    }


def _full_publication_sections_from_body(body: str) -> dict[str, str] | None:
    """Recover the five canonical sections from a complete inline package.

    A zero-change continuation may carry the canonical body and image manifest
    without redundantly copying controller-owned ``sections`` metadata.  The
    headings are the stable user-facing boundary; fail closed on duplicates,
    missing headings, reordering, or empty sections.
    """
    matches: list[tuple[int, int, str]] = []
    offset = 0
    fence: tuple[str, int] | None = None
    for line in body.splitlines(keepends=True):
        if fence is not None:
            marker, minimum = fence
            if re.match(
                rf"^ {{0,3}}{re.escape(marker)}{{{minimum},}}[ \t]*(?:\r?\n)?$",
                line,
            ):
                fence = None
            offset += len(line)
            continue
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n)?$", line)
        if fence_match:
            marker, info = fence_match.groups()
            if marker[0] == "~" or "`" not in info:
                fence = (marker[0], len(marker))
                offset += len(line)
                continue
        match = None if fence else re.match(
            r"^(?:##[ \t]+(?:[1-5][ \t]*[.、|｜][ \t]*)?(.+?)|【(.+?)】)"
            r"[ \t]*(?:\r?\n)?$",
            line,
        )
        if match is None:
            offset += len(line)
            continue
        heading = " ".join(
            (match.group(1) or match.group(2)).strip().casefold().split()
        )
        fields = [
            field
            for field, aliases in _FULL_PUBLICATION_SECTION_HEADINGS.items()
            if heading in aliases
        ]
        if len(fields) == 1:
            matches.append((offset, offset + len(line), fields[0]))
        offset += len(line)
    if [field for _, _, field in matches] != list(FULL_PUBLICATION_SECTION_FIELDS):
        return None
    sections: dict[str, str] = {}
    for index, (_, end, field) in enumerate(matches):
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(body)
        value = body[end:next_start].strip()
        if not value:
            return None
        sections[field] = value
    return sections


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"metadata.user_facing_report {field} must be non-empty")
    return text


def _unix_seconds(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MIN_PLAUSIBLE_UNIX_SECONDS
        or value > int(time.time()) + MAX_FUTURE_SKEW_SECONDS
    ):
        raise ValueError(
            f"metadata.user_facing_report {field} must be a plausible "
            "Unix-seconds timestamp"
        )
    return value


def _commerce_report_details(raw: Mapping[str, Any], *, level: str) -> dict[str, Any]:
    """Preserve typed report fields instead of silently dropping contract evidence."""
    fields = {
        "report": {
            "title": "text", "body_field": "text", "body": "text",
            "assets": "empty", "evidence_gaps": "gaps",
        },
        "row": {
            "canonical_url": "url", "post_url": "url", "evidence_url": "url",
            "visible": "bool", "pending_review": "bool", "evidence_gaps": "gaps",
        },
        "coverage": {key: "count" for key in COMMERCE_COVERAGE_COUNTERS},
    }[level]
    result = {}
    for key, kind in fields.items():
        if key not in raw:
            continue
        value = raw[key]
        # Older controller builders use an empty URL for an unknown location.
        if kind == "url" and isinstance(value, str) and not value.strip():
            value = None
        valid = True
        if kind == "text":
            valid = isinstance(value, str) and bool(value.strip())
        elif kind == "empty":
            valid = value == []
        elif kind == "gaps":
            valid = isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
        elif kind == "bool":
            valid = value is None or isinstance(value, bool)
        elif kind == "count":
            valid = value is None or (type(value) is int and value >= 0)
        elif kind == "url" and value is not None:
            valid = isinstance(value, str)
            if valid:
                try:
                    url = urlsplit(value)
                    valid = url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
                except ValueError:
                    valid = False
        if not valid:
            raise ValueError(f"metadata.user_facing_report {level}.{key} has an invalid {kind} value")
        result[key] = value
    return result


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
        row.update(_commerce_report_details(raw_row, level="row"))
        if row.get("canonical_url") is not None and row["canonical_url"].rstrip("/") != (
            f"https://www.facebook.com/groups/{destination_id}"
        ):
            raise ValueError("metadata.user_facing_report canonical_url must match destination_id")
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
            **_commerce_report_details(raw_item, level="coverage"),
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
    if raw["complete"] and not calculated_complete:
        raise ValueError(
            "metadata.user_facing_report complete must match coverage completeness"
        )

    normalized = {
        "kind": kind,
        "delivery": delivery,
        "complete": raw["complete"],
        "as_of": as_of,
        "observed_at": report_observed_at,
        "rows": rows,
        "coverage": coverage,
        **_commerce_report_details(raw, level="report"),
    }
    if "body" in raw:
        for key in ("title", "body_field", "body"):
            if key not in normalized:
                raise ValueError(f"metadata.user_facing_report requires {key} with an inline body")
    for item in coverage:
        subject_rows = [row for row in rows if row["subject_key"] == item["subject_key"]]
        published = sum(row["status"] == "public" for row in subject_rows)
        unknown = sum(row["status"] in UNRESOLVED_COMMERCE_GROUP_STATUSES for row in subject_rows)
        expected = {
            "destination_target": item["expected_total"],
            "verified_published_count": published, "unknown_count": unknown,
            "unnamed_gap": item["gap_count"],
            "remaining_verified_publication_gap": (
                max(item["expected_total"] - published, 0) if item["expected_total"] is not None else None
            ),
        }
        for key, value in expected.items():
            if key in item and item[key] != value and (
                item[key] is not None or raw.get("evidence_mode") == "historical_verified"
            ):
                raise ValueError(f"metadata.user_facing_report coverage.{key} does not reconcile with rows")
    mode = raw.get("evidence_mode")
    if mode is not None:
        if mode != "historical_verified" or raw["complete"] or any(item["complete"] for item in coverage):
            raise ValueError("historical_verified reports must remain incomplete")
        source = raw.get("evidence_source")
        if not isinstance(source, Mapping) or any(
            type(source.get(key)) is not int or source[key] <= 0
            for key in ("execution_run_id", "review_run_id")
        ):
            raise ValueError("historical_verified requires exact execution_run_id and review_run_id")
        for key in ("title", "body_field", "body", "assets", "evidence_gaps"):
            if key not in normalized:
                raise ValueError(f"historical_verified requires report.{key}")
        for row in rows:
            if not all(key in row for key in (
                "canonical_url", "visible", "pending_review", "post_url", "evidence_url", "evidence_gaps",
            )):
                raise ValueError("historical_verified requires explicit status, URLs and evidence_gaps on every row")
        for item in coverage:
            if not all(key in item for key in COMMERCE_COVERAGE_COUNTERS):
                raise ValueError("historical_verified requires explicit coverage counts; use null for unverified counts")
        normalized["evidence_mode"] = mode
        normalized["evidence_source"] = {key: source[key] for key in ("execution_run_id", "review_run_id")}
    elif "evidence_source" in raw:
        raise ValueError("evidence_source requires historical_verified evidence_mode")
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
    if delivery not in {INLINE_ONLY_DELIVERY, "inline_with_attachment"}:
        raise ValueError(
            "metadata.user_facing_report content_package delivery must be "
            "inline_only or inline_with_attachment"
        )
    if not isinstance(raw.get("complete"), bool):
        raise ValueError(
            "metadata.user_facing_report content_package complete must be boolean"
        )
    title = _required_text(raw.get("title"), "title")
    body = _required_text(raw.get("body"), "body")
    observed_at = _unix_seconds(raw.get("observed_at"), "observed_at")
    body_field = str(raw.get("body_field") or "").strip()
    raw_assets = raw.get("assets", [])
    if not isinstance(raw_assets, list):
        raise ValueError(
            "metadata.user_facing_report content_package assets must be a list"
        )
    if delivery == INLINE_ONLY_DELIVERY and not body_field:
        raise ValueError(
            "metadata.user_facing_report content_package body_field must be "
            "non-empty for inline_only delivery"
        )
    if delivery == INLINE_ONLY_DELIVERY and raw_assets:
        raise ValueError(
            "metadata.user_facing_report content_package assets must be empty "
            "for inline_only delivery"
        )
    if delivery == "inline_with_attachment" and not raw_assets:
        raise ValueError(
            "metadata.user_facing_report content_package assets must be a "
            "non-empty list for inline_with_attachment delivery"
        )
    package_kind = str(raw.get("package_kind") or "").strip()
    if package_kind and package_kind != FULL_PUBLICATION_PACKAGE_KIND:
        raise ValueError(
            "metadata.user_facing_report content_package package_kind is unsupported"
        )
    sections: dict[str, str] = {}
    if package_kind == FULL_PUBLICATION_PACKAGE_KIND:
        raw_sections = raw.get("sections")
        if not isinstance(raw_sections, Mapping):
            raise ValueError(
                "metadata.user_facing_report full publication package requires sections"
            )
        sections = {
            field: _required_text(raw_sections.get(field), f"sections.{field}")
            for field in FULL_PUBLICATION_SECTION_FIELDS
        }

    assets: list[dict[str, Any]] = []
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
        asset = {
            "filename": filename,
            "label": _required_text(
                raw_asset.get("label"), f"assets[{index}].label"
            ),
            "path": path,
            "sha256": digest,
        }
        family = str(raw_asset.get("asset_family") or "").strip()
        if family:
            dimensions = raw_asset.get("dimensions")
            match = re.fullmatch(
                r"\s*([1-9][0-9]*)\s*[x×]\s*([1-9][0-9]*)\s*",
                dimensions,
                flags=re.IGNORECASE,
            ) if isinstance(dimensions, str) else None
            width = raw_asset.get("width")
            height = raw_asset.get("height")
            if match is not None:
                parsed = (int(match.group(1)), int(match.group(2)))
                if width is None and height is None:
                    width, height = parsed
                elif (width, height) != parsed:
                    raise ValueError(
                        f"metadata.user_facing_report assets[{index}] dimensions conflict"
                    )
            if (
                isinstance(width, bool)
                or isinstance(height, bool)
                or not isinstance(width, int)
                or not isinstance(height, int)
                or width <= 0
                or height <= 0
            ):
                raise ValueError(
                    f"metadata.user_facing_report assets[{index}] family requires dimensions"
                )
            asset.update({
                "asset_family": family,
                "width": width,
                "height": height,
                "dimensions": f"{width}x{height}",
            })
        assets.append(asset)
    if package_kind == FULL_PUBLICATION_PACKAGE_KIND:
        by_family = {asset.get("asset_family"): asset for asset in assets}
        if len(by_family) != len(assets) or set(by_family) != {"page_hero", "audio_brief"}:
            raise ValueError(
                "metadata.user_facing_report full publication package requires exactly "
                "one page_hero and one audio_brief"
            )
        if (
            by_family["page_hero"]["width"] * 9
            != by_family["page_hero"]["height"] * 16
            or by_family["audio_brief"]["width"]
            != by_family["audio_brief"]["height"]
        ):
            raise ValueError(
                "metadata.user_facing_report full publication package asset ratios are invalid"
            )
        if any(value not in body for value in sections.values()):
            raise ValueError(
                "metadata.user_facing_report full publication package body must contain every section"
            )
    normalized = {
        "kind": CONTENT_PACKAGE_REPORT_KIND,
        "delivery": delivery,
        "complete": raw["complete"],
        "title": title,
        "body": body,
        "observed_at": observed_at,
        "assets": assets,
    }
    if body_field:
        normalized["body_field"] = body_field
    if package_kind:
        normalized["package_kind"] = package_kind
        normalized["sections"] = sections
        receipts = raw.get("policy_receipts")
        if not isinstance(receipts, list):
            raise ValueError(
                "metadata.user_facing_report full publication package requires policy_receipts"
            )
        if not all(
            isinstance(receipt, Mapping)
            and receipt.get("loaded") is True
            and _required_text(receipt.get("policy_id"), "policy_receipts.policy_id")
            and _required_text(receipt.get("version"), "policy_receipts.version")
            and re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256") or ""))
            for receipt in receipts
        ):
            raise ValueError(
                "metadata.user_facing_report full publication package policy_receipts are invalid"
            )
        normalized["policy_receipts"] = [dict(receipt) for receipt in receipts]
        if raw.get("external_effects") != []:
            raise ValueError(
                "metadata.user_facing_report full publication package requires external_effects=[]"
            )
        normalized["external_effects"] = []
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


def delivery_contract_from_report(report: Any) -> dict[str, Any]:
    """Build the minimal inline delivery contract implied by a valid report.

    This keeps legacy execution cards deliverable when they recorded a valid
    user-facing payload before the explicit contract field was present.
    """
    normalized = normalize_user_facing_report(report)
    if normalized["kind"] == CONTENT_PACKAGE_REPORT_KIND:
        contract = {
            "required": True,
            "kind": CONTENT_PACKAGE_REPORT_KIND,
            "delivery": normalized["delivery"],
        }
        if normalized["delivery"] == INLINE_ONLY_DELIVERY:
            contract["body_field"] = normalized["body_field"]
        else:
            contract["asset_filenames"] = [
                asset["filename"] for asset in normalized["assets"]
            ]
        return contract
    return {
        "required": True,
        "kind": COMMERCE_GROUP_REPORT_KIND,
        "delivery": normalized["delivery"],
        "subject_keys": [
            item["subject_key"] for item in normalized["coverage"]
        ],
        **({"body_field": normalized["body_field"]} if normalized.get("body_field") else {}),
    }


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
        if normalized["delivery"] == INLINE_ONLY_DELIVERY:
            return (
                delivery_contract.get("required") is True
                and delivery_contract.get("kind") == CONTENT_PACKAGE_REPORT_KIND
                and delivery_contract.get("delivery") == INLINE_ONLY_DELIVERY
                and delivery_contract.get("body_field")
                == normalized.get("body_field")
                and bool(normalized["complete"])
            )
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
    # Legacy commerce builders use body_field as a format discriminator and
    # render rows directly. Bind the field when an explicit inline body exists.
    if delivery_contract.get("body_field") and delivery_contract["body_field"] != normalized.get("body_field"):
        return False
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
        if normalized["delivery"] == INLINE_ONLY_DELIVERY:
            return (
                delivery_contract.get("required") is True
                and delivery_contract.get("kind") == CONTENT_PACKAGE_REPORT_KIND
                and delivery_contract.get("delivery") == INLINE_ONLY_DELIVERY
                and delivery_contract.get("body_field")
                == normalized.get("body_field")
            )
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
    # Legacy commerce builders use body_field as a format discriminator and
    # render rows directly. Bind the field when an explicit inline body exists.
    if delivery_contract.get("body_field") and delivery_contract["body_field"] != normalized.get("body_field"):
        return False
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
    sections = []
    if normalized.get("evidence_mode") == "historical_verified":
        sections.append("歷史已驗收證據；本次未重新查核 Facebook，目前狀態未驗證。")
    sections.append(f"社團刊登狀態（截至 {normalized['as_of']}）")
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
