"""Hard enforcement for the Grace coordinator / ClawOps executor boundary."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from gateway.session_context import get_session_env


_DIRECT_EXECUTION_NAMES = {
    "terminal", "execute_code", "patch", "write_file", "delete_file",
    "image_generate", "computer_use", "cronjob", "ha_call_service",
    "browser_click", "browser_type", "browser_press", "browser_dialog",
    "browser_navigate", "browser_upload_files",
    "facebook_page_graph_publish",
    "openclaw_delegate",
}
_DIRECT_EXECUTION_PREFIXES = (
    "playwright_", "computer_",
)
# Navigation stays in the execution set even when the intended inspection is
# read-only: an arbitrary GET can invoke logout, OAuth callbacks, or legacy
# state-changing endpoints. Grace may inspect an already-open page; changing
# the browser's URL belongs inside the scoped ClawOps contract.
_KANBAN_CONTROL_MUTATIONS = {
    "kanban_create",
    "kanban_unblock",
    "kanban_link",
    "kanban_complete",
    "kanban_block",
    "kanban_heartbeat",
    "kanban_comment",
}
_REVIEW_SELF_MUTATIONS = {
    "kanban_complete",
    "kanban_block",
    "kanban_heartbeat",
    "kanban_comment",
}

_READ_ONLY_CDP_METHODS = {
    "Target.getTargets",
    "Page.getNavigationHistory",
    "Page.getFrameTree",
    "Accessibility.getFullAXTree",
    "DOMSnapshot.captureSnapshot",
    "Network.getResponseBody",
    "Performance.getMetrics",
    "Log.enable",
}
_CURRENT_PAGE_INSPECTION_TOOLS = {
    "browser_snapshot",
    "browser_scroll",
    "browser_vision",
}
_NAVIGATION_ARGUMENTS = {
    "url",
    "href",
    "target_url",
    "targetUrl",
}


def _authorized_loop_worker_role(session_id: str = "") -> str:
    """Return ``execution``/``review``/``worker`` for an active claimed run."""
    task_id = os.getenv("HERMES_KANBAN_TASK", "").strip()
    run_id = os.getenv("HERMES_KANBAN_RUN_ID", "").strip()
    claim_lock = os.getenv("HERMES_KANBAN_CLAIM_LOCK", "").strip()
    worker_auth_token = os.getenv(
        "HERMES_KANBAN_WORKER_AUTH_TOKEN", "",
    ).strip()
    if not task_id or not run_id or not claim_lock or not worker_auth_token:
        return ""
    board = os.getenv("HERMES_KANBAN_BOARD", "").strip() or None
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect_closing(board=board) as conn:
            role = kb.validate_grace_loop_worker_auth(
                conn,
                task_id=task_id,
                run_id=run_id,
                claim_lock=claim_lock,
                worker_auth_token=worker_auth_token,
            )
            if role:
                return role
            delegated = conn.execute(
                """
                SELECT 1
                  FROM grace_delegations
                 WHERE execution_task_id = ? OR review_task_id = ?
                """,
                (task_id, task_id),
            ).fetchone()
            if delegated is not None:
                return ""
            if kb.validate_kanban_worker_auth(
                conn,
                task_id=task_id,
                run_id=run_id,
                claim_lock=claim_lock,
                worker_auth_token=worker_auth_token,
            ):
                return "worker"
            return ""
    except (OSError, sqlite3.Error, ValueError):
        return ""


def _is_authorized_clawops_worker(session_id: str = "") -> bool:
    return _authorized_loop_worker_role(session_id) == "execution"


def _session_source(session_id: str) -> str:
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    if platform:
        return platform
    if not session_id:
        return ""
    db_path = Path(os.getenv("HERMES_STATE_DB", "~/.hermes/state.db")).expanduser()
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT source FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return str(row[0] or "").strip().lower() if row else ""
    except (OSError, sqlite3.Error):
        return ""


def is_direct_execution_tool(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    name = str(tool_name or "").strip().lower()
    if name == "browser_cdp":
        method = str((args or {}).get("method") or "").strip()
        return method not in _READ_ONLY_CDP_METHODS
    return name in _DIRECT_EXECUTION_NAMES or name.startswith(_DIRECT_EXECUTION_PREFIXES)


def enforce_grace_execution_boundary(
    *, tool_name: str, args: dict[str, Any], next_call, session_id: str = "", **_kwargs: Any
) -> Any:
    """Block direct execution in Grace/cron sessions; Kanban workers remain allowed."""
    normalized_tool = str(tool_name or "").strip().lower()
    has_worker_provenance = any(
        os.getenv(name, "").strip()
        for name in (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_WORKER_AUTH_TOKEN",
        )
    )
    if has_worker_provenance and normalized_tool == "clawops_delegate":
        return json.dumps(
            {
                "status": "rejected",
                "task_created": False,
                "reason": (
                    "A delegated Loop worker may not recursively delegate. "
                    "Execution workers must report the active task; review workers "
                    "must accept, reject, or block the active review."
                ),
            },
            ensure_ascii=False,
        )
    worker_role = _authorized_loop_worker_role(session_id)
    if worker_role in {"execution", "worker"}:
        active_task_id = os.getenv("HERMES_KANBAN_TASK", "").strip()
        target_task_id = str((args or {}).get("task_id") or "").strip()
        if normalized_tool in _KANBAN_CONTROL_MUTATIONS:
            is_self_report = normalized_tool in _REVIEW_SELF_MUTATIONS and (
                not target_task_id or target_task_id == active_task_id
            )
            if not is_self_report:
                return json.dumps(
                    {
                        "status": "blocked_by_grace_execution_policy",
                        "tool": tool_name,
                        "source": _session_source(session_id),
                        "reason": (
                            "A Kanban worker may only complete, block, heartbeat, "
                            "or comment on its own active task."
                        ),
                    },
                    ensure_ascii=False,
                )
        return next_call(args)
    source = _session_source(session_id)
    has_navigation_argument = (
        normalized_tool in _CURRENT_PAGE_INSPECTION_TOOLS
        and any(
            key in (args or {}) and str((args or {}).get(key) or "").strip()
            for key in _NAVIGATION_ARGUMENTS
        )
    )
    is_forbidden_control_mutation = normalized_tool in _KANBAN_CONTROL_MUTATIONS
    if worker_role == "review" and normalized_tool in _REVIEW_SELF_MUTATIONS:
        target_task_id = str((args or {}).get("task_id") or "").strip()
        active_task_id = os.getenv("HERMES_KANBAN_TASK", "").strip()
        if not target_task_id or target_task_id == active_task_id:
            return next_call(args)
    if not (
        is_direct_execution_tool(tool_name, args)
        or is_forbidden_control_mutation
        or has_navigation_argument
    ):
        return next_call(args)
    if normalized_tool == "openclaw_delegate":
        reason = (
            "openclaw_delegate is a diagnostic dry-run bridge, not an execution fallback. "
            "Correct the validation error and retry clawops_delegate with the canonical nested "
            "Loop Contract. Do not claim delegation unless it returns both execution_task_id "
            "and grace_review_task_id."
        )
    elif is_forbidden_control_mutation:
        reason = (
            "Grace may not create, link, complete, block, heartbeat, or unblock "
            "Kanban execution cards directly. Compile the exact Loop Contract "
            "and use clawops_delegate so the reserved delegation saga creates "
            "and arms both cards with callback and subscriptions."
        )
    else:
        reason = (
            "Grace may use read-only browser inspection to understand and classify the task, "
            "but may not click, type, submit, upload, mutate external state, or execute the task. "
            "Compile a complete Loop Contract and call clawops_delegate; ClawOps performs "
            "execution and Grace reviews the evidence."
        )
    return json.dumps(
        {
            "status": "blocked_by_grace_execution_policy",
            "tool": tool_name,
            "source": source,
            "reason": reason,
        },
        ensure_ascii=False,
    )
