"""Conservative Topic-placement guard for authenticated chat messages.

The guard compares a message with registry-owned Topic names, aliases, and
explicit ``topic_hints``.  It never reads another Topic's transcript.  A
warning is emitted only when one sibling Topic is a unique, high-confidence
match and the current Topic is materially weaker.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from proactive.thread_context_registry import load_thread_context_registry


_PENDING_TTL_SECONDS = 15 * 60
_MAX_RECENT_PAYLOADS = 256
_OVERRIDE_REPLIES = {
    "留在這裡",
    "就留在這裡",
    "仍在這裡",
    "繼續在這裡",
    "留在此topic",
    "仍在此topic",
}
_CANCEL_REPLIES = {"取消", "先不要", "不用處理", "停止"}
_STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class TopicMismatch:
    current_thread_id: str
    current_topic_name: str
    suggested_thread_id: str
    suggested_topic_name: str
    matched_hints: tuple[str, ...]


@dataclass(frozen=True)
class TopicPlacementDecision:
    action: str
    message: str = ""
    replacement_text: str = ""
    replacement_payload: Any | None = None
    mismatch: TopicMismatch | None = None


@dataclass(frozen=True)
class _PendingPlacement:
    original_text: str
    original_payload: Any | None
    mismatch: TopicMismatch
    created_at: float


_PENDING: dict[tuple[str, str, str, str], _PendingPlacement] = {}
_CONFIRMED_MESSAGES: dict[tuple[str, str, str, str, str], float] = {}
_RECENT_PAYLOADS: dict[tuple[str, str, str, str, str], tuple[Any, float]] = {}


def _normalize(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


def _compact(value: object) -> str:
    return "".join(_normalize(value).split())


def _lane_key(*, platform: str, chat_id: str, thread_id: str) -> tuple[str, str, str]:
    return (
        str(platform or "").strip().lower(),
        str(chat_id or "").strip(),
        str(thread_id or "").strip(),
    )


def _contains_hint(text: str, hint: object) -> bool:
    candidate = _normalize(hint)
    compact_candidate = _compact(candidate)
    if len(compact_candidate) < 2:
        return False
    if re.search(r"[\u3400-\u9fff]", compact_candidate):
        return compact_candidate in _compact(text)
    if len(compact_candidate) < 3:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])",
            text,
        )
    )


def _context_score(
    context: Mapping[str, Any], text: str
) -> tuple[int, tuple[str, ...]]:
    strong_values = [
        context.get("topic_name"),
        context.get("project_name"),
        *list(context.get("aliases") or []),
    ]
    hint_values = list(context.get("topic_hints") or [])
    strong_matches = {
        str(value).strip()
        for value in strong_values
        if str(value or "").strip() and _contains_hint(text, value)
    }
    hint_matches = {
        str(value).strip()
        for value in hint_values
        if str(value or "").strip() and _contains_hint(text, value)
    }
    return (len(strong_matches) * 3 + len(hint_matches), tuple(sorted(hint_matches)))


def detect_topic_mismatch(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    text: str,
    registry: Mapping[str, Any] | None = None,
) -> TopicMismatch | None:
    """Return one unique sibling Topic match, otherwise fail open."""
    lane = _lane_key(platform=platform, chat_id=chat_id, thread_id=thread_id)
    normalized_text = _normalize(text)
    if lane[0] != "telegram" or len(_compact(normalized_text)) < 4:
        return None

    registry = registry or load_thread_context_registry()
    contexts = [
        dict(item)
        for item in list(registry.get("contexts") or [])
        if isinstance(item, Mapping)
        and str(item.get("platform") or "").strip().lower() == lane[0]
        and str(item.get("chat_id") or "").strip() == lane[1]
    ]
    current = next(
        (
            item
            for item in contexts
            if str(item.get("thread_id") or "").strip() == lane[2]
        ),
        None,
    )
    if current is None:
        return None

    current_score, _ = _context_score(current, normalized_text)
    candidates: list[tuple[int, dict[str, Any], tuple[str, ...]]] = []
    for context in contexts:
        candidate_thread = str(context.get("thread_id") or "").strip()
        if candidate_thread == lane[2]:
            continue
        score, matches = _context_score(context, normalized_text)
        if score >= 2 and score >= current_score + 2:
            candidates.append((score, context, matches))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None

    _, suggested, matched_hints = candidates[0]
    return TopicMismatch(
        current_thread_id=lane[2],
        current_topic_name=str(current.get("topic_name") or f"Topic {lane[2]}").strip(),
        suggested_thread_id=str(suggested.get("thread_id") or "").strip(),
        suggested_topic_name=str(
            suggested.get("topic_name") or f"Topic {suggested.get('thread_id', '')}"
        ).strip(),
        matched_hints=matched_hints,
    )


def mismatch_warning(
    mismatch: TopicMismatch,
    *,
    override_available: bool = True,
) -> str:
    message = (
        "⚠️ 這段內容看起來比較屬於「"
        f"{mismatch.suggested_topic_name}」（Topic {mismatch.suggested_thread_id}），"
        f"但你目前在「{mismatch.current_topic_name}」"
        f"（Topic {mismatch.current_thread_id}）。\n\n"
        "我先不建立任務、不回溯，也不寫入長期記憶。請到正確 Topic "
        "重新貼上"
    )
    if override_available:
        return message + "；如果你是刻意要留在這裡，請回覆「留在這裡」。"
    return message + "。"


def _sweep(now: float) -> None:
    for key, pending in list(_PENDING.items()):
        if now - pending.created_at > _PENDING_TTL_SECONDS:
            _PENDING.pop(key, None)
    for key, confirmed_at in list(_CONFIRMED_MESSAGES.items()):
        if now - confirmed_at > _PENDING_TTL_SECONDS:
            _CONFIRMED_MESSAGES.pop(key, None)
    for key, (_, stored_at) in list(_RECENT_PAYLOADS.items()):
        if now - stored_at > _PENDING_TTL_SECONDS:
            _RECENT_PAYLOADS.pop(key, None)
    if len(_RECENT_PAYLOADS) > _MAX_RECENT_PAYLOADS:
        oldest = sorted(_RECENT_PAYLOADS, key=lambda key: _RECENT_PAYLOADS[key][1])
        for key in oldest[: len(_RECENT_PAYLOADS) - _MAX_RECENT_PAYLOADS]:
            _RECENT_PAYLOADS.pop(key, None)


def evaluate_inbound_topic_placement(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    message_id: str,
    text: str,
    original_payload: Any | None = None,
) -> TopicPlacementDecision:
    """Warn, consume an override/cancel reply, or allow the message."""
    lane = _lane_key(platform=platform, chat_id=chat_id, thread_id=thread_id)
    actor = str(user_id or "").strip()
    pending_key = (*lane, actor)
    now = time.time()
    normalized_reply = _compact(text)
    with _STATE_LOCK:
        _sweep(now)
        pending = _PENDING.get(pending_key) if actor else None
        if pending is not None and normalized_reply in _OVERRIDE_REPLIES:
            _PENDING.pop(pending_key, None)
            _CONFIRMED_MESSAGES[(*pending_key, str(message_id or "").strip())] = now
            return TopicPlacementDecision(
                action="allow",
                replacement_text=pending.original_text,
                replacement_payload=pending.original_payload,
                mismatch=pending.mismatch,
            )
        if pending is not None and normalized_reply in _CANCEL_REPLIES:
            _PENDING.pop(pending_key, None)
            return TopicPlacementDecision(
                action="cancel",
                message="已取消；沒有建立任務、回溯或寫入長期記憶。",
                mismatch=pending.mismatch,
            )
        if pending is not None:
            return TopicPlacementDecision(
                action="warn",
                message=(
                    "上一則內容仍在等待 Topic 確認，因此這則訊息尚未處理。"
                    "請回覆「留在這裡」以繼續上一則內容，或回覆「取消」。"
                ),
                mismatch=pending.mismatch,
            )

        mismatch = detect_topic_mismatch(
            platform=lane[0],
            chat_id=lane[1],
            thread_id=lane[2],
            text=text,
        )
        if mismatch is None:
            if actor and str(message_id or "").strip() and original_payload is not None:
                _RECENT_PAYLOADS[(*pending_key, str(message_id or "").strip())] = (
                    original_payload,
                    now,
                )
            return TopicPlacementDecision(action="allow")
        if actor:
            _PENDING[pending_key] = _PendingPlacement(
                original_text=str(text or ""),
                original_payload=original_payload,
                mismatch=mismatch,
                created_at=now,
            )
    return TopicPlacementDecision(
        action="warn",
        message=mismatch_warning(mismatch, override_available=bool(actor)),
        mismatch=mismatch,
    )


def topic_override_confirmed(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    message_id: str,
) -> bool:
    key = (
        *_lane_key(platform=platform, chat_id=chat_id, thread_id=thread_id),
        str(user_id or "").strip(),
        str(message_id or "").strip(),
    )
    now = time.time()
    with _STATE_LOCK:
        _sweep(now)
        return bool(key[-1] and key in _CONFIRMED_MESSAGES)


def register_pending_topic_override(
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    message_id: str,
    original_text: str,
    mismatch: TopicMismatch,
) -> bool:
    """Make a delegate-only mismatch warning resumable by the same sender."""
    lane = _lane_key(platform=platform, chat_id=chat_id, thread_id=thread_id)
    actor = str(user_id or "").strip()
    inbound_message_id = str(message_id or "").strip()
    original = str(original_text or "").strip()
    if not actor:
        return False
    now = time.time()
    with _STATE_LOCK:
        _sweep(now)
        recent = _RECENT_PAYLOADS.pop(
            (*lane, actor, inbound_message_id),
            None,
        )
        if recent is None:
            return False
        original_payload = recent[0]
        if not original and original_payload is not None:
            original = str(getattr(original_payload, "text", "") or "").strip()
        pending_key = (*lane, actor)
        if pending_key in _PENDING:
            return False
        _PENDING[pending_key] = _PendingPlacement(
            original_text=original,
            original_payload=original_payload,
            mismatch=mismatch,
            created_at=now,
        )
    return True


def _clear_guard_state_for_tests() -> None:
    with _STATE_LOCK:
        _PENDING.clear()
        _CONFIRMED_MESSAGES.clear()
        _RECENT_PAYLOADS.clear()
