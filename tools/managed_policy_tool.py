#!/usr/bin/env python3
"""Context-bound, read-only access to managed Topic policies."""

from __future__ import annotations

import json

from tools.registry import registry


def managed_policy_read(*, session_id: str | None) -> str:
    """Read policies bound to the caller's persisted messaging Topic."""
    from hermes_constants import get_default_hermes_root
    from hermes_state import SessionDB
    from proactive.policy_registry import (
        PolicyRegistryError,
        resolve_topic_policies_for_scope,
    )

    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return json.dumps({"success": False, "error": "trusted session_id is required"})

    db = SessionDB(db_path=get_default_hermes_root() / "state.db")
    try:
        session = db.get_session(clean_session_id)
    finally:
        db.close()
    if not session:
        return json.dumps({"success": False, "error": "trusted session was not found"})

    source = str(session.get("source") or "").strip().lower()
    chat_id = str(session.get("chat_id") or "").strip()
    thread_id = str(session.get("thread_id") or "").strip()
    if not source or not chat_id or not thread_id:
        return json.dumps(
            {
                "success": False,
                "error": "current session is not bound to a messaging Topic",
            }
        )

    try:
        result = resolve_topic_policies_for_scope(source, chat_id, thread_id)
    except PolicyRegistryError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    return json.dumps(
        {
            "success": True,
            "scope": {
                "platform": source,
                "chat_id": chat_id,
                "thread_id": thread_id,
            },
            **result,
        },
        ensure_ascii=False,
    )


registry.register(
    name="managed_policy_read",
    toolset="managed_policy",
    schema={
        "name": "managed_policy_read",
        "description": (
            "Read the complete, current, hash-verified managed policies bound to "
            "this exact messaging Topic. Always use this before answering questions "
            "about formal instructions, brand/channel rules, active policy versions, "
            "or policy SHA values, and before compiling policy-governed work. Topic "
            "Memory or Mem0 summaries are not substitutes. This tool is read-only and "
            "accepts no namespace; scope comes from the trusted current session."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    handler=lambda args, **kw: managed_policy_read(session_id=kw.get("session_id")),
    max_result_size_chars=120_000,
)
