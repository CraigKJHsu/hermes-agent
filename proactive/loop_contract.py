"""Fail-closed Loop Contract validation for Grace -> ClawOps delegation."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping


CONTRACT_VERSION = "1.0"


class LoopContractError(ValueError):
    """Raised when Grace has not supplied an executable contract."""


_DISPLAY_ID_END = r"(?![0-9A-Za-z_-]|\.(?=[0-9A-Za-z_]))"
_FACEBOOK_MARKETPLACE_TARGET_PATTERNS = (
    re.compile(
        r"Facebook\s+Marketplace\s+item\s+(?P<id>[0-9]+)"
        + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"Facebook\s+Marketplace\s+(?:listing\s+)?ID\s*[:：]?\s*"
        r"(?P<id>[0-9]+)" + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"Facebook\s+(?:市集項目|市集刊登)(?:\s*ID)?\s*[:：]?\s*"
        r"(?P<id>[0-9]+)" + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
)
_FACEBOOK_GROUP_TARGET_PATTERNS = (
    re.compile(
        r"Facebook\s+Group(?:\s+ID)?\s*[:：]?\s*(?P<id>[0-9]+)"
        + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"Facebook\s+(?:社團|群組)(?:\s*ID)?\s*[:：]?\s*"
        r"(?P<id>[0-9]+)" + _DISPLAY_ID_END,
        flags=re.IGNORECASE,
    ),
)
_FACEBOOK_MARKETPLACE_EXACT_TARGET_PATTERNS = (
    re.compile(
        r"(?:https://)?(?:(?:www|m)\.)?facebook\.com/marketplace/item/"
        r"(?P<id>[0-9]+)(?:[/?#][^\s]*)?",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"facebook:marketplace:(?P<id>[0-9]+)",
        flags=re.IGNORECASE,
    ),
)
_FACEBOOK_GROUP_EXACT_TARGET_PATTERNS = (
    re.compile(
        r"(?:https://)?(?:(?:www|m)\.)?facebook\.com/groups/"
        r"(?P<id>[0-9]+)(?:[/?#][^\s]*)?",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"facebook:group:(?P<id>[0-9]+)",
        flags=re.IGNORECASE,
    ),
)
_EXPLICIT_TARGET_TOKEN_SPLIT_RE = re.compile(r"[\s→]+")
_EXPLICIT_TARGET_TOKEN_WRAPPERS = (
    "()[]{}<>\"'，,。；;「」.!?！？（）【】《》"
)


def facebook_crosspost_target_ids(
    targets: Any,
) -> tuple[frozenset[str], frozenset[str]]:
    """Read only explicit Marketplace listing and Facebook group identifiers.

    This intentionally accepts the canonical labels emitted by Grace plus the
    corresponding Facebook URLs and localized ID labels. Arbitrary numbers are
    never treated as authority.
    """
    if not isinstance(targets, list):
        return frozenset(), frozenset()
    target_text = " ".join(str(target or "") for target in targets)
    listing_ids = {
        match.group("id")
        for pattern in _FACEBOOK_MARKETPLACE_TARGET_PATTERNS
        for match in pattern.finditer(target_text)
    }
    group_ids = {
        match.group("id")
        for pattern in _FACEBOOK_GROUP_TARGET_PATTERNS
        for match in pattern.finditer(target_text)
    }
    for target in targets:
        for raw_token in _EXPLICIT_TARGET_TOKEN_SPLIT_RE.split(str(target)):
            token = raw_token.strip(_EXPLICIT_TARGET_TOKEN_WRAPPERS)
            if not token:
                continue
            for pattern in _FACEBOOK_MARKETPLACE_EXACT_TARGET_PATTERNS:
                match = pattern.fullmatch(token)
                if match is not None:
                    listing_ids.add(match.group("id"))
            for pattern in _FACEBOOK_GROUP_EXACT_TARGET_PATTERNS:
                match = pattern.fullmatch(token)
                if match is not None:
                    group_ids.add(match.group("id"))
    return frozenset(listing_ids), frozenset(group_ids)


def contract_fingerprint(contract: Mapping[str, Any]) -> str:
    """Return the immutable scope fingerprint used by approval and callbacks.

    The persisted fingerprint field is excluded from its own digest so the
    approval provenance can carry the exact value without creating a recursive
    hash definition.
    """
    canonical_value = deepcopy(dict(contract or {}))
    canonical_value.pop("approval_provenance", None)
    canonical = json.dumps(
        canonical_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_loop_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized contract or reject it before a task is created."""
    value = deepcopy(dict(contract or {}))
    errors: list[str] = []

    def required_text(path: str) -> None:
        cur: Any = value
        for key in path.split("."):
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if not isinstance(cur, str) or not cur.strip():
            errors.append(f"{path} is required")

    def required_list(path: str) -> None:
        cur: Any = value
        for key in path.split("."):
            cur = cur.get(key) if isinstance(cur, Mapping) else None
        if (
            not isinstance(cur, list)
            or not cur
            or any(not isinstance(item, str) or not item.strip() for item in cur)
        ):
            errors.append(f"{path} must contain only non-empty strings")

    for path in (
        "identity.project",
        "identity.topic_name",
        "identity.request_instance_id",
        "original_request",
        "grace_interpretation",
        "trigger",
        "goal.objective",
        "memory.namespace",
        "completion_mode",
    ):
        required_text(path)
    if value.get("completion_mode") not in {"terminal", "intermediate"}:
        errors.append("completion_mode must be terminal or intermediate")
    thread_id = (value.get("identity") or {}).get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        errors.append("identity.thread_id must be a string")
    for path in (
        "goal.deliverables",
        "goal.non_goals",
        "scope.allowed",
        "scope.forbidden",
        "verification.checks",
        "verification.evidence_required",
        "verification.acceptance_criteria",
        "stop_rules.success",
        "stop_rules.blocked",
        "stop_rules.no_progress",
        "memory.working",
        "memory.promote_on_acceptance",
    ):
        required_list(path)
    if "external_targets" in value:
        required_list("external_targets")
    if "facebook_crosspost" in value:
        crosspost = value.get("facebook_crosspost")
        if not isinstance(crosspost, Mapping):
            errors.append("facebook_crosspost must be an object")
        else:
            listing_id = crosspost.get("marketplace_listing_id")
            group_ids = crosspost.get("group_ids")
            if not isinstance(listing_id, str) or not listing_id.isdigit():
                errors.append(
                    "facebook_crosspost.marketplace_listing_id must be a "
                    "numeric string"
                )
            if (
                not isinstance(group_ids, list)
                or not group_ids
                or any(
                    not isinstance(group_id, str) or not group_id.isdigit()
                    for group_id in group_ids
                )
                or len(set(group_ids)) != len(group_ids)
            ):
                errors.append(
                    "facebook_crosspost.group_ids must contain unique numeric "
                    "strings"
                )
            targets = value.get("external_targets")
            mentioned_listing_ids, mentioned_group_ids = (
                facebook_crosspost_target_ids(targets)
            )
            if not mentioned_listing_ids or not mentioned_group_ids:
                errors.append(
                    "facebook_crosspost requires Facebook Marketplace and "
                    "group destinations in external_targets"
                )
            if isinstance(group_ids, list) and all(
                isinstance(group_id, str) for group_id in group_ids
            ):
                if mentioned_group_ids != frozenset(group_ids):
                    errors.append(
                        "facebook_crosspost.group_ids must match group ids "
                        "shown in external_targets"
                    )
            if (
                not mentioned_listing_ids
                or (
                    not isinstance(listing_id, str)
                    or mentioned_listing_ids != frozenset({listing_id})
                )
            ):
                errors.append(
                    "facebook_crosspost.marketplace_listing_id must match "
                    "the listing id shown in external_targets"
                )

    max_iterations = value.get("stop_rules", {}).get("max_iterations")
    if not isinstance(max_iterations, int) or not 1 <= max_iterations <= 20:
        errors.append("stop_rules.max_iterations must be an integer from 1 to 20")
    max_runtime = value.get("stop_rules", {}).get("max_runtime_seconds")
    if not isinstance(max_runtime, int) or not 60 <= max_runtime <= 14400:
        errors.append("stop_rules.max_runtime_seconds must be 60..14400")

    if errors:
        raise LoopContractError("; ".join(errors))
    value["contract_version"] = CONTRACT_VERSION
    return value
