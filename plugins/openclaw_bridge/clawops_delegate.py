"""Grace-only compiled delegation entry point for ClawOps."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from gateway.session_context import (
    get_session_env,
    record_cron_functional_error,
)
from hermes_cli import kanban_db as kb
from proactive.grace_task_compiler import compile_and_delegate
from proactive.hubops_routing import (
    normalize_clawops_task_type,
    registered_worker_task_types,
    resolved_route_binding,
    route_requires_owner_approval,
    route_clawops_objective,
)
from proactive.loop_contract import (
    browser_readonly_marketplace_fallback_listing_id,
    canonicalize_name_bound_facebook_crosspost_targets,
    canonical_marketplace_readonly_delegate_args,
    canonical_marketplace_readonly_sections,
    contract_fingerprint,
    exact_facebook_marketplace_listing_target_id,
    facebook_crosspost_inspection_listing_id,
    facebook_crosspost_target_ids,
    facebook_crosspost_target_names,
    marketplace_readonly_user_request_listing_id,
    validate_loop_contract,
)
from proactive.prompt_policy import approval_attempt_candidate
from proactive.thread_context_registry import (
    resolve_thread_context,
    resolve_thread_context_alias,
)


_LIST = {"type": "array", "items": {"type": "string"}, "minItems": 1}
_TASK_TYPES = [
    *registered_worker_task_types(),
    "secondhand_commerce_group_status",
]
_PROTECTED_FACEBOOK_PAGE_NAME_MARKERS = (
    "solobizai",
    "aibizweek",
    "一人公司商業誌",
)
_VALID_APPROVAL_TOKEN = re.compile(r"^[0-9a-f]{16}$")


def _is_safe_approval_message(message_text: str, approval_token: str) -> bool:
    """Accept a bound approval phrase with only harmless conversational framing.

    The token and approval verb remain exact and case-sensitive.  A short
    acknowledgement may precede them, and a courtesy may follow them, but
    newlines, quoted context, a second token, or any additional instruction
    fails closed.
    """
    token = str(approval_token or "").strip()
    if not token:
        return False
    raw_message = str(message_text or "")
    if any(
        character.isspace()
        and character not in {" ", "\t", "\u3000"}
        for character in raw_message
    ):
        return False
    horizontal_space = r"[ \t\u3000]"
    optional_space = horizontal_space + "*"
    prefix = (
        rf"(?:(?:好吧|好的?|可以|沒問題|收到){optional_space}"
        rf"(?:[，,、:：]{optional_space})?)?"
    )
    approval = rf"核准{horizontal_space}+{re.escape(token)}"
    courtesy = (
        rf"(?:{optional_space}(?:[，,、]{optional_space})?"
        rf"(?:謝謝|麻煩了))?"
    )
    ending = rf"{optional_space}[。.!！]?"
    return re.fullmatch(
        prefix + approval + courtesy + ending,
        raw_message.strip(),
    ) is not None


def _approval_token_candidate(message_text: str) -> str:
    return approval_attempt_candidate(message_text)


def _approval_child_request_instance_id(
    parent_request_instance_id: str,
    parent_contract_fingerprint: str,
) -> str:
    """Derive one immutable approval child from trusted parent provenance.

    The parent is gateway-derived from the authenticated inbound message and
    the fingerprint binds the exact compiled sub-contract.  Model input cannot
    choose either value, so one user message may safely request multiple
    independently approved contracts without sharing a delegation reservation.
    """
    return "gri_" + hashlib.sha256(
        (
            "approval-child:"
            f"{parent_request_instance_id.strip()}:"
            f"{parent_contract_fingerprint.strip()}"
        ).encode("utf-8")
    ).hexdigest()[:32]


def _requires_structured_facebook_crosspost(
    task_type: str,
    external_targets: list[str],
) -> bool:
    """Identify Marketplace-to-group publishing before issuing approval."""
    if normalize_clawops_task_type(str(task_type or "")) not in {
        "browser_publish",
        "facebook_marketplace_group_publish",
    }:
        return False
    target_text = " ".join(external_targets).casefold()
    has_marketplace_source = (
        "marketplace" in target_text
        or "市集" in target_text
        or "/marketplace/item/" in target_text
    )
    has_group_destination = (
        "group" in target_text
        or "社團" in target_text
        or "/groups/" in target_text
    )
    return (
        "facebook" in target_text
        and has_marketplace_source
        and has_group_destination
    )


def _requires_structured_facebook_page_post(
    task_type: str,
    external_targets: list[str],
) -> bool:
    """Identify one direct Facebook Page publication before approval."""
    normalized_task_type = normalize_clawops_task_type(str(task_type or ""))
    if normalized_task_type == "facebook_page_api_publish":
        return True
    if normalized_task_type != "browser_publish":
        return False
    for target in external_targets:
        text = str(target or "").strip()
        normalized = text.casefold()
        urls = [
            token
            for token in re.findall(
                r"https?://[^\s]+|"
                r"(?<![0-9A-Za-z.-])(?:[0-9A-Za-z-]+\.)*"
                r"facebook\.com\.?(?::[0-9]{1,5})?(?:/[^\s]*)?",
                text,
                flags=re.IGNORECASE,
            )
            if "facebook.com" in token.casefold()
        ]
        surrounding_text = text
        for raw_url in urls:
            surrounding_text = surrounding_text.replace(raw_url, " ")
        compact_surrounding = re.sub(
            r"[^0-9a-z]+",
            "",
            surrounding_text.casefold(),
        )
        if any(
            marker in compact_surrounding or marker in surrounding_text.casefold()
            for marker in _PROTECTED_FACEBOOK_PAGE_NAME_MARKERS
        ):
            return True
        if not urls:
            listing_ids, group_ids = facebook_crosspost_target_ids([text])
            group_names = facebook_crosspost_target_names([text])
            if listing_ids or group_ids or group_names:
                continue
        saw_facebook_url = False
        for raw_url in urls:
            candidate = raw_url
            try:
                parsed = urlsplit(
                    candidate
                    if "://" in candidate
                    else f"https://{candidate}"
                )
                parsed.port
            except ValueError:
                return True
            hostname = str(parsed.hostname or "").rstrip(".").casefold()
            parts = [part for part in parsed.path.split("/") if part]
            if not (
                hostname == "facebook.com"
                or hostname.endswith(".facebook.com")
            ):
                continue
            saw_facebook_url = True
            decoded_parts = [unquote(part).casefold() for part in parts]
            safe_path_parts = bool(
                decoded_parts
                and all(
                    part not in {".", ".."}
                    and re.fullmatch(r"[0-9a-z._-]+", part) is not None
                    for part in decoded_parts
                )
            )
            exact_group_route = bool(
                len(decoded_parts) >= 2
                and decoded_parts[0] == "groups"
                and safe_path_parts
            )
            exact_marketplace_route = bool(
                decoded_parts
                and decoded_parts[0] == "marketplace"
                and safe_path_parts
            )
            if exact_group_route or exact_marketplace_route:
                continue
            # Every other Facebook URL in a browser_publish target is Page-like
            # or ambiguous. It must fail closed through the structured Page
            # action validator rather than receive a generic approval.
            return True
        if saw_facebook_url:
            continue
        if urls:
            return True
        if any(
            marker in normalized
            for marker in ("粉專", "粉絲專頁", "粉絲頁")
        ):
            return True
        if "facebook" in normalized and re.search(
            r"(?:^|[\s/:：])(?:page|fan\s?page)(?:\s|$)", normalized
        ):
            return True
    return False


def _requires_structured_facebook_marketplace_price_update(
    task_type: str,
    external_targets: list[str],
    goal: dict[str, Any],
    scope: dict[str, Any],
) -> bool:
    """Fail closed when a generic browser contract describes a price update.

    This detector never creates mutation authority from prose.  It only stops
    a legacy/generic contract before approval so Grace must supply the exact
    listing, currency, and amount in the dedicated structured capability.
    """
    normalized_task_type = normalize_clawops_task_type(str(task_type or ""))
    if normalized_task_type == "facebook_marketplace_price_update":
        return True
    if normalized_task_type not in {"browser_ops", "browser_publish"}:
        return False
    target_text = " ".join(external_targets).casefold()
    has_marketplace_target = bool(
        any(
            exact_facebook_marketplace_listing_target_id(target) is not None
            for target in external_targets
        )
        or (
            ("facebook" in target_text or "臉書" in target_text)
            and ("marketplace" in target_text or "市集" in target_text)
        )
    )
    if not has_marketplace_target:
        return False
    intent_values = [
        goal.get("objective"),
        goal.get("deliverables"),
        scope.get("allowed"),
    ]
    intent_parts: list[str] = []
    for value in intent_values:
        if isinstance(value, list):
            intent_parts.extend(str(item or "") for item in value)
        elif value is not None:
            intent_parts.append(str(value))
    intent_text = " ".join(intent_parts).casefold()
    has_price = any(marker in intent_text for marker in ("price", "價格", "售價"))
    has_update = bool(
        re.search(r"\b(?:update|change|set)\b", intent_text)
        or any(
            marker in intent_text
            for marker in ("更新", "調整", "改為", "改成", "修改")
        )
    )
    return has_price and has_update


_GOAL = {
    "type": "object",
    "properties": {
        "objective": {
            "type": "string",
            "description": (
                "Post-approval task outcome for the delegated worker. Never "
                "use approval challenge, checkpoint, or token creation as the "
                "worker objective; clawops_delegate creates that artifact."
            ),
        },
        "deliverables": _LIST,
        "non_goals": _LIST,
    },
    "required": ["objective", "deliverables", "non_goals"],
    "additionalProperties": False,
}
_SCOPE = {
    "type": "object",
    "properties": {"allowed": _LIST, "forbidden": _LIST},
    "required": ["allowed", "forbidden"],
    "additionalProperties": False,
}
_VERIFICATION = {
    "type": "object",
    "properties": {
        "checks": _LIST,
        "evidence_required": _LIST,
        "acceptance_criteria": _LIST,
    },
    "required": ["checks", "evidence_required", "acceptance_criteria"],
    "additionalProperties": False,
}
_STOP_RULES = {
    "type": "object",
    "properties": {
        "success": _LIST,
        "blocked": _LIST,
        "no_progress": _LIST,
        "max_iterations": {"type": "integer", "minimum": 1, "maximum": 20},
        "max_runtime_seconds": {"type": "integer", "minimum": 60, "maximum": 14400},
    },
    "required": ["success", "blocked", "no_progress", "max_iterations", "max_runtime_seconds"],
    "additionalProperties": False,
}
_MEMORY = {
    "type": "object",
    "properties": {
        "working": _LIST,
        "promote_on_acceptance": _LIST,
    },
    "required": ["working", "promote_on_acceptance"],
    "additionalProperties": False,
}
_USER_FACING_DELIVERY = {
    "type": "object",
    "properties": {
        "required": {"type": "boolean", "const": True},
        "kind": {"type": "string", "enum": ["commerce_group_status"]},
        "delivery": {"type": "string", "enum": ["inline_only"]},
        "subject_keys": _LIST,
    },
    "required": ["required", "kind", "delivery", "subject_keys"],
    "additionalProperties": False,
}

CLAWOPS_DELEGATE_PARAMETERS = {
    "type": "object",
    "properties": {
        "original_request": {"type": "string", "description": "Audit copy only; never the worker instruction."},
        "grace_interpretation": {"type": "string", "description": "Grace's explicit understanding of KJ's intent."},
        "trigger": {"type": "string"},
        "goal": _GOAL,
        "scope": _SCOPE,
        "verification": _VERIFICATION,
        "stop_rules": _STOP_RULES,
        "memory": _MEMORY,
        "user_facing_delivery": _USER_FACING_DELIVERY,
        "task_type": {
            "type": "string",
            "enum": _TASK_TYPES,
            "description": "Choose one canonical task type from the active HubOps worker routes.",
        },
        "completion_mode": {
            "type": "string",
            "enum": ["terminal", "intermediate"],
            "description": (
                "terminal only when this contract's acceptance satisfies the "
                "complete user outcome; intermediate when another stage, "
                "approval checkpoint, or external action remains."
            ),
        },
        "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "approved": {"type": "boolean"},
        "approval_token": {
            "type": "string",
            "description": (
                "One-time token returned by a prior approval_required result. "
                "KJ must confirm it in a fresh authenticated message."
            ),
        },
        "external_targets": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": (
                "Exact external platforms or destinations affected by a "
                "controlled external action; required when approval is needed."
            ),
        },
        "facebook_crosspost": {
            "type": "object",
            "properties": {
                "transport": {
                    "type": "string",
                    "const": "browser",
                    "description": (
                        "Facebook Groups API was removed; Marketplace group "
                        "distribution is controlled-browser only."
                    ),
                },
                "marketplace_listing_id": {
                    "type": "string",
                    "pattern": "^[0-9]+$",
                    "description": (
                        "Exact existing Facebook Marketplace listing id."
                    ),
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[0-9]+$"},
                    "minItems": 1,
                    "uniqueItems": True,
                    "description": (
                        "Exact Facebook group ids selected through List in "
                        "more places."
                    ),
                },
                "group_names": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 20,
                    "uniqueItems": True,
                    "description": (
                        "Exact full Facebook group display names when saved "
                        "read-only evidence does not expose numeric group ids."
                    ),
                },
            },
            "required": ["marketplace_listing_id"],
            "oneOf": [
                {
                    "required": ["group_ids"],
                    "not": {"required": ["group_names"]},
                },
                {
                    "required": ["group_names"],
                    "not": {"required": ["group_ids"]},
                },
            ],
            "additionalProperties": False,
            "description": (
                "Required for an existing Marketplace listing cross-post to "
                "Facebook groups. The approval fingerprint binds both the "
                "source listing and every destination group id or exact name. "
                "transport may only be browser; Graph API is unavailable."
            ),
        },
        "facebook_page_post": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "const": "create_post"},
                "page_url": {
                    "type": "string",
                    "pattern": "^https://www\\.facebook\\.com/[A-Za-z0-9.]+$",
                },
                "transport": {"type": "string", "const": "graph_api"},
                "message_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "image_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": ["action", "page_url"],
            "additionalProperties": False,
            "description": (
                "Required for creating one post on one exact Facebook Page. "
                "For facebook_page_api_publish, transport must be graph_api "
                "and both exact payload SHA-256 values are required. The "
                "approval fingerprint binds the destination and payload."
            ),
        },
        "facebook_marketplace_price_update": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "const": "update_price"},
                "transport": {"type": "string", "const": "browser"},
                "marketplace_listing_id": {
                    "type": "string",
                    "pattern": "^[0-9]+$",
                },
                "currency": {"type": "string", "const": "TWD"},
                "price_twd": {"type": "integer", "minimum": 1},
            },
            "required": [
                "action", "transport", "marketplace_listing_id",
                "currency", "price_twd",
            ],
            "additionalProperties": False,
            "description": (
                "Required for changing only the price of one existing "
                "Facebook Marketplace listing. Supplying this exact object "
                "canonicalizes task_type and external_targets before the "
                "approval fingerprint is computed."
            ),
        },
        "request_instance_id": {
            "type": "string",
            "description": (
                "Stable opaque request/run instance. Normally derived by the "
                "gateway; required only when a scheduled caller has no message id."
            ),
        },
        "context_alias": {
            "type": "string",
            "description": "Required only for trusted scheduled jobs; ignored for chat lanes.",
        },
        "origin_callback_review_id": {
            "type": "string",
            "description": "Required for a safe continuation created inside a Grace callback.",
        },
        "origin_callback_event_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Active callback event paired with origin_callback_review_id.",
        },
        "origin_callback_board": {
            "type": "string",
            "description": (
                "Originating Kanban board for a callback continuation. Required "
                "with callback ids on a fresh approval turn."
            ),
        },
    },
    "required": [
        "original_request", "grace_interpretation", "trigger", "goal", "scope",
        "verification", "stop_rules", "memory",
        "task_type", "completion_mode", "risk_level", "approved",
    ],
    "additionalProperties": False,
}

CLAWOPS_DELEGATE_SCHEMA = {
    "description": (
        "After Grace has fully understood an execution request, delegate one complete "
        "canonical nested Loop Contract to ClawOps. Never call with an empty object. "
        "For every fresh authenticated execution request, call this tool again even "
        "when an equivalent contract was rejected in an earlier message: prior "
        "validation output is historical and cannot establish the current route or "
        "schema result."
    ),
    "parameters": CLAWOPS_DELEGATE_PARAMETERS,
}

GRACE_CALLBACK_OUTCOME_PARAMETERS = {
    "type": "object",
    "properties": {
        "review_task_id": {"type": "string"},
        "event_id": {"type": "integer", "minimum": 1},
        "outcome_kind": {
            "type": "string",
            "enum": [
                "closed", "continued", "approval_blocked",
                "decision_blocked", "capability_blocked",
                "evidence_delivered",
            ],
        },
        "payload": {
            "type": "object",
            "description": (
                "closed: summary; continued: delegation_id, execution_task_id, "
                "review_task_id; approval_blocked: action, platform, scope, "
                "exact_question; decision_blocked: decision, exact_question, "
                "and optional options; capability_blocked: capability_key, "
                "summary, retry_after; evidence_delivered: summary"
            ),
        },
    },
    "required": ["review_task_id", "event_id", "outcome_kind", "payload"],
    "additionalProperties": False,
}

GRACE_CALLBACK_OUTCOME_SCHEMA = {
    "description": (
        "Record the durable postcondition for an active internal Grace Loop "
        "callback. Required before that callback can be marked delivered."
    ),
    "parameters": GRACE_CALLBACK_OUTCOME_PARAMETERS,
}

CLAWOPS_FINALIZE_SAVED_EVIDENCE_PARAMETERS = {
    "type": "object",
    "properties": {
        "execution_task_id": {
            "type": "string",
            "pattern": "^t_[0-9a-f]{8}$",
            "description": (
                "Exact existing blocked ClawOps execution task whose durable "
                "commerce evidence must be finalized without browser access."
            ),
        },
        "board": {
            "type": "string",
            "description": "Exact durable board; defaults to default.",
        },
    },
    "required": ["execution_task_id"],
    "additionalProperties": False,
}

CLAWOPS_FINALIZE_SAVED_EVIDENCE_SCHEMA = {
    "description": (
        "Resume the same schema-blocked commerce execution/review pair from "
        "its saved evidence only. This never creates a new task or opens a browser."
    ),
    "parameters": CLAWOPS_FINALIZE_SAVED_EVIDENCE_PARAMETERS,
}


def _canonical_sections(args: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Accept the canonical nested contract and preserve legacy callers during rollout."""
    targets = args.get("external_targets")
    delivery = args.get("user_facing_delivery")
    requested_task_type = str(args.get("task_type") or "").strip()
    legacy_readonly_listing_id = (
        browser_readonly_marketplace_fallback_listing_id(args)
        if requested_task_type == "browser_readonly"
        else None
    )
    if (
        (
            requested_task_type in {
                "secondhand_commerce_group_status",
                "facebook_marketplace_readonly",
            }
            or legacy_readonly_listing_id is not None
        )
        and isinstance(targets, list)
        and len(targets) == 1
        and (
            delivery is None
            or (
                isinstance(delivery, dict)
                and delivery.get("required") is True
                and delivery.get("kind") == "commerce_group_status"
                and delivery.get("delivery") == "inline_only"
            )
        )
    ):
        listing_id = (
            exact_facebook_marketplace_listing_target_id(targets[0])
            or legacy_readonly_listing_id
            or browser_readonly_marketplace_fallback_listing_id(args)
        )
        if listing_id is not None:
            canonical = canonical_marketplace_readonly_sections(listing_id)
            args.update(canonical)
            # Keep the semantic task type in the Loop Contract. HubOps will
            # still resolve it to the dedicated facebook_marketplace_readonly
            # worker profile, but the contract validator, browser guard, and
            # callback layer now share one canonical authority vocabulary.
            args["task_type"] = "secondhand_commerce_group_status"
            args["external_targets"] = [
                f"Facebook Marketplace listing ID {listing_id}",
            ]
            if delivery is None:
                args["user_facing_delivery"] = {
                    "required": True,
                    "kind": "commerce_group_status",
                    "delivery": "inline_only",
                    "subject_keys": [f"facebook_marketplace:{listing_id}"],
                }
            else:
                supplied_subjects = delivery.get("subject_keys")
                canonical_subject = f"facebook_marketplace:{listing_id}"
                if (
                    isinstance(supplied_subjects, list)
                    and len(supplied_subjects) == 1
                    and (
                        supplied_subjects[0] == canonical_subject
                        or exact_facebook_marketplace_listing_target_id(
                            supplied_subjects[0]
                        ) == listing_id
                    )
                ):
                    # Grace callback turns may copy the canonical, human-
                    # readable external target into subject_keys.  Both
                    # fields prove the same exact listing, but the durable
                    # delivery ledger requires its namespaced subject key.
                    # Normalize only an exact same-listing alias; preserve a
                    # conflicting subject so Loop Contract validation still
                    # rejects it instead of silently broadening authority.
                    normalized_delivery = dict(delivery)
                    normalized_delivery["subject_keys"] = [canonical_subject]
                    args["user_facing_delivery"] = normalized_delivery
    scope = args.get("scope")
    if isinstance(scope, dict):
        # Some providers occasionally place the following canonical siblings
        # inside ``scope`` even though the tool schema declares them at the
        # top level.  Lift only these exact keys, and only when the top-level
        # value is absent, so validation and fingerprinting remain strict.
        normalized_scope = dict(scope)
        for key in ("trigger", "verification", "stop_rules", "task_type"):
            if key not in args and key in normalized_scope:
                args[key] = normalized_scope.pop(key)
        args["scope"] = normalized_scope
    if all(isinstance(args.get(key), dict) for key in ("goal", "scope", "verification", "stop_rules", "memory")):
        return (
            dict(args["goal"]),
            dict(args["scope"]),
            dict(args["verification"]),
            dict(args["stop_rules"]),
            dict(args["memory"]),
        )
    return (
        {
            "objective": str(args.get("objective") or "").strip(),
            "deliverables": list(args.get("deliverables") or []),
            "non_goals": list(args.get("non_goals") or []),
        },
        {
            "allowed": list(args.get("scope_allowed") or []),
            "forbidden": list(args.get("scope_forbidden") or []),
        },
        {
            "checks": list(args.get("verification_checks") or []),
            "evidence_required": list(args.get("evidence_required") or []),
            "acceptance_criteria": list(args.get("acceptance_criteria") or []),
        },
        {
            "success": list(args.get("stop_success") or []),
            "blocked": list(args.get("stop_blocked") or []),
            "no_progress": list(args.get("stop_no_progress") or []),
            "max_iterations": args.get("max_iterations"),
            "max_runtime_seconds": args.get("max_runtime_seconds"),
        },
        {
            "working": list(args.get("working_memory") or []),
            "promote_on_acceptance": list(args.get("promote_on_acceptance") or []),
        },
    )


def _saved_commerce_candidate_request(
    args: dict[str, Any],
) -> tuple[str, str] | None:
    """Return listing/category for an internal saved-evidence shortlist."""
    if str(args.get("task_type") or "").strip() != (
        "secondhand_commerce_group_status"
    ):
        return None
    text = json.dumps(args, ensure_ascii=False, sort_keys=True).casefold()
    if not any(
        marker in text
        for marker in ("候選", "篩選", "排除", "shortlist", "candidate")
    ) or not any(
        marker in text
        for marker in (
            "保存證據", "已保存", "preserved", "saved evidence",
            "27 named", "27 筆", "已核實",
        )
    ):
        return None
    targets = args.get("external_targets")
    if not isinstance(targets, list) or len(targets) != 1:
        return None
    listing_id = exact_facebook_marketplace_listing_target_id(targets[0])
    if listing_id is None:
        return None
    if "carimali" in text or "咖啡" in text:
        category = "coffee_equipment"
    elif "celestron" in text or "望遠鏡" in text or "天文" in text:
        category = "telescope"
    elif "冷氣" in text or "air conditioner" in text:
        category = "air_conditioner"
    elif "kolin" in text or "家電" in text:
        category = "home_appliance"
    else:
        return None
    return listing_id, category


def _resolve_callback_approval_board(
    *,
    review_task_id: str,
    event_id: int,
    platform: str,
    chat_id: str,
    thread_id: str,
    session_id: str,
) -> str:
    """Resolve a fresh approval checkpoint from durable rows, not model input."""
    matches: list[str] = []
    for metadata in kb.list_boards(include_archived=False):
        slug = str(metadata.get("slug") or kb.DEFAULT_BOARD)
        try:
            with kb.connect_closing(board=slug) as conn:
                kb.validate_delivered_grace_callback_approval_origin(
                    conn,
                    review_task_id=review_task_id,
                    event_id=event_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    session_id=session_id,
                )
        except (ValueError, OSError):
            continue
        matches.append(slug)
    if not matches:
        raise ValueError(
            "Fresh callback approval origin is not valid on any durable board."
        )
    if len(matches) > 1:
        raise ValueError(
            "Fresh callback approval origin resolved to multiple durable boards."
        )
    return matches[0]


def _resolve_approval_challenge(token: str) -> tuple[str, dict[str, Any]]:
    """Resolve a one-time token to exactly one durable board and challenge."""
    if _VALID_APPROVAL_TOKEN.fullmatch(str(token or "").strip()) is None:
        raise ValueError(
            "Approval token must be exactly 16 lowercase hexadecimal characters."
        )
    matches: list[tuple[str, dict[str, Any]]] = []
    for metadata in kb.list_boards(include_archived=False):
        slug = str(metadata.get("slug") or kb.DEFAULT_BOARD)
        try:
            with kb.connect_closing(board=slug) as conn:
                challenge = kb.get_grace_approval_challenge(conn, token)
        except OSError:
            continue
        if challenge is not None:
            matches.append((slug, challenge))
    if len(matches) != 1:
        raise ValueError(
            "Approval token must resolve to exactly one durable board."
        )
    return matches[0]


def recover_clawops_approval_args(token: str) -> dict[str, Any] | None:
    """Recover the exact delegate arguments persisted with a durable token."""
    _board, challenge = _resolve_approval_challenge(token)
    raw_args = str(challenge.get("delegation_args") or "").strip()
    if not raw_args:
        return None
    try:
        recovered = json.loads(raw_args)
    except (TypeError, ValueError):
        return None
    if not isinstance(recovered, dict):
        return None
    recovered.pop("approval_token", None)
    recovered.pop("_approval_refresh_token", None)
    recovered["approved"] = False
    return recovered


def _queued_delegation_replay(
    delegation: dict[str, Any] | None,
    *,
    project: str,
    topic_name: str,
    board: str | None,
) -> str | None:
    """Return the original queued task pair for an exact idempotent replay."""
    if (
        not delegation
        or delegation.get("state") != "queued"
        or not delegation.get("execution_task_id")
        or not delegation.get("review_task_id")
    ):
        return None
    with kb.connect_closing(board=board) as conn:
        execution = kb.get_task(conn, str(delegation["execution_task_id"]))
        review = kb.get_task(conn, str(delegation["review_task_id"]))
        subscriptions = kb.list_notify_subs(
            conn, str(delegation["execution_task_id"]),
        )
    if execution is None or review is None:
        raise RuntimeError(
            "Queued Grace delegation references missing task cards."
        )
    return json.dumps(
        {
            "status": "queued",
            "project": project,
            "topic_name": topic_name,
            "assigned_agent": execution.assignee,
            "delegation_id": str(delegation["delegation_id"]),
            "execution_task_id": str(delegation["execution_task_id"]),
            "grace_review_task_id": str(delegation["review_task_id"]),
            "progress_subscription": bool(subscriptions),
            "idempotent_replay": True,
            "routing": (
                "KJ -> Grace understanding -> Loop Contract -> "
                "ClawOps -> Grace review -> KJ"
            ),
        },
        ensure_ascii=False,
    )


def handle_clawops_delegate(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
    """Create execution + Grace-review cards only after a complete contract exists."""
    args = dict(args or {})
    approval_refresh_token = str(
        args.pop("_approval_refresh_token", "") or ""
    ).strip()
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    session_platform = platform
    session_source = get_session_env("HERMES_SESSION_SOURCE", "").strip().lower()
    scheduled_turn = session_source == "cron" or session_platform == "cron"
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "")
    user_id = get_session_env("HERMES_SESSION_USER_ID", "")
    session_key = get_session_env("HERMES_SESSION_KEY", "")
    session_id = get_session_env("HERMES_SESSION_ID", "")
    trusted_cron_session_id = session_id if session_source == "cron" else ""
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "")
    message_text = get_session_env("HERMES_SESSION_MESSAGE_TEXT", "")
    session_internal = (
        get_session_env("HERMES_SESSION_INTERNAL", "").strip().lower() == "true"
    )
    internal_kind = get_session_env(
        "HERMES_SESSION_INTERNAL_KIND", ""
    ).strip().lower()
    # Restart recovery travels through the gateway as an internal event so it
    # can resume silently.  It still represents the authenticated user message
    # that was interrupted; it is not a Grace review callback.
    restart_resume_turn = session_internal and internal_kind == "restart_resume"
    internal_turn = session_internal and not restart_resume_turn
    trusted_readonly_listing_id = (
        None
        if internal_turn
        else marketplace_readonly_user_request_listing_id(message_text)
    )
    if trusted_readonly_listing_id is not None:
        args = {
            "original_request": message_text.strip(),
            **canonical_marketplace_readonly_delegate_args(
                trusted_readonly_listing_id
            ),
        }
        approval_refresh_token = ""
    callback_lease_owner = get_session_env(
        "HERMES_GRACE_CALLBACK_LEASE_OWNER", "",
    ).strip()
    trusted_callback_board = get_session_env(
        "HERMES_GRACE_CALLBACK_BOARD", "",
    ).strip()
    requested_callback_board = str(
        args.get("origin_callback_board") or ""
    ).strip()
    origin_review_id = str(
        args.get("origin_callback_review_id") or ""
    ).strip()
    origin_event_raw = args.get("origin_callback_event_id")
    origin_event_id = (
        int(origin_event_raw) if origin_event_raw is not None else None
    )
    approval_token = str(args.get("approval_token") or "").strip()
    if internal_turn:
        if (
            requested_callback_board
            and requested_callback_board != (trusted_callback_board or "default")
        ):
            return json.dumps(
                {
                    "status": "rejected",
                    "reason": "Callback board does not match trusted internal context.",
                    "task_created": False,
                },
                ensure_ascii=False,
            )
        board = trusted_callback_board or None
    else:
        board = None
    owner_user_id = get_session_env("HERMES_SESSION_OWNER_USER_ID", "").strip()
    notifier_profile = get_session_env("HERMES_PROFILE", "").strip()
    if not notifier_profile:
        session_parts = session_key.split(":")
        if len(session_parts) >= 2 and session_parts[0] == "agent":
            notifier_profile = (
                "default" if session_parts[1] == "main" else session_parts[1]
            )
    if not notifier_profile:
        notifier_profile = "default"
    try:
        approval_candidate = _approval_token_candidate(message_text)
        if session_internal and (approval_token or approval_refresh_token):
            raise ValueError(
                "An internal gateway continuation cannot consume an approval token."
            )
        if (
            approval_candidate
            and not approval_token
            and not approval_refresh_token
            and not internal_turn
        ):
            raise ValueError(
                "A token-shaped approval message must be validated with its "
                "approval_token; it cannot be treated as a fresh request."
            )
        approval_challenge: dict[str, Any] | None = None
        approval_board = ""
        challenge_lookup_token = approval_token or approval_refresh_token
        if challenge_lookup_token and not internal_turn:
            approval_board, approval_challenge = _resolve_approval_challenge(
                challenge_lookup_token,
            )
            if (
                approval_challenge.get("platform") != platform
                or approval_challenge.get("chat_id") != chat_id
                or approval_challenge.get("thread_id") != thread_id
                or approval_challenge.get("session_key") != session_key
            ):
                raise ValueError(
                    "Approval token is bound to another conversation lane: "
                    f"{approval_challenge.get('platform')}/"
                    f"{approval_challenge.get('chat_id')}/thread/"
                    f"{approval_challenge.get('thread_id')}."
                )
            challenge_review_id = str(
                approval_challenge.get("origin_review_task_id") or ""
            ).strip()
            challenge_event_raw = approval_challenge.get("origin_event_id")
            challenge_event_id = (
                int(challenge_event_raw)
                if challenge_event_raw is not None
                else None
            )
            if (
                origin_review_id
                and origin_review_id != challenge_review_id
            ) or (
                origin_event_id is not None
                and origin_event_id != challenge_event_id
            ):
                raise ValueError(
                    "Approval token is bound to another callback origin."
                )
            if (
                requested_callback_board
                and requested_callback_board != approval_board
            ):
                raise ValueError(
                    "Approval token is bound to another Kanban board."
                )
            origin_review_id = challenge_review_id
            origin_event_id = challenge_event_id
            if challenge_review_id and challenge_event_id is not None:
                requested_callback_board = approval_board
            board = (
                None if approval_board == kb.DEFAULT_BOARD else approval_board
            )
        if platform and platform not in {"cron"}:
            raw_goal = args.get("goal")
            goal_objective = (
                str(raw_goal.get("objective") or "")
                if isinstance(raw_goal, dict)
                else ""
            )
            context = resolve_thread_context(
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                auto_create=True,
                work_hint=" ".join(
                    value
                    for value in (
                        goal_objective,
                        str(args.get("grace_interpretation") or ""),
                        str(args.get("trigger") or ""),
                    )
                    if value.strip()
                ),
            )
        else:
            context = resolve_thread_context_alias(str(args.get("context_alias") or ""))
            platform = str(context.get("platform") or "")
            chat_id = str(context.get("chat_id") or "")
            thread_id = str(context.get("thread_id") or "")
        project = str(context["project"])
        topic_name = str(context["topic_name"])
        namespace = str(context.get("memory_namespace") or f"topic:{chat_id}:{thread_id}/{project}")
        if not internal_turn and not scheduled_turn:
            from proactive.topic_placement_guard import (
                detect_topic_mismatch,
                mismatch_warning,
                register_pending_topic_override,
                topic_override_confirmed,
            )

            raw_goal = args.get("goal")
            goal_objective = (
                str(raw_goal.get("objective") or "")
                if isinstance(raw_goal, dict)
                else str(args.get("objective") or "")
            )
            placement_text = " ".join(
                value
                for value in (
                    str(args.get("original_request") or ""),
                    str(args.get("grace_interpretation") or ""),
                    goal_objective,
                )
                if value.strip()
            )
            mismatch = detect_topic_mismatch(
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                text=placement_text,
            )
            if mismatch is not None and not topic_override_confirmed(
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                message_id=message_id,
            ):
                override_registered = register_pending_topic_override(
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    user_id=user_id,
                    message_id=message_id,
                    original_text=message_text,
                    mismatch=mismatch,
                )
                return json.dumps(
                    {
                        "status": "rejected",
                        "reason": "topic_mismatch",
                        "task_created": False,
                        "current_topic": {
                            "name": mismatch.current_topic_name,
                            "thread_id": mismatch.current_thread_id,
                        },
                        "suggested_topic": {
                            "name": mismatch.suggested_topic_name,
                            "thread_id": mismatch.suggested_thread_id,
                        },
                        "message": mismatch_warning(
                            mismatch,
                            override_available=override_registered,
                        ),
                    },
                    ensure_ascii=False,
                )
        scheduled_identity = (
            {
                "platform": platform,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "project": project,
                "topic_name": topic_name,
                "memory_namespace": namespace,
            }
            if scheduled_turn
            else {}
        )
        if (
            not internal_turn
            and origin_review_id
            and origin_event_id is not None
        ):
            resolved_board = approval_board
            if approval_challenge is None:
                resolved_board = _resolve_callback_approval_board(
                    review_task_id=origin_review_id,
                    event_id=origin_event_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    session_id=session_id,
                )
            if (
                requested_callback_board
                and requested_callback_board != resolved_board
            ):
                raise ValueError(
                    "Callback board does not match the durable approval checkpoint."
                )
            board = None if resolved_board == kb.DEFAULT_BOARD else resolved_board
        elif requested_callback_board:
            board = requested_callback_board
        if scheduled_turn:
            session_key = session_key or f"cron:{project}"
            session_id = session_id or f"cron:{project}"
        candidate_request = (
            None
            if internal_turn or scheduled_turn
            else _saved_commerce_candidate_request(args)
        )
        if candidate_request is not None:
            candidate_listing_id, product_category = candidate_request
            with kb.connect_closing(board=board) as conn:
                candidate_result = kb.filter_saved_commerce_candidates(
                    conn,
                    listing_id=candidate_listing_id,
                    product_category=product_category,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    user_id=user_id,
                )
            return json.dumps(
                {
                    "status": "saved_candidates_ready",
                    **candidate_result,
                    "instruction": (
                        "Present recommended and optional destinations inline "
                        "by name and current status. Do not delegate, open a "
                        "browser, request approval, or publish."
                    ),
                },
                ensure_ascii=False,
            )
        goal, scope, verification, stop_rules, memory = _canonical_sections(args)
        task_type = normalize_clawops_task_type(
            str(args.get("task_type") or "")
        )
        if (
            task_type == "secondhand_commerce_group_status"
            and not isinstance(args.get("user_facing_delivery"), dict)
        ):
            raise ValueError(
                "secondhand_commerce_group_status requires "
                "user_facing_delivery"
            )
        risk_level = str(args.get("risk_level") or "")
        external_targets = [
            str(item).strip()
            for item in list(args.get("external_targets") or [])
            if str(item).strip()
        ]
        raw_facebook_crosspost = args.get("facebook_crosspost")
        facebook_crosspost = (
            json.loads(json.dumps(raw_facebook_crosspost))
            if isinstance(raw_facebook_crosspost, dict)
            else None
        )
        raw_facebook_page_post = args.get("facebook_page_post")
        facebook_page_post = (
            json.loads(json.dumps(raw_facebook_page_post))
            if isinstance(raw_facebook_page_post, dict)
            else None
        )
        raw_marketplace_price_update = args.get(
            "facebook_marketplace_price_update"
        )
        marketplace_price_update = (
            json.loads(json.dumps(raw_marketplace_price_update))
            if isinstance(raw_marketplace_price_update, dict)
            else None
        )
        if marketplace_price_update is not None:
            listing_id = str(
                marketplace_price_update.get("marketplace_listing_id") or ""
            ).strip()
            expected_target = f"Facebook Marketplace item {listing_id}"
            if external_targets and (
                len(external_targets) != 1
                or exact_facebook_marketplace_listing_target_id(
                    external_targets[0]
                ) != listing_id
            ):
                raise ValueError(
                    "facebook_marketplace_price_update conflicts with "
                    "external_targets; provide only its exact Marketplace item."
                )
            task_type = "facebook_marketplace_price_update"
            external_targets = [expected_target]
        external_targets = canonicalize_name_bound_facebook_crosspost_targets(
            external_targets,
            facebook_crosspost,
        )
        if _requires_structured_facebook_crosspost(
            task_type,
            external_targets,
        ) and facebook_crosspost is None:
            raise ValueError(
                "Facebook Marketplace group cross-post approval requires "
                "facebook_crosspost.marketplace_listing_id and exactly one "
                "of facebook_crosspost.group_ids or exact group_names before "
                "an approval token can be issued."
            )
        if _requires_structured_facebook_marketplace_price_update(
            task_type,
            external_targets,
            goal,
            scope,
        ) and marketplace_price_update is None:
            raise ValueError(
                "Facebook Marketplace price updates require "
                "facebook_marketplace_price_update with action=update_price, "
                "transport=browser, the exact marketplace_listing_id, "
                "currency=TWD, and positive integer price_twd before an "
                "approval token can be issued."
            )
        if (
            task_type == "facebook_marketplace_group_publish"
            and (
                not isinstance(facebook_crosspost, dict)
                or not facebook_crosspost
            )
        ):
            raise ValueError(
                "facebook_marketplace_group_publish requires a nonempty "
                "facebook_crosspost contract."
            )
        if _requires_structured_facebook_page_post(
            task_type,
            external_targets,
        ) and facebook_page_post is None:
            raise ValueError(
                "Direct Facebook Page publication requires "
                "facebook_page_post.action=create_post, the exact canonical "
                "facebook_page_post.page_url, transport=graph_api, and immutable "
                "message_sha256/image_sha256 bindings before an approval token "
                "can be issued."
            )
        if (
            facebook_crosspost is not None
            and task_type != "facebook_marketplace_group_publish"
        ):
            raise ValueError(
                "facebook_crosspost is only valid for task_type="
                "facebook_marketplace_group_publish."
            )
        if (
            facebook_crosspost is not None
            and facebook_crosspost.get("transport") != "browser"
        ):
            raise ValueError(
                "facebook_crosspost transport must be browser because Meta "
                "removed publish_to_groups and the Groups API from all Graph "
                "API versions."
            )
        if facebook_page_post is not None:
            page_transport = str(
                facebook_page_post.get("transport") or "browser"
            ).strip()
            expected_page_task_type = (
                "facebook_page_api_publish"
                if page_transport == "graph_api"
                else "browser_publish"
            )
            if task_type != expected_page_task_type:
                raise ValueError(
                    "facebook_page_post transport="
                    f"{page_transport} requires task_type="
                    f"{expected_page_task_type}."
                )
        if (
            marketplace_price_update is not None
            and task_type != "facebook_marketplace_price_update"
        ):
            raise ValueError(
                "facebook_marketplace_price_update requires task_type="
                "facebook_marketplace_price_update."
            )
        supplied_request_instance = str(
            args.get("request_instance_id") or ""
        ).strip()
        chat_parent_request_instance_id = ""
        if approval_token or approval_refresh_token:
            if approval_challenge is None:
                with kb.connect_closing(board=board) as conn:
                    approval_challenge = kb.get_grace_approval_challenge(
                        conn, approval_token or approval_refresh_token,
                    )
            if approval_challenge is None:
                raise ValueError("Approval challenge was not found.")
            request_instance_id = str(
                approval_challenge.get("request_instance_id") or ""
            ).strip()
            if not request_instance_id:
                raise ValueError(
                    "Approval challenge predates request-instance binding; "
                    "request a new approval challenge."
                )
            if (
                supplied_request_instance
                and supplied_request_instance != request_instance_id
            ):
                raise ValueError(
                    "Approval token is bound to another request instance."
                )
        elif origin_review_id and origin_event_id is not None:
            request_instance_id = "gri_" + hashlib.sha256(
                (
                    f"callback:{board or 'default'}:"
                    f"{origin_review_id}:{origin_event_id}"
                ).encode("utf-8")
            ).hexdigest()[:32]
        elif not scheduled_turn and message_id:
            request_instance_id = "gri_" + hashlib.sha256(
                (
                    f"message:{session_platform}:{session_key}:{message_id}"
                ).encode("utf-8")
            ).hexdigest()[:32]
            chat_parent_request_instance_id = request_instance_id
        elif scheduled_turn and trusted_cron_session_id:
            scheduled_contract_discriminator = hashlib.sha256(
                json.dumps(
                    {
                        "identity": scheduled_identity,
                        "board": str(board or "default"),
                        "original_request": str(
                            args.get("original_request") or ""
                        ).strip(),
                        "grace_interpretation": str(
                            args.get("grace_interpretation") or ""
                        ).strip(),
                        "trigger": str(args.get("trigger") or "").strip(),
                        "goal": goal,
                        "scope": scope,
                        "verification": verification,
                        "stop_rules": stop_rules,
                        "memory": memory,
                        "task_type": task_type,
                        "risk_level": risk_level,
                        "completion_mode": str(
                            args.get("completion_mode") or ""
                        ).strip(),
                        "user_facing_delivery": args.get(
                            "user_facing_delivery"
                        ),
                        "external_targets": external_targets,
                        "facebook_crosspost": facebook_crosspost,
                        "facebook_page_post": facebook_page_post,
                        "facebook_marketplace_price_update": (
                            marketplace_price_update
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            request_instance_id = "gri_" + hashlib.sha256(
                (
                    f"cron:{trusted_cron_session_id}:"
                    f"{scheduled_contract_discriminator}"
                ).encode("utf-8")
            ).hexdigest()[:32]
            if (
                supplied_request_instance
                and supplied_request_instance != request_instance_id
            ):
                raise ValueError(
                    "Scheduled request_instance_id must match the trusted "
                    "scheduler-derived instance."
                )
        elif scheduled_turn and supplied_request_instance:
            # Compatibility for direct scheduled callers that predate the
            # trusted HERMES_SESSION_SOURCE/session-id binding.
            request_instance_id = supplied_request_instance
        else:
            raise ValueError(
                "Delegation requires a stable request_instance_id when no "
                "originating message or callback event exists."
            )
        contract = {
            "identity": {
                "platform": platform,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "topic_name": topic_name,
                "project": project,
                "board": str(board or "default"),
                "request_instance_id": request_instance_id,
                "requested_by": (
                    "trusted_scheduled_job"
                    if scheduled_turn
                    else "authenticated_user"
                ),
                "compiled_by": "Grace",
            },
            "original_request": str(args.get("original_request") or "").strip(),
            "grace_interpretation": str(args.get("grace_interpretation") or "").strip(),
            "trigger": str(args.get("trigger") or "").strip(),
            "goal": goal,
            "scope": scope,
            "verification": verification,
            "stop_rules": stop_rules,
            "memory": {
                "namespace": namespace,
                "working": list(memory.get("working") or []),
                "promote_on_acceptance": list(memory.get("promote_on_acceptance") or []),
            },
            "routing": {
                "task_type": task_type,
                "risk_level": risk_level,
            },
            "completion_mode": str(args.get("completion_mode") or "").strip(),
        }
        if isinstance(args.get("user_facing_delivery"), dict):
            contract["user_facing_delivery"] = dict(
                args["user_facing_delivery"]
            )
        if external_targets:
            contract["external_targets"] = external_targets
        if facebook_crosspost is not None:
            contract["facebook_crosspost"] = facebook_crosspost
        if facebook_page_post is not None:
            contract["facebook_page_post"] = facebook_page_post
        if marketplace_price_update is not None:
            contract["facebook_marketplace_price_update"] = (
                marketplace_price_update
            )
        preliminary_contract = validate_loop_contract(contract)
        preliminary_fingerprint = contract_fingerprint(preliminary_contract)
        routing_preview = route_clawops_objective(
            str(goal.get("objective") or ""),
            project=project,
            task_type=task_type,
            risk_level=risk_level,
            approved=True,
            contract_fingerprint=preliminary_fingerprint,
        )
        if routing_preview.get("status") != "routed":
            raise ValueError(
                str(
                    routing_preview.get("blocked_reason")
                    or "ClawOps routing is blocked."
                )
            )
        contract["routing"]["resolved"] = resolved_route_binding(routing_preview)
        normalized_contract = validate_loop_contract(contract)
        exact_fingerprint = contract_fingerprint(normalized_contract)
        readonly_marketplace_inspection = (
            facebook_crosspost_inspection_listing_id(normalized_contract)
            is not None
        )
        approval_needed = (
            (bool(external_targets) and not readonly_marketplace_inspection)
            or route_requires_owner_approval(routing_preview)
        )
        if (
            approval_needed
            and chat_parent_request_instance_id
            and not approval_token
            and not approval_refresh_token
        ):
            # A fresh authenticated message may explicitly contain multiple
            # independently approved sub-contracts.  Derive each child from
            # the gateway-owned message instance plus the exact parent-bound
            # contract.  Preserve a matching legacy root binding only so
            # pre-rollout challenges/delegations remain replayable.
            with kb.connect_closing(board=board) as conn:
                legacy_delegation = kb.get_grace_delegation(
                    conn,
                    contract_fingerprint=exact_fingerprint,
                )
                root_delegation = kb.get_grace_delegation_for_request_instance(
                    conn,
                    platform=platform,
                    session_key=session_key,
                    request_instance_id=chat_parent_request_instance_id,
                )
                legacy_challenge = (
                    kb.get_grace_approval_challenge_for_contract_instance(
                        conn,
                        contract_fingerprint=exact_fingerprint,
                        request_instance_id=chat_parent_request_instance_id,
                        platform=platform,
                        session_key=session_key,
                    )
                )
            keep_legacy_root = bool(
                legacy_delegation is not None
                or (
                    legacy_challenge is not None
                    and root_delegation is None
                )
            )
            if not keep_legacy_root:
                request_instance_id = _approval_child_request_instance_id(
                    chat_parent_request_instance_id,
                    exact_fingerprint,
                )
                contract["identity"]["request_instance_id"] = (
                    request_instance_id
                )
                normalized_contract = validate_loop_contract(contract)
                exact_fingerprint = contract_fingerprint(normalized_contract)
        if (
            supplied_request_instance
            and supplied_request_instance != request_instance_id
        ):
            if approval_token or approval_refresh_token:
                raise ValueError(
                    "Approval token is bound to another request instance."
                )
            raise ValueError(
                "Chat request_instance_id must match the authenticated "
                "message-derived instance."
            )
        approval_scope = list(scope.get("allowed") or [])
        approval_platform = "、".join(external_targets)
        approval_scope_json = json.dumps(
            approval_scope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if approval_token:
            if approval_challenge is None:
                raise ValueError("Approval challenge was not found.")
            if not _is_safe_approval_message(message_text, approval_token):
                raise ValueError(
                    "核准訊息只能包含此授權碼，可加「好的」「收到」等簡短"
                    f"禮貌語，但不可附帶其他指令：核准 {approval_token}"
                )
            if not approval_needed:
                raise ValueError(
                    "Approval token is bound to a controlled external-action "
                    "contract and cannot authorize a non-approval route."
                )
            if not message_id or not session_key or not session_id:
                raise ValueError(
                    "Approval token requires an authenticated user context "
                    "and durable session/message identifiers."
                )
            if not owner_user_id or not user_id or user_id != owner_user_id:
                raise ValueError(
                    "Approval token requires the authenticated configured owner."
                )
            expected_user_hash = hashlib.sha256(
                owner_user_id.encode("utf-8")
            ).hexdigest()
            challenge_state = str(
                approval_challenge.get("state") or ""
            ).strip()
            with kb.connect_closing(board=board) as conn:
                existing_approved_delegation = kb.get_grace_delegation(
                    conn, contract_fingerprint=exact_fingerprint,
                )
            is_exact_consumed_replay = (
                challenge_state == "consumed"
                and existing_approved_delegation is not None
                and existing_approved_delegation.get("challenge_token")
                == approval_token
            )
            if challenge_state == "pending":
                if int(approval_challenge.get("expires_at") or 0) <= int(
                    time.time()
                ):
                    raise ValueError(
                        "Approval token is expired and no longer valid."
                    )
            elif not is_exact_consumed_replay:
                raise ValueError(
                    "Approval token is expired, consumed, or no longer pending."
                )
            if (
                approval_challenge.get("contract_fingerprint")
                != exact_fingerprint
                or approval_challenge.get("platform") != platform
                or approval_challenge.get("chat_id") != chat_id
                or approval_challenge.get("thread_id") != thread_id
                or approval_challenge.get("session_key") != session_key
                or approval_challenge.get("session_id") != session_id
                or approval_challenge.get("user_id_sha256")
                != expected_user_hash
                or approval_challenge.get("approval_platform")
                != approval_platform
                or approval_challenge.get("approval_scope")
                != approval_scope_json
            ):
                raise ValueError(
                    "Approval token is bound to another contract, identity, "
                    "platform, or scope."
                )
            if (
                str(approval_challenge.get("requested_message_id") or "")
                == message_id
            ):
                raise ValueError(
                    "Approval token must be confirmed in a fresh authenticated "
                    "message."
                )
        elif approval_refresh_token:
            if approval_challenge is None:
                raise ValueError("Approval challenge was not found.")
            if not approval_needed:
                raise ValueError(
                    "Approval refresh is bound to a controlled external-action "
                    "contract and cannot authorize a non-approval route."
                )
            if not message_id or not session_key or not session_id:
                raise ValueError(
                    "Approval refresh requires an authenticated user context "
                    "and durable session/message identifiers."
                )
            if not owner_user_id or not user_id or user_id != owner_user_id:
                raise ValueError(
                    "Approval refresh requires the authenticated configured owner."
                )
            expected_user_hash = hashlib.sha256(
                owner_user_id.encode("utf-8")
            ).hexdigest()
            if (
                approval_challenge.get("state") != "pending"
                or int(approval_challenge.get("expires_at") or 0)
                > int(time.time())
            ):
                raise ValueError(
                    "Only an expired, still-pending approval challenge may be "
                    "refreshed."
                )
            if (
                approval_challenge.get("contract_fingerprint")
                != exact_fingerprint
                or approval_challenge.get("platform") != platform
                or approval_challenge.get("chat_id") != chat_id
                or approval_challenge.get("thread_id") != thread_id
                or approval_challenge.get("session_key") != session_key
                or approval_challenge.get("user_id_sha256")
                != expected_user_hash
                or approval_challenge.get("approval_platform")
                != approval_platform
                or approval_challenge.get("approval_scope")
                != approval_scope_json
            ):
                raise ValueError(
                    "Approval refresh is bound to another contract, identity, "
                    "platform, or scope."
                )
            if (
                str(approval_challenge.get("requested_message_id") or "")
                == message_id
            ):
                raise ValueError(
                    "Approval refresh requires a fresh authenticated message."
                )
        if internal_turn:
            if not origin_review_id or origin_event_id is None:
                raise ValueError(
                    "Internal continuation requires the active callback review "
                    "and event identifiers."
                )
            if not callback_lease_owner:
                raise ValueError(
                    "Internal continuation requires the trusted callback lease owner."
                )
            with kb.connect_closing(board=board) as conn:
                kb.rebind_active_grace_callback_session(
                    conn,
                    review_task_id=origin_review_id,
                    event_id=origin_event_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    session_id=session_id,
                    lease_owner=callback_lease_owner,
                )
                execution_blocker_origin = (
                    kb.is_grace_callback_execution_blocker_origin(
                        conn,
                        review_task_id=origin_review_id,
                        event_id=origin_event_id,
                        platform=platform,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        session_id=session_id,
                        lease_owner=callback_lease_owner,
                    )
                )
                if execution_blocker_origin:
                    approval_needed = True
                if approval_needed:
                    kb.validate_grace_callback_approval_origin(
                        conn,
                        review_task_id=origin_review_id,
                        event_id=origin_event_id,
                        platform=platform,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        session_id=session_id,
                        lease_owner=callback_lease_owner,
                    )
                    (
                        source_crosspost_listing_id,
                        source_crosspost_group_ids,
                        source_crosspost_group_names,
                    ) = kb.grace_callback_facebook_crosspost_scopes(
                        conn,
                        review_task_id=origin_review_id,
                        event_id=origin_event_id,
                    )
                    if (
                        source_crosspost_listing_id is not None
                        and (
                            source_crosspost_group_ids
                            or source_crosspost_group_names
                        )
                        and facebook_crosspost is None
                    ):
                        raise ValueError(
                            "Origin callback locks an exact Facebook cross-post "
                            "scope; facebook_crosspost cannot be omitted."
                        )
                    if facebook_crosspost is not None:
                        kb.validate_grace_callback_facebook_crosspost_scope(
                            conn,
                            review_task_id=origin_review_id,
                            event_id=origin_event_id,
                            listing_id=str(
                                facebook_crosspost.get(
                                    "marketplace_listing_id"
                                ) or ""
                            ),
                            group_ids=list(
                                facebook_crosspost.get("group_ids") or []
                            ),
                            group_names=list(
                                facebook_crosspost.get("group_names") or []
                            ),
                        )
                else:
                    kb.validate_accepted_grace_callback_origin(
                        conn,
                        review_task_id=origin_review_id,
                        event_id=origin_event_id,
                        platform=platform,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        session_id=session_id,
                        lease_owner=callback_lease_owner,
                    )
        elif (
            origin_review_id
            or origin_event_id is not None
            or requested_callback_board
        ):
            if (
                not origin_review_id
                or origin_event_id is None
                or not requested_callback_board
            ):
                raise ValueError(
                    "Fresh callback approval requires review id, event id, and board."
                )
            if approval_challenge is None:
                with kb.connect_closing(board=board) as conn:
                    kb.validate_delivered_grace_callback_approval_origin(
                        conn,
                        review_task_id=origin_review_id,
                        event_id=origin_event_id,
                        platform=platform,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        session_id=session_id,
                    )
        effective_approved = False
        approval_provenance: dict[str, Any] = {}
        if scheduled_turn and approval_needed:
            raise ValueError(
                "Scheduled jobs cannot authorize external actions with approved=true. "
                "A persisted owner approval bound to this exact contract is required."
            )
        if approval_needed and not external_targets:
            raise ValueError(
                "External-action delegation requires explicit external_targets."
            )
        with kb.connect_closing(board=board) as conn:
            existing_delegation = kb.get_grace_delegation(
                conn,
                contract_fingerprint=exact_fingerprint,
            )
            browser_blocker = kb.find_recent_commerce_browser_blocker(
                conn,
                normalized_contract,
            )
        existing_replay = _queued_delegation_replay(
            existing_delegation,
            project=project,
            topic_name=topic_name,
            board=board,
        )
        if browser_blocker is not None and existing_replay is None:
            return json.dumps(
                {
                    "status": "capability_blocked",
                    "task_created": False,
                    **browser_blocker,
                    "reason": (
                        "同一拍品最近已因受控 Facebook 瀏覽器不可讀而阻塞；"
                        "冷卻期間不再建立重複任務。"
                    ),
                },
                ensure_ascii=False,
            )
        if not scheduled_turn and approval_needed:
            approval_request_message_id = message_id
            if (
                internal_turn
                and not approval_request_message_id
                and origin_review_id
                and origin_event_id is not None
            ):
                approval_request_message_id = (
                    f"callback:{board or kb.DEFAULT_BOARD}:"
                    f"{origin_review_id}:{origin_event_id}"
                )
            if (
                not approval_request_message_id
                or not session_key
                or not session_id
            ):
                raise ValueError(
                    "External-action approval requires an authenticated user context "
                    "and durable session/message identifiers."
                )
            if not owner_user_id:
                raise ValueError(
                    "External-action approval requires an explicitly configured owner."
                )
            if not internal_turn and (not user_id or user_id != owner_user_id):
                raise ValueError(
                    "External-action approval requires the authenticated configured "
                    "owner."
                )
            user_id_sha256 = hashlib.sha256(
                owner_user_id.encode("utf-8")
            ).hexdigest()
            if internal_turn and approval_token:
                raise ValueError(
                    "An internal callback may create an external-action approval "
                    "challenge but cannot consume one. KJ must send the exact reply "
                    "in a fresh authenticated turn."
                )
            if not approval_token:
                with kb.connect_closing(board=board) as conn:
                    existing_delegation = kb.get_grace_delegation(
                        conn, contract_fingerprint=exact_fingerprint,
                    )
                replay = _queued_delegation_replay(
                    existing_delegation,
                    project=project,
                    topic_name=topic_name,
                    board=board,
                )
                if replay is not None:
                    if scheduled_turn:
                        record_cron_functional_error("")
                    return replay
                approval_replay_args = dict(args)
                approval_replay_args.pop("approval_token", None)
                approval_replay_args.pop("_approval_refresh_token", None)
                approval_replay_args["approved"] = False
                approval_replay_args_json = json.dumps(
                    approval_replay_args,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                with kb.connect_closing(board=board) as conn:
                    challenge = kb.create_grace_approval_challenge(
                        conn,
                        contract_fingerprint=exact_fingerprint,
                        request_instance_id=request_instance_id,
                        platform=platform,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        session_key=session_key,
                        session_id=session_id,
                        user_id_sha256=user_id_sha256,
                        requested_message_id=approval_request_message_id,
                        action_summary=str(goal.get("objective") or "").strip(),
                        approval_platform=approval_platform,
                        approval_scope=approval_scope_json,
                        delegation_args=approval_replay_args_json,
                        origin_review_task_id=origin_review_id,
                        origin_event_id=origin_event_id,
                        callback_lease_owner=(
                            callback_lease_owner if internal_turn else ""
                        ),
                    )
                token = str(challenge["token"])
                return json.dumps(
                    {
                        "status": "approval_required",
                        "task_created": False,
                        "approval_token": token,
                        "request_instance_id": request_instance_id,
                        "exact_reply": f"核准 {token}",
                        "reply_policy": (
                            "可加「好的」「收到」等簡短禮貌語；不可附帶其他指令"
                            "或改變動作、平台與範圍。"
                        ),
                        "expires_at": challenge["expires_at"],
                        "action": str(goal.get("objective") or "").strip(),
                        "platform": approval_platform,
                        "scope": approval_scope,
                        "reason": (
                            "External action approval must be confirmed in a "
                            "fresh authenticated KJ message for this exact contract."
                        ),
                    },
                    ensure_ascii=False,
                )
            with kb.connect_closing(board=board) as conn:
                challenge_row = kb.get_grace_approval_challenge(
                    conn, approval_token,
                )
                delegation = kb.reserve_grace_delegation(
                    conn,
                    contract_fingerprint=exact_fingerprint,
                    request_instance_id=request_instance_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    session_key=session_key,
                    session_id=session_id,
                    resolved_route=contract["routing"]["resolved"],
                    approval_required=True,
                    challenge_token=approval_token,
                    user_id_sha256=user_id_sha256,
                    approved_message_id=message_id,
                    origin_review_task_id=origin_review_id,
                    origin_event_id=origin_event_id,
                )
            if challenge_row is None:
                raise ValueError("Approval challenge was not found.")
            approval_provenance = {
                "source": "one_time_authenticated_owner_challenge",
                "platform": platform,
                "requested_message_id": challenge_row["requested_message_id"],
                "approved_message_id": delegation["approved_message_id"],
                "user_id_sha256": user_id_sha256,
                "internal": False,
                "challenge_token_sha256": hashlib.sha256(
                    approval_token.encode("utf-8")
                ).hexdigest(),
                "contract_fingerprint": exact_fingerprint,
                "scope_binding": "exact_loop_contract_fingerprint",
            }
            effective_approved = True
        else:
            with kb.connect_closing(board=board) as conn:
                delegation = kb.reserve_grace_delegation(
                    conn,
                    contract_fingerprint=exact_fingerprint,
                    request_instance_id=request_instance_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    session_key=session_key,
                    session_id=session_id,
                    resolved_route=contract["routing"]["resolved"],
                    approval_required=False,
                    origin_review_task_id=origin_review_id,
                    origin_event_id=origin_event_id,
                    callback_lease_owner=(
                        callback_lease_owner if internal_turn else ""
                    ),
                )
        if approval_provenance:
            contract["approval_provenance"] = approval_provenance
        replay = _queued_delegation_replay(
            delegation,
            project=project,
            topic_name=topic_name,
            board=board,
        )
        if replay is not None:
            if scheduled_turn:
                record_cron_functional_error("")
            return replay
        delegation_id = str(delegation["delegation_id"])
        build_owner = "builder_" + secrets.token_hex(12)
        with kb.connect_closing(board=board) as conn:
            if not kb.claim_grace_delegation_build(
                conn,
                delegation_id=delegation_id,
                build_owner=build_owner,
            ):
                raise RuntimeError(
                    "Grace delegation is already being built; retry the same "
                    "contract after the active builder lease completes."
                )
        try:
            result = compile_and_delegate(
                contract,
                context=context,
                task_type=task_type,
                risk_level=risk_level,
                approved=effective_approved,
                delegation_id=delegation_id,
                delegation_build_owner=build_owner,
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                session_key=session_key,
                session_id=session_id,
                message_id=message_id,
                notifier_profile=notifier_profile,
                board=board,
                callback_lease_owner=(
                    callback_lease_owner if internal_turn else ""
                ),
            )
        except Exception:
            with kb.connect_closing(board=board) as conn:
                kb.release_grace_delegation_build(
                    conn,
                    delegation_id=delegation_id,
                    build_owner=build_owner,
                )
            raise
    except (ValueError, TypeError, RuntimeError) as exc:
        reason = str(exc).strip() or type(exc).__name__
        if scheduled_turn:
            record_cron_functional_error(reason)
        return json.dumps(
            {"status": "rejected", "reason": reason, "task_created": False},
            ensure_ascii=False,
        )
    except Exception as exc:
        if scheduled_turn:
            record_cron_functional_error(
                str(exc).strip() or type(exc).__name__
            )
        raise
    if scheduled_turn:
        record_cron_functional_error("")
    return json.dumps(
        {
            "status": "queued",
            "project": result.project,
            "topic_name": result.topic_name,
            "assigned_agent": result.assignee,
            "delegation_id": str(delegation["delegation_id"]),
            "execution_task_id": result.execution_task_id,
            "grace_review_task_id": result.review_task_id,
            "progress_subscription": result.subscribed,
            "routing": "KJ -> Grace understanding -> Loop Contract -> ClawOps -> Grace review -> KJ",
        },
        ensure_ascii=False,
    )


def handle_clawops_finalize_saved_evidence(
    args: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> str:
    """Requeue one exact blocked commerce execution for evidence-only close."""
    values = dict(args or {})
    execution_task_id = str(values.get("execution_task_id") or "").strip()
    board = str(values.get("board") or kb.DEFAULT_BOARD).strip()
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "").strip()
    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    session_key = get_session_env("HERMES_SESSION_KEY", "").strip()
    session_id = get_session_env("HERMES_SESSION_ID", "").strip()
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip()
    owner_user_id = get_session_env(
        "HERMES_SESSION_OWNER_USER_ID", "",
    ).strip()
    session_internal = (
        get_session_env("HERMES_SESSION_INTERNAL", "").strip().lower()
        == "true"
    )
    try:
        if set(values) - {"execution_task_id", "board"}:
            raise ValueError(
                "Saved-evidence finalization accepts only execution_task_id "
                "and board."
            )
        if not re.fullmatch(r"t_[0-9a-f]{8}", execution_task_id):
            raise ValueError(
                "Saved-evidence finalization requires one exact execution task id."
            )
        if session_internal:
            raise ValueError(
                "Saved-evidence finalization must originate from a fresh "
                "authenticated user turn, not a callback continuation."
            )
        if not platform or not chat_id:
            raise ValueError(
                "Saved-evidence finalization requires an authenticated chat lane."
            )
        if not owner_user_id:
            raise ValueError(
                "Saved-evidence finalization requires one explicitly "
                "configured owner."
            )
        if user_id != owner_user_id:
            raise ValueError(
                "Saved-evidence finalization requires the authenticated "
                "configured owner."
            )
        with kb.connect_closing(board=board) as conn:
            result = kb.resume_blocked_commerce_finalization_from_saved_evidence(
                conn,
                execution_task_id=execution_task_id,
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
                session_id=session_id,
                message_id=message_id,
            )
    except (ValueError, TypeError, RuntimeError) as exc:
        return json.dumps(
            {
                "status": "rejected",
                "reason": str(exc).strip() or type(exc).__name__,
                "task_created": False,
                "browser_opened": False,
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "status": "finalization_queued",
            **result,
            "board": board,
            "task_created": False,
            "browser_opened": False,
            "instruction": (
                "The original execution task will finalize from durable "
                "evidence only; do not create another delegation."
            ),
        },
        ensure_ascii=False,
    )


def handle_grace_callback_outcome(
    args: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> str:
    """Persist a callback postcondition only from its active internal turn."""
    args = dict(args or {})
    try:
        if (
            get_session_env("HERMES_SESSION_INTERNAL", "")
            .strip()
            .lower()
            != "true"
        ):
            raise ValueError(
                "grace_callback_outcome is available only inside an internal callback."
            )
        callback_board = get_session_env(
            "HERMES_GRACE_CALLBACK_BOARD", "",
        ).strip()
        callback_lease_owner = get_session_env(
            "HERMES_GRACE_CALLBACK_LEASE_OWNER", "",
        ).strip()
        if not callback_lease_owner:
            raise ValueError("Internal callback lease owner is missing.")
        payload = dict(args.get("payload") or {})
        if str(args.get("outcome_kind") or "") == "approval_blocked":
            payload["board"] = callback_board or "default"
        with kb.connect_closing(board=callback_board or None) as conn:
            kb.rebind_active_grace_callback_session(
                conn,
                review_task_id=str(args.get("review_task_id") or ""),
                event_id=int(args.get("event_id") or 0),
                platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
                chat_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
                thread_id=get_session_env("HERMES_SESSION_THREAD_ID", ""),
                session_id=get_session_env("HERMES_SESSION_ID", ""),
                lease_owner=callback_lease_owner,
            )
            row = kb.record_grace_loop_callback_outcome(
                conn,
                review_task_id=str(args.get("review_task_id") or ""),
                event_id=int(args.get("event_id") or 0),
                platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
                chat_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
                thread_id=get_session_env("HERMES_SESSION_THREAD_ID", ""),
                session_id=get_session_env("HERMES_SESSION_ID", ""),
                lease_owner=callback_lease_owner,
                outcome_kind=str(args.get("outcome_kind") or ""),
                payload=payload,
            )
    except (ValueError, TypeError, RuntimeError) as exc:
        return json.dumps(
            {"status": "rejected", "reason": str(exc), "recorded": False},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "status": "recorded",
            "recorded": True,
            "review_task_id": row["review_task_id"],
            "event_id": row["outcome_event_id"],
            "outcome_kind": row["outcome_kind"],
        },
        ensure_ascii=False,
    )
