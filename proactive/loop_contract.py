"""Fail-closed Loop Contract validation for Grace -> ClawOps delegation."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping

from proactive.policy_registry import PolicyRegistryError, resolve_contract_policies


CONTRACT_VERSION = "1.0"

_INTERNAL_ONLY_INTENT_MARKERS = (
    "僅產出",
    "僅修訂",
    "僅校正",
    "僅提供",
    "只產出",
    "只修訂",
    "只校正",
    "只提供",
)
_NO_EXTERNAL_ACTION_MARKERS = (
    "不登入或操作",
    "不登入或上架",
    "不上架或操作",
    "不登入、編輯或發布",
    "不登入、操作或發布",
    "不登入、發布、編輯、排程或操作",
)
_MARKETPLACE_MORE_OPTIONS_LABELS = ("more options", "更多選項")
_MARKETPLACE_LIST_MORE_LABELS = (
    "list in more places",
    "list your item in more places",
    "刊登到更多地方",
    "在更多地方刊登",
)
_MARKETPLACE_READONLY_LABELS = ("唯讀", "只讀", "read-only", "readonly")
_MARKETPLACE_SELECTION_TERMS = (
    "checkbox",
    "switch",
    "select any option",
    "select option",
    "勾選",
    "選取",
)
_MARKETPLACE_SUBMISSION_TERMS = (
    "post",
    "publish",
    "submit",
    "cross-post",
    "crosspost",
    "發布",
    "刊登",
    "提交",
    "跨貼",
)
_MARKETPLACE_BROAD_MUTATION_BANS = (
    "no external state change",
    "no facebook state change",
    "do not change external state",
    "不得變更外部狀態",
    "不得變更任何外部狀態",
    "不變更外部狀態",
    "不變更任何外部狀態",
    "不變更任何 facebook 外部狀態",
    "不得改變外部狀態",
    "不得改變任何外部狀態",
    "不改變外部狀態",
    "不改變任何外部狀態",
    "任何外部狀態變更",
    "任何 facebook 外部狀態變更",
    "任何 facebook 狀態變更",
    "不得對 facebook 做任何變更",
    "不對 facebook 做任何變更",
)


class LoopContractError(ValueError):
    """Raised when Grace has not supplied an executable contract."""


def is_internal_only_target(value: object) -> bool:
    """Recognize a target that names a platform only to deny operating it.

    The Chinese form is intentionally conservative: both a local-content
    intent and an explicit no-platform-action phrase must be present.
    """
    target = str(value or "").strip().lower()
    zero_effect_sentinel = re.search(
        r"(?<![\w-])zero (?:"
        r"facebook, meta, spotify, gemini notebook, or other "
        r")?external platform action[.!]?$",
        target,
    )
    canonical_sentinel = (
        target.startswith("internal ")
        and (
            "no external platform action" in target
            or zero_effect_sentinel is not None
        )
    )
    explicit_zh_internal_only = (
        any(marker in target for marker in _INTERNAL_ONLY_INTENT_MARKERS)
        and any(marker in target for marker in _NO_EXTERNAL_ACTION_MARKERS)
    )
    return canonical_sentinel or explicit_zh_internal_only


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def browser_readonly_marketplace_fallback_listing_id(
    value: Mapping[str, Any],
) -> str | None:
    """Recover only a fully scoped legacy Marketplace read-only contract.

    A generic ``browser_readonly`` route is accepted only when one numeric
    listing is bound to the complete local UI transition and the contract
    explicitly bans selection, submission, and external state changes.
    Callers must replace the supplied prose with canonical read-only sections
    before routing or persistence.
    """
    if (
        str(value.get("task_type") or "").strip().casefold()
        != "browser_readonly"
        or isinstance(value.get("facebook_crosspost"), Mapping)
    ):
        return None
    targets = value.get("external_targets")
    if not isinstance(targets, list) or len(targets) != 1:
        return None
    target = " ".join(str(targets[0] or "").split())
    match = re.fullmatch(
        r"(?:Facebook\s+Marketplace\s+(?:listing(?:\s+ID)?|item)\s+)?"
        r"(?P<id>[1-9][0-9]*)",
        target,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    listing_id = match.group("id")

    goal = value.get("goal")
    scope = value.get("scope")
    verification = value.get("verification")
    allowed = scope.get("allowed") if isinstance(scope, Mapping) else None
    forbidden = scope.get("forbidden") if isinstance(scope, Mapping) else None
    if not isinstance(allowed, list) or not isinstance(forbidden, list):
        return None

    intent_parts: list[str] = []
    for candidate in (
        goal.get("objective") if isinstance(goal, Mapping) else None,
        goal.get("deliverables") if isinstance(goal, Mapping) else None,
        allowed,
        verification.get("checks") if isinstance(verification, Mapping) else None,
    ):
        if isinstance(candidate, list):
            intent_parts.extend(str(item or "") for item in candidate)
        elif candidate is not None:
            intent_parts.append(str(candidate))
    normalized_intent = " ".join(" ".join(intent_parts).split()).casefold()
    normalized_allowed = [
        " ".join(str(item or "").split()).casefold() for item in allowed
    ]
    normalized_forbidden = " ".join(
        " ".join(str(item or "").split()).casefold() for item in forbidden
    )
    listing_bound_transition = any(
        listing_id in clause
        and "facebook" in clause
        and "marketplace" in clause
        and _contains_any(clause, _MARKETPLACE_MORE_OPTIONS_LABELS)
        and _contains_any(clause, _MARKETPLACE_LIST_MORE_LABELS)
        and _contains_any(clause, _MARKETPLACE_READONLY_LABELS)
        for clause in normalized_allowed
    )
    if not (
        listing_bound_transition
        and _contains_any(normalized_intent, _MARKETPLACE_MORE_OPTIONS_LABELS)
        and _contains_any(normalized_intent, _MARKETPLACE_LIST_MORE_LABELS)
        and _contains_any(normalized_intent, _MARKETPLACE_READONLY_LABELS)
        and _contains_any(normalized_forbidden, _MARKETPLACE_SELECTION_TERMS)
        and _contains_any(normalized_forbidden, _MARKETPLACE_SUBMISSION_TERMS)
        and _contains_any(
            normalized_forbidden,
            _MARKETPLACE_BROAD_MUTATION_BANS,
        )
    ):
        return None
    return listing_id


def canonical_marketplace_readonly_sections(
    listing_id: str,
) -> dict[str, dict[str, Any]]:
    """Build the fixed zero-effect Marketplace candidate-discovery scope."""
    return {
        "goal": {
            "objective": (
                "Read Facebook Marketplace listing "
                f"{listing_id} visible List in more places candidates without "
                "changing external state"
            ),
            "deliverables": [
                "Visible candidate group names and statuses",
            ],
            "non_goals": [
                "No checkbox selection, submission, publishing, or external "
                "state change",
            ],
        },
        "scope": {
            "allowed": [
                "Read-only Facebook Marketplace listing "
                f"{listing_id}: open More options → List in more places and "
                "read visible candidate names/status only",
            ],
            "forbidden": [
                "Do not select any checkbox or switch",
                "Do not click Post, Publish, Submit, or Share",
                "Do not change external state",
            ],
        },
        "verification": {
            "checks": [
                "Read the listing-bound List in more places candidate surface",
            ],
            "evidence_required": [
                "Visible candidate names, statuses, and observation time",
            ],
            "acceptance_criteria": [
                "Every reported candidate is visibly read from the exact "
                "listing and no external state changes occur",
            ],
        },
    }


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
    try:
        value = resolve_contract_policies(contract)
    except PolicyRegistryError as exc:
        raise LoopContractError(f"policy resolution failed: {exc}") from exc
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

    objective_ref = value.get("objective_ref")
    if objective_ref is not None:
        if not isinstance(objective_ref, Mapping):
            errors.append("objective_ref must be an object")
        else:
            required_text("objective_ref.objective_id")
            required_text("objective_ref.stage_key")

    user_facing_delivery = value.get("user_facing_delivery")
    routing_task_type = str(
        (value.get("routing") or {}).get("task_type")
        if isinstance(value.get("routing"), Mapping)
        else ""
    ).strip()
    if (
        routing_task_type == "secondhand_commerce_group_status"
        and user_facing_delivery is None
    ):
        errors.append(
            "secondhand_commerce_group_status requires user_facing_delivery"
        )
    if user_facing_delivery is not None:
        if not isinstance(user_facing_delivery, Mapping):
            errors.append("user_facing_delivery must be an object")
        else:
            if user_facing_delivery.get("required") is not True:
                errors.append("user_facing_delivery.required must be true")
            if user_facing_delivery.get("kind") != "commerce_group_status":
                errors.append(
                    "user_facing_delivery.kind must be commerce_group_status"
                )
            if user_facing_delivery.get("delivery") != "inline_only":
                errors.append(
                    "user_facing_delivery.delivery must be inline_only"
                )
            required_list("user_facing_delivery.subject_keys")

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
