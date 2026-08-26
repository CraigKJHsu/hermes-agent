"""Structured Telegram labels for human and multi-agent interactions.

The caller supplies trusted provenance in message metadata. This module only
renders that provenance; it never infers actors from free-form assistant text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from hermes_cli.telegram_message_path import (
    actor as trace_actor,
    normalize_message_path,
)


METADATA_KEY = "telegram_interaction"

_KIND_LABELS = {
    "direct": ("🟢", "直接對話"),
    "handoff": ("🟠", "任務交接"),
    "execution": ("🔵", "Agent 執行"),
    "review": ("🟣", "Agent 驗收"),
    "callback": ("🟣", "Agent 回報"),
}


def interaction_metadata(
    kind: str,
    path: Iterable[Any],
    *,
    assigned_agent: Any = "",
) -> dict[str, Any]:
    """Return adapter metadata for a provenance-backed Telegram label."""
    normalized_path = _normalize_path(path)
    assigned = _clean_actor(assigned_agent)
    if assigned and assigned not in normalized_path:
        normalized_path.append(assigned)
    return {
        METADATA_KEY: {
            "kind": str(kind or "").strip().lower(),
            "path": normalized_path,
            "assigned_agent": assigned,
        }
    }


def interaction_metadata_from_message_path(
    message_path: Mapping[str, Any] | str | None,
    kind: str,
    *,
    actor_id: Any = "",
) -> dict[str, Any]:
    """Render a user-facing route from the trusted trace, never task prose."""
    path = normalize_message_path(message_path)
    clean_kind = str(kind or "").strip().lower()
    raw_actor_id = " ".join(str(actor_id or "").split())
    backend_id = _clean_actor(
        actor_id or path.get("openclaw_backend_agent_id") or "OpenClaw"
    )
    if clean_kind in {"review", "callback"}:
        route = (
            ["Grace 驗收", "你"]
            if raw_actor_id.casefold() in {"default", "clawops-review", "grace-review"}
            else [backend_id, "ClawOps", "Grace 驗收", "你"]
        )
    elif clean_kind in {"execution", "handoff"}:
        route = ["你", "Grace", "ClawOps", backend_id]
    else:
        route = ["你", "Grace"]
    return interaction_metadata(
        clean_kind,
        route,
        assigned_agent=(backend_id if clean_kind == "execution" else ""),
    )


def merge_interaction_metadata(
    metadata: Mapping[str, Any] | None,
    interaction: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge a trusted interaction descriptor into delivery metadata."""
    merged = dict(metadata or {})
    if interaction:
        merged[METADATA_KEY] = dict(interaction)
    return merged or None


def initialize_turn_interaction_context(
    context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Clone turn context and seed fresh direct/callback provenance."""
    initialized = dict(context or {})
    if initialized.get("internal_kind") == "grace_callback":
        assigned = _clean_actor(
            initialized.get("execution_assignee") or "執行 Agent"
        )
        seed = interaction_metadata(
            "callback",
            [assigned, "ClawOps", "Grace", "你"],
        )
    else:
        seed = interaction_metadata("direct", ["你", "Grace"])
    initialized[METADATA_KEY] = seed[METADATA_KEY]
    return initialized


def propagate_interaction_context(
    target: dict[str, Any] | None,
    source: Mapping[str, Any] | None,
) -> bool:
    """Copy the final descriptor across recursive turn-delivery boundaries."""
    if not isinstance(target, dict) or not isinstance(source, Mapping):
        return False
    descriptor = source.get(METADATA_KEY)
    if not isinstance(descriptor, Mapping):
        return False
    target[METADATA_KEY] = dict(descriptor)
    return True


def decorate_telegram_message(
    content: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Prefix one Telegram message with its interaction class and route."""
    if not content:
        return content
    interaction = (metadata or {}).get(METADATA_KEY)
    if not isinstance(interaction, Mapping):
        return content
    kind = str(interaction.get("kind") or "").strip().lower()
    label = _KIND_LABELS.get(kind)
    path = _normalize_path(interaction.get("path") or [])
    if label is None or not path:
        return content
    icon, title = label
    route = " ↔ ".join(path) if kind == "direct" else " → ".join(path)
    header = f"{icon} {title}｜{route}"
    stripped = content.lstrip()
    if stripped == header or stripped.startswith(header + "\n"):
        return content
    return f"{header}\n\n{content}"


def delegation_result_from_messages(
    messages: Sequence[Mapping[str, Any]] | None,
) -> dict[str, str] | None:
    """Read the latest current-turn ``clawops_delegate`` tool result.

    Scanning stops at the latest user boundary, preventing an older delegation
    in conversation history from being attributed to a new direct reply.
    """
    for message in reversed(list(messages or [])):
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        if role == "user":
            break
        if role != "tool" or message.get("tool_name") != "clawops_delegate":
            continue
        payload = _json_mapping(message.get("content"))
        if payload is None:
            return None
        result = {
            "status": str(payload.get("status") or ""),
            "assigned_agent": _clean_actor(payload.get("assigned_agent")),
            "delegation_id": str(payload.get("delegation_id") or ""),
            "execution_task_id": str(payload.get("execution_task_id") or ""),
            "grace_review_task_id": str(payload.get("grace_review_task_id") or ""),
        }
        if payload.get("trace_id"):
            result["trace_id"] = str(payload["trace_id"])
        if payload.get("kanban_board"):
            result["kanban_board"] = str(payload["kanban_board"])
        return result
    return None


def delegation_was_queued(result: Mapping[str, Any] | None) -> bool:
    """Return true only when ClawOps created the execution/review task pair."""
    if not isinstance(result, Mapping):
        return False
    return bool(
        str(result.get("status") or "").strip().lower() == "queued"
        and str(result.get("execution_task_id") or "").strip()
        and str(result.get("grace_review_task_id") or "").strip()
    )


def is_queued_clawops_delegation(
    tool_name: Any,
    result: Mapping[str, Any] | None,
) -> bool:
    """Require both trusted tool identity and a created ClawOps task pair."""
    return (
        str(tool_name or "").strip() == "clawops_delegate"
        and delegation_was_queued(result)
    )


def _json_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _clean_actor(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if text.casefold() in {"default", "clawops-review", "grace-review"}:
        return trace_actor(text, "grace_review")["display_name"]
    return text[:80]


def _normalize_path(path: Iterable[Any]) -> list[str]:
    if isinstance(path, (str, bytes)):
        path = [path]
    normalized: list[str] = []
    for raw in path:
        actor = _clean_actor(raw)
        if actor and (not normalized or normalized[-1] != actor):
            normalized.append(actor)
    return normalized


__all__ = [
    "METADATA_KEY",
    "decorate_telegram_message",
    "delegation_result_from_messages",
    "delegation_was_queued",
    "initialize_turn_interaction_context",
    "interaction_metadata",
    "interaction_metadata_from_message_path",
    "is_queued_clawops_delegation",
    "merge_interaction_metadata",
    "propagate_interaction_context",
]
