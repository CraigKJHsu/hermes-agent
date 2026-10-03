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


def _owner_turn():
    if any(os.getenv(key, "").strip() for key in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_WORKER_AUTH_TOKEN",
    )):
        raise ValueError("Objective planning belongs to Grace, not a Kanban worker")
    if get_session_env("HERMES_SESSION_INTERNAL", "").strip().lower() == "true" or get_session_env("HERMES_SESSION_SOURCE", "").strip().lower() == "cron":
        raise ValueError("Objective planning requires a fresh owner turn, not a callback")
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
    return platform, chat, thread, session, message, source, context


def handle_grace_objective_create(args=None, **_kwargs):
    args = dict(args or {})
    try:
        platform, chat, thread, session, message, source, context = _owner_turn()
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


GRACE_OBJECTIVE_PLAN_SCHEMA = {
    "name": "grace_objective_plan",
    "description": (
        "Grace control-plane action: revise an existing Objective's ordered stages "
        "before formal delegation. Requires a fresh authenticated owner Topic turn "
        "and the current revision. Preserve bound history and the existing terminal "
        "stage; insert a required repair before its dependent stage. Creates no cards, "
        "changes no review verdict, and grants no external-action authority."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "objective_id": {"type": "string", "minLength": 1},
            "expected_revision": {"type": "integer", "minimum": 1},
            "stage_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "current_stage_key": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["objective_id", "expected_revision", "stage_keys", "current_stage_key", "reason"],
        "additionalProperties": False,
    },
}


def handle_grace_objective_plan(args=None, **_kwargs):
    args = dict(args or {})
    try:
        platform, chat, thread, session, message, source, _context = _owner_turn()
        allowed = set(GRACE_OBJECTIVE_PLAN_SCHEMA["parameters"]["required"])
        if set(args) != allowed:
            raise ValueError("Exactly the public Objective plan fields are required")
        stages = args["stage_keys"]
        if (type(args["expected_revision"]) is not int or args["expected_revision"] < 1
                or not isinstance(stages, list) or not stages
                or any(not isinstance(s, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", s) for s in stages)
                or len(set(stages)) != len(stages)
                or not isinstance(args["current_stage_key"], str) or args["current_stage_key"] not in stages
                or not isinstance(args["objective_id"], str) or not args["objective_id"].strip()
                or not isinstance(args["reason"], str) or not args["reason"].strip()):
            raise ValueError("Valid Objective, revision, unique stages, current stage and reason are required")
        from hermes_cli.objective_workflow import plan
        digest = hashlib.sha256(source.encode()).hexdigest()
        with kb.connect_closing() as conn:
            result = plan(
                conn, objective_id=args["objective_id"], expected_revision=args["expected_revision"],
                platform=platform, chat_id=chat, thread_id=thread,
                required_stage_keys=stages, current_stage_key=args["current_stage_key"],
                reason=args["reason"] + f"\nOwner Topic message {message}; sha256={digest}",
            )
        return json.dumps({"status": "planned",
                           "objective_id": result["objective"]["objective_id"],
                           "revision": result["objective"]["revision"],
                           "objective_status": result["objective"]["status"],
                           "current_stage_key": result["objective"]["current_stage_key"],
                           "stages": [{key: row[key] for key in ("stage_key", "position", "status", "execution_task_id", "review_task_id")} for row in result["stages"]],
                           "planning_receipt": {"source": "controller", "message_id": message,
                                                "request_sha256": digest},
                           "tasks_created": False, "external_authority_granted": False}, ensure_ascii=False)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        return json.dumps({"status": "rejected", "reason": str(exc), "tasks_created": False}, ensure_ascii=False)
