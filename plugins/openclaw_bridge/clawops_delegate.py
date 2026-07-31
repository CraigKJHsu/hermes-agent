"""Grace-only compiled delegation entry point for ClawOps."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any

from gateway.session_context import (
    get_session_env,
    record_cron_functional_error,
)
from hermes_cli import kanban_db as kb
from proactive.grace_task_compiler import compile_and_delegate
from proactive.hubops_routing import (
    registered_worker_task_types,
    resolved_route_binding,
    route_requires_owner_approval,
    route_clawops_objective,
)
from proactive.loop_contract import contract_fingerprint, validate_loop_contract
from proactive.thread_context_registry import (
    resolve_thread_context,
    resolve_thread_context_alias,
)


_LIST = {"type": "array", "items": {"type": "string"}, "minItems": 1}
_TASK_TYPES = list(registered_worker_task_types())
_APPROVAL_ATTEMPT = re.compile(r"核准[ \t\u3000]+([^ \t\u3000，,。.!！？?、:：;；]+)")


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
    match = _APPROVAL_ATTEMPT.search(str(message_text or ""))
    return match.group(1) if match is not None else ""


def _requires_structured_facebook_crosspost(
    task_type: str,
    external_targets: list[str],
) -> bool:
    """Identify Marketplace-to-group publishing before issuing approval."""
    if str(task_type or "").strip().casefold() != "browser_publish":
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


_GOAL = {
    "type": "object",
    "properties": {
        "objective": {"type": "string"},
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
            },
            "required": ["marketplace_listing_id", "group_ids"],
            "additionalProperties": False,
            "description": (
                "Required for an existing Marketplace listing cross-post to "
                "Facebook groups. The approval fingerprint binds both the "
                "source listing and every destination group."
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
        "canonical nested Loop Contract to ClawOps. Never call with an empty object."
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
            "enum": ["closed", "continued", "approval_blocked"],
        },
        "payload": {
            "type": "object",
            "description": (
                "closed: summary; continued: delegation_id, execution_task_id, "
                "review_task_id; approval_blocked: action, platform, scope, exact_question"
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


def _canonical_sections(args: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Accept the canonical nested contract and preserve legacy callers during rollout."""
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
    internal_turn = (
        get_session_env("HERMES_SESSION_INTERNAL", "").strip().lower() == "true"
    )
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
        if internal_turn and (approval_token or approval_refresh_token):
            raise ValueError(
                "An internal callback cannot consume an approval token."
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
            context = resolve_thread_context(
                platform=platform, chat_id=chat_id, thread_id=thread_id,
            )
        else:
            context = resolve_thread_context_alias(str(args.get("context_alias") or ""))
            platform = str(context.get("platform") or "")
            chat_id = str(context.get("chat_id") or "")
            thread_id = str(context.get("thread_id") or "")
        project = str(context["project"])
        topic_name = str(context["topic_name"])
        namespace = str(context.get("memory_namespace") or f"topic:{chat_id}:{thread_id}/{project}")
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
        goal, scope, verification, stop_rules, memory = _canonical_sections(args)
        task_type = str(args.get("task_type") or "")
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
        if _requires_structured_facebook_crosspost(
            task_type,
            external_targets,
        ) and facebook_crosspost is None:
            raise ValueError(
                "Facebook Marketplace group cross-post approval requires "
                "facebook_crosspost.marketplace_listing_id and exact "
                "facebook_crosspost.group_ids before an approval token can "
                "be issued."
            )
        if facebook_crosspost is not None and task_type != "browser_publish":
            raise ValueError(
                "facebook_crosspost is only valid for task_type=browser_publish."
            )
        supplied_request_instance = str(
            args.get("request_instance_id") or ""
        ).strip()
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
            if (
                supplied_request_instance
                and supplied_request_instance != request_instance_id
            ):
                raise ValueError(
                    "Chat request_instance_id must match the authenticated "
                    "message-derived instance."
                )
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
                        "external_targets": external_targets,
                        "facebook_crosspost": facebook_crosspost,
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
        if external_targets:
            contract["external_targets"] = external_targets
        if facebook_crosspost is not None:
            contract["facebook_crosspost"] = facebook_crosspost
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
        approval_needed = (
            bool(external_targets)
            or route_requires_owner_approval(routing_preview)
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
                    ) = kb.accepted_grace_callback_facebook_crosspost_scope(
                        conn,
                        review_task_id=origin_review_id,
                        event_id=origin_event_id,
                    )
                    if (
                        source_crosspost_listing_id is not None
                        and source_crosspost_group_ids
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
