"""Grace-owned Objective admission; execution workers cannot create roots."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

from gateway.session_context import get_session_env
from hermes_cli import kanban_db as kb
from proactive.thread_context_registry import resolve_thread_context


GRACE_OBJECTIVE_CREATE_SCHEMA = {
    "name": "grace_objective_create",
    "description": (
        "Grace control-plane action: create and read back the originating Objective "
        "and ordered stages BEFORE clawops_delegate. Never delegate Objective creation "
        "to a worker. Requires a fresh authenticated owner turn in a registered Topic. "
        "Creates no execution/review cards and grants no external-action authority."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "stage_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["title", "stage_keys", "acceptance_criteria"],
        "additionalProperties": False,
    },
}


def handle_grace_objective_create(args=None, **_kwargs):
    args = dict(args or {})
    try:
        if any(os.getenv(key, "").strip() for key in (
            "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_WORKER_AUTH_TOKEN",
        )):
            raise ValueError("Objective creation belongs to Grace, not a Kanban worker")
        if get_session_env("HERMES_SESSION_INTERNAL", "").strip().lower() == "true" or get_session_env("HERMES_SESSION_SOURCE", "").strip().lower() == "cron":
            raise ValueError("A callback cannot create a new originating Objective")
        platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
        chat = get_session_env("HERMES_SESSION_CHAT_ID", "").strip()
        thread = get_session_env("HERMES_SESSION_THREAD_ID", "").strip()
        user = get_session_env("HERMES_SESSION_USER_ID", "").strip()
        owner = get_session_env("HERMES_SESSION_OWNER_USER_ID", "").strip()
        message = get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip()
        source = get_session_env("HERMES_SESSION_MESSAGE_TEXT", "")
        session = get_session_env("HERMES_SESSION_KEY", "").strip()
        if platform != "telegram" or not all((chat, thread, message, source.strip(), session, user, owner)) or user != owner:
            raise ValueError("A fresh authenticated owner Telegram Topic message is required")
        context = resolve_thread_context(platform=platform, chat_id=chat, thread_id=thread)
        title = str(args.get("title") or "").strip()
        stages = args.get("stage_keys")
        criteria = args.get("acceptance_criteria")
        if not title or not isinstance(stages, list) or not stages or any(
            not isinstance(s, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", s) for s in stages
        ) or len(set(stages)) != len(stages):
            raise ValueError("Title and unique ordered stage keys are required")
        if not isinstance(criteria, list) or not criteria or any(not isinstance(s, str) or not s.strip() for s in criteria):
            raise ValueError("Non-empty acceptance criteria are required")
        digest = hashlib.sha256(source.encode()).hexdigest()
        identity = json.dumps([platform, chat, thread, message, digest], separators=(",", ":"))
        objective_id = "go_user_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        with kb.connect_closing() as conn:
            objective = kb.create_grace_objective(
                conn, objective_id=objective_id, platform=platform, chat_id=chat,
                thread_id=thread, session_key=session, title=title, objective=source,
                original_request_sha256=digest, required_stage_keys=stages,
                terminal_stage_key=stages[-1], acceptance_criteria=criteria,
                behavior_project=context["project"],
            )
            readback = [dict(r) for r in conn.execute(
                "SELECT stage_key,position,status FROM grace_objective_stages "
                "WHERE objective_id=? ORDER BY position", (objective_id,)
            )]
        return json.dumps({"status": "created", "objective_id": objective_id,
                           "objective_status": objective["status"], "stages": readback,
                           "objective_ref": {"objective_id": objective_id, "stage_key": stages[0]},
                           "planning_receipt": {"source": "controller", "message_id": message,
                                                "original_request_sha256": digest},
                           "tasks_created": False, "external_authority_granted": False}, ensure_ascii=False)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        return json.dumps({"status": "rejected", "reason": str(exc), "tasks_created": False}, ensure_ascii=False)
