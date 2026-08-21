"""Structured Telegram labels for human and multi-agent interactions.

The caller supplies trusted provenance in message metadata.  This module only
renders that provenance; it never infers actors from free-form assistant text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


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


def merge_interaction_metadata(
    metadata: Mapping[str, Any] | None,
    interaction: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge a trusted interaction descriptor into delivery metadata."""
    merged = dict(metadata or {})
    if interaction:
        merged[METADATA_KEY] = dict(interaction)
    return merged or None


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
        return {
            "status": str(payload.get("status") or ""),
            "assigned_agent": _clean_actor(payload.get("assigned_agent")),
            "delegation_id": str(payload.get("delegation_id") or ""),
            "execution_task_id": str(payload.get("execution_task_id") or ""),
            "grace_review_task_id": str(payload.get("grace_review_task_id") or ""),
        }
    return None


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
    "interaction_metadata",
    "merge_interaction_metadata",
]
