"""Validation helpers for structured, chat-first task deliverables."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


COMMERCE_GROUP_REPORT_KIND = "commerce_group_status"
INLINE_ONLY_DELIVERY = "inline_only"
SECONDHAND_COMMERCE_RECONCILIATION_SUBJECT_KEYS = frozenset({
    "carimali-armonia-soft-plus",
    "kolin-kd291m06",
    "celestron-130eq",
})
SECONDHAND_COMMERCE_SUBJECT_ALIASES = {
    "carimali-armonia-soft-plus": frozenset({
        "carimali-armonia-soft-plus",
        "carimali",
        "36803832485927906",
        "facebook_marketplace:36803832485927906",
    }),
    "kolin-kd291m06": frozenset({
        "kolin-kd291m06",
        "kolin",
        "37217119148451132",
        "facebook_marketplace:37217119148451132",
        "915975414881937",
        "facebook_marketplace:915975414881937",
    }),
    "celestron-130eq": frozenset({
        "celestron-130eq",
        "celestron",
        "27909676598721497",
        "facebook_marketplace:27909676598721497",
    }),
}
SECONDHAND_COMMERCE_SUBJECT_LABELS = {
    "carimali-armonia-soft-plus": "Carimali Armonia Soft Plus",
    "kolin-kd291m06": "Kolin KD-291M06",
    "celestron-130eq": "Celestron 130EQ",
}
SECONDHAND_COMMERCE_PRIMARY_LISTING_IDS = {
    "carimali-armonia-soft-plus": "36803832485927906",
    "kolin-kd291m06": "37217119148451132",
    "celestron-130eq": "27909676598721497",
}

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
COMMERCE_GROUP_STATUS_LABELS = {
    "public": "已刊登",
    "pending_approval": "待審核",
    "rejected": "已拒絕",
    "not_found": "未找到",
    "ambiguous_after_submit": "送出後狀態不明",
    "not_posted": "未刊登",
    "unknown": "尚未驗證",
}
# Reports are rendered into bounded chat chunks, so the structured payload may
# safely be larger than one Telegram message.  12k rejected ordinary 27-row
# Marketplace destination lists before the existing chunker could run.
MAX_REPORT_JSON_CHARS = 64_000
MAX_FUTURE_SKEW_SECONDS = 300
VISIBLE_NAME_DESTINATION_PREFIX = "visible-name-sha256:"
COMMERCE_REPORT_SCOPES = frozenset({"selected_listings", "all_listings"})


def canonicalize_commerce_subject_keys(subject_keys: Any) -> list[str]:
    """Collapse known product labels and listing ids to durable subject keys.

    Grace may naturally describe one product with both its display label and
    Marketplace listing id. They are aliases, not two independently required
    report subjects. Unknown keys remain exact so this compatibility layer
    cannot broaden a contract to another product.
    """
    if not isinstance(subject_keys, list):
        return []
    alias_lookup = {
        alias.casefold(): canonical
        for canonical, aliases in SECONDHAND_COMMERCE_SUBJECT_ALIASES.items()
        for alias in aliases
    }
    canonicalized: list[str] = []
    seen: set[str] = set()
    for raw_key in subject_keys:
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        clean_key = raw_key.strip()
        canonical_key = alias_lookup.get(clean_key.casefold(), clean_key)
        if canonical_key not in seen:
            seen.add(canonical_key)
            canonicalized.append(canonical_key)
    return canonicalized


def commerce_subject_listing_id(subject_key: Any) -> str:
    """Return the configured Marketplace listing id for one known subject."""
    bare = re.fullmatch(r"[1-9][0-9]*", str(subject_key or "").strip())
    if bare is not None:
        return bare.group(0)
    direct = re.fullmatch(
        r"facebook_marketplace:(?P<id>[1-9][0-9]*)",
        str(subject_key or "").strip(),
        flags=re.IGNORECASE,
    )
    if direct is not None:
        return direct.group("id")
    canonicalized = canonicalize_commerce_subject_keys([subject_key])
    if len(canonicalized) != 1:
        return ""
    canonical = canonicalized[0]
    return SECONDHAND_COMMERCE_PRIMARY_LISTING_IDS.get(canonical, "")


def commerce_subject_listing_ids(subject_key: Any) -> frozenset[str]:
    """Return every authoritative Marketplace listing ID for one subject."""
    bare = re.fullmatch(r"[1-9][0-9]*", str(subject_key or "").strip())
    if bare is not None:
        return frozenset({bare.group(0)})
    direct = re.fullmatch(
        r"facebook_marketplace:(?P<id>[1-9][0-9]*)",
        str(subject_key or "").strip(),
        flags=re.IGNORECASE,
    )
    if direct is not None:
        return frozenset({direct.group("id")})
    canonicalized = canonicalize_commerce_subject_keys([subject_key])
    if len(canonicalized) != 1:
        return frozenset()
    aliases = SECONDHAND_COMMERCE_SUBJECT_ALIASES.get(
        canonicalized[0], frozenset()
    )
    return frozenset(alias for alias in aliases if alias.isdigit())


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


def _nullable_non_negative_int(value: Any, field: str) -> int | None:
    """Validate a visible counter while preserving unavailable versus zero."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"metadata.user_facing_report {field} must be null or a "
            "non-negative integer"
        )
    return value


def _visible_destination_id(subject_key: str, destination_name: str) -> str:
    """Return a stable local row key when Facebook does not expose group ID.

    This identifier is deliberately namespaced so it can never be mistaken for
    a Facebook group ID or used as an external browser destination.  It only
    lets the read-only report and evidence ledger refer to the same visible
    named row without requiring CDP/DOM extraction.
    """
    normalized_name = " ".join(
        unicodedata.normalize("NFC", destination_name).split()
    )
    material = f"{subject_key}\n{normalized_name}".encode("utf-8")
    return VISIBLE_NAME_DESTINATION_PREFIX + hashlib.sha256(material).hexdigest()


def _normalize_destination_identity(
    *,
    raw_destination_id: Any,
    subject_key: str,
    destination_name: str,
    field: str,
) -> tuple[str, str]:
    """Normalize a platform ID or derive a non-external visible-name key."""
    supplied = str(raw_destination_id or "").strip()
    derived = _visible_destination_id(subject_key, destination_name)
    if not supplied:
        return derived, "visible_name"
    if re.fullmatch(r"[1-9][0-9]*", supplied) is not None:
        return supplied, "facebook_group_id"
    if supplied == derived:
        return supplied, "visible_name"
    raise ValueError(
        f"metadata.user_facing_report {field} must be a canonical numeric "
        "Facebook group ID, the canonical visible-name key, or omitted when "
        "the controlled UI does not expose an ID"
    )


def _publication_evidence_url(
    value: Any,
    *,
    destination_id: str,
    group_listing_id: str,
    required: bool,
    field: str,
) -> str:
    """Validate an exact Facebook page used as publication proof."""
    candidate = str(value or "").strip()
    if not candidate:
        if required:
            raise ValueError(
                f"metadata.user_facing_report {field} is required for public status"
            )
        return ""
    parsed = urlsplit(candidate)
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"metadata.user_facing_report {field} must be a credential-free "
            "canonical HTTPS Facebook URL without query or fragment"
        )
    path = parsed.path.rstrip("/") or "/"
    group_route = re.fullmatch(
        r"/groups/(?P<group>[1-9][0-9]*)/"
        r"(?:posts|permalink)/(?P<item>[1-9][0-9]*)",
        path,
    )
    contribution_route = re.fullmatch(
        r"/groups/(?P<group>[1-9][0-9]*)/user/(?P<user>[1-9][0-9]*)",
        path,
    )
    marketplace_route = re.fullmatch(
        r"/marketplace/item/(?P<item>[1-9][0-9]*)",
        path,
    )
    group_matches = bool(
        destination_id.isdigit()
        and (
            (group_route and group_route.group("group") == destination_id)
            or (
                contribution_route
                and contribution_route.group("group") == destination_id
            )
        )
    )
    marketplace_matches = bool(
        marketplace_route
        and group_listing_id
        and marketplace_route.group("item") == group_listing_id
    )
    if not group_matches and not marketplace_matches:
        raise ValueError(
            f"metadata.user_facing_report {field} must identify the exact "
            "destination group post, seller contribution page, or matching "
            "group-specific commerce listing"
        )
    return urlunsplit(("https", "www.facebook.com", path, "", ""))


def normalize_user_facing_report(raw: Any) -> dict[str, Any]:
    """Validate a product-to-Facebook-group report for inline delivery."""
    if not isinstance(raw, Mapping):
        raise ValueError("metadata.user_facing_report must be an object")
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
    report_scope = str(raw.get("scope") or "selected_listings").strip()
    if report_scope not in COMMERCE_REPORT_SCOPES:
        raise ValueError(
            "metadata.user_facing_report scope must be selected_listings or "
            "all_listings"
        )

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
        raw_subject_key = _required_text(
            raw_row.get("subject_key"), f"rows[{index}].subject_key"
        )
        subject_key = canonicalize_commerce_subject_keys([
            raw_subject_key,
        ])[0]
        destination_name = _required_text(
            raw_row.get("destination_name"),
            f"rows[{index}].destination_name",
        )
        destination_id, destination_identity_kind = (
            _normalize_destination_identity(
                raw_destination_id=raw_row.get("destination_id"),
                subject_key=subject_key,
                destination_name=destination_name,
                field=f"rows[{index}].destination_id",
            )
        )
        identity = (subject_key, destination_id)
        if identity in seen:
            raise ValueError(
                "metadata.user_facing_report contains a duplicate "
                f"subject/destination row: {subject_key}/{destination_id}"
            )
        seen.add(identity)
        status = _required_text(
            raw_row.get("status"), f"rows[{index}].status"
        ).lower()
        if status not in COMMERCE_GROUP_STATUSES:
            raise ValueError(
                f"metadata.user_facing_report rows[{index}].status is unsupported"
            )
        row_observed_at = _unix_seconds(
            raw_row.get("observed_at"), f"rows[{index}].observed_at"
        )
        source_listing_id = _required_text(
            raw_row.get("source_listing_id"),
            f"rows[{index}].source_listing_id",
        )
        raw_source_listing_ids = raw_row.get("source_listing_ids")
        if raw_source_listing_ids is None:
            source_listing_ids = [source_listing_id]
        elif not isinstance(raw_source_listing_ids, list):
            raise ValueError(
                f"metadata.user_facing_report rows[{index}].source_listing_ids "
                "must be a list"
            )
        else:
            source_listing_ids = []
            for raw_listing_id in raw_source_listing_ids:
                listing_id = str(raw_listing_id or "").strip()
                if (
                    re.fullmatch(r"[1-9][0-9]*", listing_id) is None
                    or listing_id in source_listing_ids
                ):
                    raise ValueError(
                        "metadata.user_facing_report "
                        f"rows[{index}].source_listing_ids must contain unique "
                        "canonical numeric Marketplace IDs"
                    )
                source_listing_ids.append(listing_id)
        if source_listing_id not in source_listing_ids:
            raise ValueError(
                f"metadata.user_facing_report rows[{index}].source_listing_ids "
                "must include source_listing_id"
            )
        source_listing_ids.sort()
        expected_listing_ids = commerce_subject_listing_ids(subject_key)
        if expected_listing_ids and not set(source_listing_ids) <= expected_listing_ids:
            raise ValueError(
                "metadata.user_facing_report "
                f"rows[{index}].source_listing_id must match "
                f"{subject_key} listing set "
                + ", ".join(sorted(expected_listing_ids))
            )
        if re.fullmatch(r"[1-9][0-9]*", source_listing_id) is None:
            raise ValueError(
                "metadata.user_facing_report "
                f"rows[{index}].source_listing_id must be a canonical numeric "
                "Facebook Marketplace listing ID"
            )
        group_listing_id = str(raw_row.get("group_listing_id") or "").strip()
        if group_listing_id and re.fullmatch(r"[1-9][0-9]*", group_listing_id) is None:
            raise ValueError(
                "metadata.user_facing_report "
                f"rows[{index}].group_listing_id must be a canonical numeric "
                "Facebook commerce listing ID or omitted"
            )
        evidence_url = _publication_evidence_url(
            raw_row.get("evidence_url"),
            destination_id=destination_id,
            group_listing_id=group_listing_id,
            required=status == "public",
            field=f"rows[{index}].evidence_url",
        )
        reaction_count = _nullable_non_negative_int(
            raw_row.get("reaction_count"), f"rows[{index}].reaction_count"
        )
        comment_count = _nullable_non_negative_int(
            raw_row.get("comment_count"), f"rows[{index}].comment_count"
        )
        view_count = _nullable_non_negative_int(
            raw_row.get("view_count"), f"rows[{index}].view_count"
        )
        metrics_observed_at = raw_row.get("metrics_observed_at")
        if metrics_observed_at is None:
            metrics_observed_at = row_observed_at
        else:
            metrics_observed_at = _unix_seconds(
                metrics_observed_at, f"rows[{index}].metrics_observed_at"
            )
        rows.append({
            "subject_key": subject_key,
            "subject_label": _required_text(
                raw_row.get("subject_label"), f"rows[{index}].subject_label"
            ),
            "destination_id": destination_id,
            "destination_identity_kind": destination_identity_kind,
            "destination_name": destination_name,
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
            "evidence_url": evidence_url,
            "source_listing_id": source_listing_id,
            "source_listing_ids": source_listing_ids,
            "group_listing_id": group_listing_id,
            "reaction_count": reaction_count,
            "comment_count": comment_count,
            "view_count": view_count,
            "metrics_observed_at": metrics_observed_at,
            "source_task_id": str(raw_row.get("source_task_id") or "").strip(),
        })

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
        raw_subject_key = _required_text(
            raw_item.get("subject_key"), f"coverage[{index}].subject_key"
        )
        subject_key = canonicalize_commerce_subject_keys([
            raw_subject_key,
        ])[0]
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
        listing_click_count = _nullable_non_negative_int(
            raw_item.get("listing_click_count"),
            f"coverage[{index}].listing_click_count",
        )
        listing_click_window_days = _nullable_non_negative_int(
            raw_item.get("listing_click_window_days"),
            f"coverage[{index}].listing_click_window_days",
        )
        if (listing_click_count is None) != (listing_click_window_days is None):
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}] must provide "
                "both listing_click_count and listing_click_window_days, or neither"
            )
        if listing_click_window_days == 0:
            raise ValueError(
                f"metadata.user_facing_report coverage[{index}]."
                "listing_click_window_days must be positive when provided"
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
            "listing_click_count": listing_click_count,
            "listing_click_window_days": listing_click_window_days,
            "note": _required_text(
                raw_item.get("note"), f"coverage[{index}].note"
            ),
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
    all_listing_subjects_present = bool(
        report_scope != "all_listings"
        or SECONDHAND_COMMERCE_RECONCILIATION_SUBJECT_KEYS <= coverage_keys
    )
    calculated_complete = all_listing_subjects_present and all(
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
    rows.sort(key=lambda row: (
        row["subject_key"],
        tuple(row["source_listing_ids"]),
        row["destination_name"].casefold(),
        row["destination_id"],
    ))
    coverage.sort(key=lambda item: (item["subject_key"], item["subject_label"]))
    normalized = {
        "kind": kind,
        "delivery": delivery,
        "scope": report_scope,
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
    report_subjects = set(canonicalize_commerce_subject_keys([
        item["subject_key"] for item in normalized["coverage"]
    ]))
    contract_subjects = set(canonicalize_commerce_subject_keys(
        requested_subjects,
    ))
    return (
        report_subjects == contract_subjects
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
    report_subjects = canonicalize_commerce_subject_keys([
        item["subject_key"] for item in normalized["coverage"]
    ])
    contract_subjects = canonicalize_commerce_subject_keys(requested_subjects)
    return set(report_subjects) == set(contract_subjects)


def report_allows_closed_outcome(report: Any) -> bool:
    try:
        normalized = normalize_user_facing_report(report)
    except ValueError:
        return False
    return bool(normalized["complete"])


def report_is_inline_only(report: Any) -> bool:
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
    rows_by_subject: dict[str, list[dict[str, Any]]] = {}
    for row in normalized["rows"]:
        rows_by_subject.setdefault(row["subject_key"], []).append(row)
    title = (
        "Facebook 全部刊登與互動狀態清單"
        if normalized["scope"] == "all_listings"
        else "Facebook 刊登與互動狀態清單"
    )
    sections = [f"{title}（截至 {normalized['as_of']}）"]
    for coverage in normalized["coverage"]:
        subject_rows = rows_by_subject.get(coverage["subject_key"], [])
        subject_label = SECONDHAND_COMMERCE_SUBJECT_LABELS.get(
            coverage["subject_key"],
            coverage["subject_label"],
        )
        listing_ids = sorted({
            listing_id
            for row in subject_rows
            for listing_id in row["source_listing_ids"]
        } | set(commerce_subject_listing_ids(coverage["subject_key"])))
        listing_suffix = (
            f"（Marketplace {'、'.join(listing_ids)}）"
            if listing_ids
            else ""
        )
        lines = [f"\n{subject_label}{listing_suffix}"]
        if coverage["listing_click_count"] is not None:
            lines.append(
                "Marketplace 商品詳情點擊："
                f"{coverage['listing_click_count']:,}"
                f"（最近 {coverage['listing_click_window_days']} 天）"
            )
        else:
            lines.append("Marketplace 商品詳情點擊：—（Facebook 未提供）")
        if subject_rows:
            for row in subject_rows:
                status_label = COMMERCE_GROUP_STATUS_LABELS.get(
                    row["status"],
                    row["status_label"],
                )
                row_listing_ids = "、".join(row["source_listing_ids"])
                listing_label = (
                    f"；Marketplace {row_listing_ids}；"
                    f"群組刊登 {row['group_listing_id']}"
                    if row["group_listing_id"]
                    else f"；Marketplace {row_listing_ids}"
                )
                group_id_label = (
                    f"；社團 {row['destination_id']}"
                    if row["destination_identity_kind"] == "facebook_group_id"
                    else ""
                )
                metric = lambda value: "—" if value is None else f"{value:,}"
                lines.append(
                    f"- {row['destination_name']}：{status_label}｜"
                    f"讚 {metric(row['reaction_count'])}｜"
                    f"留言 {metric(row['comment_count'])}｜"
                    f"觀看 {metric(row['view_count'])}"
                    f"（{row['verified_at']}{listing_label}{group_id_label}）"
                    f"\n  {row['evidence']}"
                    + (
                        f"\n  證據：{row['evidence_url']}"
                        if row["evidence_url"]
                        else ""
                    )
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
    sections.append("\n註：— 代表 Facebook 未提供或目前不可見，不等於 0。")
    text = "\n".join(sections)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive UTF-16 unit limit")
    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    for character in text:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if current and current_units + character_units > max_chars:
            chunks.append("".join(current))
            current = []
            current_units = 0
        current.append(character)
        current_units += character_units
    if current:
        chunks.append("".join(current))
    return chunks
