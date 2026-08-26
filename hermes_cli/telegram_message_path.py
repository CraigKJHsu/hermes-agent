"""Trusted Telegram correlation envelope shared by Gateway and control plane.

The envelope is orchestration metadata, never model-authored task input.  The
full form is kept inside Hermes; ``backend_projection`` removes addressable
Telegram identifiers before the trace crosses the OpenClaw bridge.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Mapping


METADATA_KEY = "telegram_message_path"
SCHEMA_VERSION = "1.0"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _install_key() -> bytes:
    configured = os.getenv("HERMES_TELEGRAM_TRACE_HMAC_KEY", "").encode("utf-8")
    if configured:
        return configured
    from hermes_constants import get_hermes_home

    key_path = get_hermes_home() / "telegram-trace-hmac.key"
    try:
        existing = key_path.read_bytes()
        if len(existing) >= 32:
            return existing
    except FileNotFoundError:
        pass
    key_path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_bytes(32)
    try:
        descriptor = os.open(
            key_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, generated)
        finally:
            os.close(descriptor)
        return generated
    except FileExistsError:
        existing = key_path.read_bytes()
        if len(existing) < 32:
            raise RuntimeError("Telegram trace HMAC key is malformed")
        return existing


def _keyed_digest(value: str) -> str:
    return hmac.new(_install_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _opaque_ref(kind: str, value: Any) -> str:
    clean = _clean(value)
    return f"{kind}_{_keyed_digest(clean)[:24]}" if clean else ""


def _timestamp(value: str = "") -> str:
    return _clean(value) or datetime.now(timezone.utc).isoformat()


def actor(actor_id: str, role: str, display_name: str = "") -> dict[str, str]:
    """Keep machine identity, semantic role, and user-facing name separate."""
    clean_id = _clean(actor_id)
    clean_role = _clean(role)
    if display_name:
        label = _clean(display_name)
    elif clean_role == "user":
        label = "你"
    elif clean_role == "grace":
        label = "Grace"
    elif clean_role == "grace_review":
        label = "Grace 驗收"
    elif clean_role == "clawops":
        label = "ClawOps"
    elif clean_role == "openclaw_backend":
        label = clean_id or "OpenClaw"
    else:
        label = clean_id or clean_role or "未知角色"
    return {"id": clean_id, "role": clean_role, "display_name": label}


def build_telegram_message_path(
    *,
    chat_id: str,
    thread_id: str = "",
    chat_type: str = "",
    user_id: str = "",
    inbound_message_id: str = "",
    reply_to_message_id: str = "",
    session_key: str = "",
    session_id: str = "",
    codex_thread_id: str = "",
    observed_at: str = "",
) -> dict[str, Any]:
    """Create one deterministic Gateway-owned trace for a Telegram turn."""
    origin = {
        "platform": "telegram",
        "chat_id": _clean(chat_id),
        "thread_id": _clean(thread_id),
        "inbound_message_id": _clean(inbound_message_id),
        "session_key": _clean(session_key),
        "session_id": _clean(session_id),
    }
    trace_id = "tgtrace_" + _keyed_digest(
        json.dumps(origin, sort_keys=True, separators=(",", ":"))
    )[:32]
    path: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": trace_id,
        "platform": "telegram",
        "chat_id": origin["chat_id"],
        "thread_id": origin["thread_id"],
        "chat_type": _clean(chat_type),
        "user_id_sha256": _sha256(_clean(user_id)) if _clean(user_id) else "",
        "inbound_message_id": origin["inbound_message_id"],
        "reply_to_message_id": _clean(reply_to_message_id),
        "outbound_message_ids": [],
        "session_key": origin["session_key"],
        "session_id": origin["session_id"],
        "codex_thread_id": _clean(codex_thread_id),
        "delegation_id": "",
        "execution_task_id": "",
        "review_task_id": "",
        "run_id": "",
        "openclaw_backend_agent_id": "",
        "openclaw_backend_run_id": "",
        "openclaw_backend_session_key": "",
        "callback_target": {
            "platform": "telegram",
            "chat_id": origin["chat_id"],
            "thread_id": origin["thread_id"],
        },
        "hops": [],
        "privacy": {
            "raw_user_message": "not_disclosed",
            "user_id": "sha256_only",
        },
    }
    return append_hop(
        path,
        stage="telegram_inbound",
        from_actor=actor("telegram-user", "user"),
        to_actor=actor("grace", "grace"),
        status="observed",
        observed_at=observed_at,
        identifiers={"inbound_message_id": origin["inbound_message_id"]},
    )


def normalize_message_path(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, Mapping):
        return {}
    path = copy.deepcopy(dict(value))
    if path.get("platform") != "telegram" or not _clean(path.get("trace_id")):
        return {}
    path.setdefault("schema_version", SCHEMA_VERSION)
    path.setdefault("hops", [])
    path.setdefault("privacy", {"raw_user_message": "not_disclosed"})
    return path


def dumps_message_path(value: Any) -> str:
    path = normalize_message_path(value)
    return json.dumps(path, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if path else ""


def bind_message_path(value: Any, **identifiers: Any) -> dict[str, Any]:
    """Return a copy enriched only with known correlation identifiers."""
    path = normalize_message_path(value)
    if not path:
        return {}
    allowed = {
        "session_key",
        "session_id",
        "codex_thread_id",
        "delegation_id",
        "execution_task_id",
        "review_task_id",
        "run_id",
        "openclaw_backend_agent_id",
        "openclaw_backend_run_id",
        "openclaw_backend_session_key",
    }
    repeatable = {
        "run_id": "run_ids",
        "openclaw_backend_agent_id": "openclaw_backend_agent_ids",
        "openclaw_backend_run_id": "openclaw_backend_run_ids",
        "openclaw_backend_session_key": "openclaw_backend_session_keys",
    }
    for key, raw in identifiers.items():
        if key not in allowed:
            continue
        clean = _clean(raw)
        if not clean:
            continue
        existing = _clean(path.get(key))
        if existing and existing != clean:
            history_key = repeatable.get(key)
            if not history_key:
                raise ValueError(f"telegram_message_path {key} is already bound")
            history = [
                _clean(item) for item in path.get(history_key, []) if _clean(item)
            ]
            path[history_key] = list(dict.fromkeys([*history, existing, clean]))
        path[key] = clean
    return path


def merge_message_paths(current: Any, incoming: Any) -> dict[str, Any]:
    """Merge append-only trace advances without losing concurrent receipts."""
    base = normalize_message_path(current)
    newer = normalize_message_path(incoming)
    if not base:
        return newer
    if not newer:
        return base
    if base.get("trace_id") != newer.get("trace_id"):
        raise ValueError("Cannot merge different Telegram trace origins")
    immutable = {
        "schema_version",
        "trace_id",
        "platform",
        "chat_id",
        "thread_id",
        "chat_type",
        "user_id_sha256",
        "inbound_message_id",
        "reply_to_message_id",
        "session_key",
        "session_id",
        "codex_thread_id",
        "callback_target",
        "privacy",
    }
    for key, value in newer.items():
        if key in immutable or key in {"hops", "outbound_message_ids"}:
            continue
        if base.get(key) in (None, "", [], {}) and value not in (
            None,
            "",
            [],
            {},
        ):
            base[key] = copy.deepcopy(value)
    repeatable_fields = {
        "run_id": "run_ids",
        "openclaw_backend_agent_id": "openclaw_backend_agent_ids",
        "openclaw_backend_run_id": "openclaw_backend_run_ids",
        "openclaw_backend_session_key": "openclaw_backend_session_keys",
    }
    for key, history_key in repeatable_fields.items():
        current_value = _clean(base.get(key))
        incoming_value = _clean(newer.get(key))
        incoming_history = {
            _clean(item) for item in newer.get(history_key, []) if _clean(item)
        }
        # ``begin_backend_attempt`` places the former scalar into the incoming
        # history before advancing it. That proves ordering. A stale delivery
        # snapshot lacks the current scalar in its history and cannot regress
        # the canonical latest value.
        if (
            incoming_value
            and incoming_value != current_value
            and (not current_value or current_value in incoming_history)
        ):
            base[key] = incoming_value
    base["outbound_message_ids"] = list(
        dict.fromkeys(
            item
            for item in [
                *[_clean(item) for item in base.get("outbound_message_ids", [])],
                *[_clean(item) for item in newer.get("outbound_message_ids", [])],
            ]
            if item
        )
    )
    for history_key in (
        "run_ids",
        "openclaw_backend_agent_ids",
        "openclaw_backend_run_ids",
        "openclaw_backend_session_keys",
    ):
        base[history_key] = list(
            dict.fromkeys(
                item
                for item in [
                    *[_clean(item) for item in base.get(history_key, [])],
                    *[_clean(item) for item in newer.get(history_key, [])],
                ]
                if item
            )
        )
    for hop in newer.get("hops", []):
        base = append_hop(
            base,
            stage=str(hop.get("stage") or ""),
            from_actor=hop.get("from_actor") or {},
            to_actor=hop.get("to_actor") or {},
            status=str(hop.get("status") or ""),
            identifiers=hop.get("identifiers") or {},
            observed_at=str(hop.get("observed_at") or ""),
        )
    return base


def begin_backend_attempt(
    value: Any,
    *,
    run_id: Any,
    backend_agent_id: Any,
) -> dict[str, Any]:
    """Advance to a correction attempt while preserving prior backend ids."""
    path = normalize_message_path(value)
    if not path:
        return {}
    for key, history_key in (
        ("run_id", "run_ids"),
        ("openclaw_backend_agent_id", "openclaw_backend_agent_ids"),
        ("openclaw_backend_run_id", "openclaw_backend_run_ids"),
        ("openclaw_backend_session_key", "openclaw_backend_session_keys"),
    ):
        old = _clean(path.get(key))
        if old:
            history = [_clean(item) for item in path.get(history_key, [])]
            path[history_key] = list(dict.fromkeys([*history, old]))
        if key != "run_id":
            path[key] = ""
    path["run_id"] = ""
    return bind_message_path(
        path,
        run_id=run_id,
        openclaw_backend_agent_id=backend_agent_id,
    )


def append_hop(
    value: Any,
    *,
    stage: str,
    from_actor: Mapping[str, Any],
    to_actor: Mapping[str, Any],
    status: str,
    identifiers: Mapping[str, Any] | None = None,
    observed_at: str = "",
) -> dict[str, Any]:
    path = normalize_message_path(value) if value else dict(value or {})
    if not path:
        return {}
    hop = {
        "stage": _clean(stage),
        "from_actor": dict(from_actor),
        "to_actor": dict(to_actor),
        "status": _clean(status),
        "observed_at": _timestamp(observed_at),
        "identifiers": {
            str(key): _clean(item)
            for key, item in dict(identifiers or {}).items()
            if _clean(item)
        },
    }
    signature = (
        hop["stage"],
        hop["from_actor"].get("id"),
        hop["to_actor"].get("id"),
        tuple(sorted(hop["identifiers"].items())),
    )
    hops = path.setdefault("hops", [])
    for existing in hops:
        existing_signature = (
            existing.get("stage"),
            (existing.get("from_actor") or {}).get("id"),
            (existing.get("to_actor") or {}).get("id"),
            tuple(sorted((existing.get("identifiers") or {}).items())),
        )
        if existing_signature == signature:
            # Evidence is append-only. A delayed snapshot must not rewrite the
            # status or timestamp of an already committed hop.
            return path
    hops.append(hop)
    return path


def backend_projection(value: Any) -> dict[str, Any]:
    """Remove Telegram routing secrets while retaining correlation evidence."""
    path = normalize_message_path(value)
    if not path:
        return {}
    projected = copy.deepcopy(path)
    for key, kind in (
        ("chat_id", "chat"),
        ("thread_id", "thread"),
        ("inbound_message_id", "message"),
        ("reply_to_message_id", "message"),
    ):
        projected[key] = _opaque_ref(kind, projected.get(key))
    projected["session_key"] = _opaque_ref("session", projected.get("session_key"))
    projected["session_id"] = _opaque_ref("session", projected.get("session_id"))
    projected["codex_thread_id"] = _opaque_ref(
        "codex_thread", projected.get("codex_thread_id")
    )
    projected["openclaw_backend_session_key"] = _opaque_ref(
        "backend_session", projected.get("openclaw_backend_session_key")
    )
    projected["openclaw_backend_session_keys"] = [
        _opaque_ref("backend_session", item)
        for item in projected.get("openclaw_backend_session_keys", [])
        if _clean(item)
    ]
    projected["outbound_message_ids"] = [
        _opaque_ref("message", item)
        for item in projected.get("outbound_message_ids", [])
        if _clean(item)
    ]
    telegram_identifier_keys = {
        "chat_id",
        "thread_id",
        "inbound_message_id",
        "reply_to_message_id",
        "outbound_message_ids",
        "telegram_message_id",
        "approval_message_id",
    }
    for hop in projected.get("hops", []):
        identifiers = hop.get("identifiers")
        if not isinstance(identifiers, dict):
            continue
        for key in list(identifiers):
            if key not in telegram_identifier_keys:
                continue
            identifiers[key] = _opaque_ref("message", identifiers[key])
    callback = projected.get("callback_target") or {}
    projected["callback_target"] = {
        "platform": "telegram",
        "route_ref": _opaque_ref(
            "route",
            f"{callback.get('chat_id', '')}:{callback.get('thread_id', '')}",
        ),
    }
    projected["privacy"] = {
        "raw_user_message": "not_disclosed",
        "telegram_addresses": "opaque_refs_only",
        "user_id": "keyed_opaque_ref_only",
    }
    projected["user_id_sha256"] = _opaque_ref(
        "user", projected.get("user_id_sha256")
    )
    return projected


def record_outbound_delivery(
    value: Any,
    message_ids: list[Any],
    *,
    interaction_kind: str = "",
    observed_at: str = "",
) -> dict[str, Any]:
    """Bind Telegram acknowledgements and the final visible delivery hop."""
    path = normalize_message_path(value)
    if not path:
        return {}
    normalized_ids = [_clean(item) for item in message_ids if _clean(item)]
    existing = [_clean(item) for item in path.get("outbound_message_ids", [])]
    path["outbound_message_ids"] = list(dict.fromkeys([*existing, *normalized_ids]))
    kind = _clean(interaction_kind).lower()
    if kind == "execution":
        source = actor(
            _clean(path.get("openclaw_backend_agent_id")) or "openclaw",
            "openclaw_backend",
        )
    elif kind in {"review", "callback"}:
        source = actor("grace-review", "grace_review")
    else:
        source = actor("grace", "grace")
    return append_hop(
        path,
        stage="telegram_outbound",
        from_actor=source,
        to_actor=actor("telegram-user", "user"),
        status="observed",
        identifiers={"outbound_message_ids": ",".join(normalized_ids)},
        observed_at=observed_at,
    )


def display_path(value: Any, *, fallback: list[str] | None = None) -> list[str]:
    """Project observed actor hops into a concise Telegram label path."""
    path = normalize_message_path(value)
    names: list[str] = []
    for hop in path.get("hops", []) if path else []:
        for side in ("from_actor", "to_actor"):
            label = _clean((hop.get(side) or {}).get("display_name"))
            if label and (not names or names[-1] != label):
                names.append(label)
    return names or list(fallback or [])
