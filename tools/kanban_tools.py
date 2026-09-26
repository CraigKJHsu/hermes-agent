"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session still sees **zero**
kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.redact import redact_sensitive_text
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200
KANBAN_SHOW_STRING_LIMIT = 2_000
KANBAN_SHOW_LIST_LIMIT = 20
KANBAN_COMPLETE_METADATA_MAX_BYTES = 1_000_000
WORKSPACE_COMPLETION_RECEIPT_KEY = "_workspace_completion_receipt"


def _bound_show_value(value: Any, *, depth: int = 0) -> Any:
    """Bound diagnostic history while keeping JSON valid and self-describing."""
    if isinstance(value, str):
        if len(value) <= KANBAN_SHOW_STRING_LIMIT:
            return value
        omitted = len(value) - KANBAN_SHOW_STRING_LIMIT
        return value[:KANBAN_SHOW_STRING_LIMIT] + f"<truncated chars={omitted}>"
    if depth >= 10:
        return "<truncated depth>"
    if isinstance(value, dict):
        items = list(value.items())
        bounded = {
            str(key): _bound_show_value(item, depth=depth + 1)
            for key, item in items[:50]
        }
        if len(items) > 50:
            bounded["_truncated_keys"] = len(items) - 50
        return bounded
    if isinstance(value, (list, tuple)):
        bounded = [
            _bound_show_value(item, depth=depth + 1)
            for item in value[:KANBAN_SHOW_LIST_LIMIT]
        ]
        if len(value) > KANBAN_SHOW_LIST_LIMIT:
            bounded.append({"_truncated_items": len(value) - KANBAN_SHOW_LIST_LIMIT})
        return bounded
    return value


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _load_worker_completion_metadata(path_value: Any) -> tuple[dict, dict]:
    """Load a completion metadata object from this worker's workspace.

    Large structured handoffs should not have to be re-serialized into a
    model tool call after the worker has already written and validated them.
    The dispatcher-provided workspace is the trust boundary: resolving both
    paths rejects traversal and symlink escapes before any bytes are read.
    """
    workspace_raw = os.environ.get("HERMES_KANBAN_WORKSPACE")
    if not workspace_raw:
        raise ValueError(
            "metadata_path requires a dispatcher-provided worker workspace"
        )
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError("metadata_path must be a non-empty file path")

    try:
        workspace_spelling = Path(workspace_raw).expanduser().absolute()
        workspace = workspace_spelling.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("dispatcher-provided worker workspace is invalid") from exc
    candidate = Path(path_value)
    if candidate.is_absolute():
        relative = None
        for allowed_root in (workspace_spelling, workspace):
            try:
                relative = candidate.relative_to(allowed_root)
                break
            except ValueError:
                continue
        if relative is None:
            raise ValueError(
                "metadata_path must stay inside the current task workspace"
            )
    else:
        relative = candidate
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("metadata_path must stay inside the current task workspace")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    opened: list[int] = []
    try:
        current_fd = os.open(workspace, directory_flags)
        opened.append(current_fd)
        for part in parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            opened.append(current_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        opened.append(file_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise ValueError("metadata_path must name an existing regular file")
        if file_stat.st_size > KANBAN_COMPLETE_METADATA_MAX_BYTES:
            raise ValueError(
                "metadata_path exceeds the 1000000-byte completion metadata limit"
            )
        chunks: list[bytes] = []
        remaining = KANBAN_COMPLETE_METADATA_MAX_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > KANBAN_COMPLETE_METADATA_MAX_BYTES:
            raise ValueError(
                "metadata_path exceeds the 1000000-byte completion metadata limit"
            )
        text = raw.decode("utf-8")
    except OSError as exc:
        raise ValueError(
            "metadata_path must stay inside the current task workspace and "
            "name an existing regular file"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ValueError("metadata_path must contain UTF-8 JSON") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)

    def _finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    def _reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        loaded = json.loads(
            text,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("metadata_path must contain valid JSON") from exc
    if not isinstance(loaded, dict):
        raise ValueError("metadata_path JSON must be an object/dict")
    return loaded, {
        "source_relative_path": relative.as_posix(),
        "source_size_bytes": len(raw),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _validate_workspace_file_completion_contract(task_id: str) -> None:
    """Require a sealed devops route before accepting ``metadata_path``.

    ``metadata_path`` is a structured execution capability, so its authority
    comes from the compiled contract rather than from natural-language intent.
    The task row and fenced contract are both controller-written state.
    """
    run_id = _worker_run_id(task_id)
    if run_id is None:
        raise ValueError(
            "metadata_path requires an active dispatcher run with a compiled "
            "completion_handoff"
        )
    kb, conn = _connect()
    try:
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        contract = (
            kb._grace_compiled_contract(task.body or "")
            if task is not None
            and kb._grace_loop_stage_header(task.body or "") == "execution"
            else None
        )
    finally:
        conn.close()
    if (
        task is None
        or run is None
        or task.current_run_id != run_id
        or run.task_id != task_id
        or run.ended_at is not None
    ):
        raise ValueError(
            "metadata_path requires the worker's current active dispatcher run"
        )
    handoff = (
        contract.get("completion_handoff")
        if isinstance(contract, Mapping)
        else None
    )
    resolved = (
        ((contract.get("routing") or {}).get("resolved") or {})
        if isinstance(contract, Mapping)
        else {}
    )
    assignment = (
        resolved.get("assignment")
        if isinstance(resolved, Mapping)
        else None
    )
    spawn = (
        run.metadata.get("worker_spawn")
        if isinstance(run.metadata, dict)
        else None
    )
    required_tools = {
        str(name or "").strip()
        for name in (
            assignment.get("required_callable_tools") or []
            if isinstance(assignment, Mapping)
            else []
        )
        if str(name or "").strip()
    }
    if not (
        isinstance(handoff, Mapping)
        and handoff.get("metadata_source") == "workspace_file"
        and resolved.get("task_type") == "devops"
        and isinstance(assignment, Mapping)
        and assignment.get("runtime_profile") == "clawops-dev"
        and task.assignee == "clawops-dev"
        and task.executor_profile == "clawops-dev"
        and run.profile == "clawops-dev"
        and "terminal" in required_tools
        and isinstance(spawn, dict)
        and spawn.get("ok") is True
        and spawn.get("profile") == "clawops-dev"
        and "terminal" in set(spawn.get("required_runtime_tools") or [])
        and not list(spawn.get("missing_required_tools") or [])
    ):
        raise ValueError(
            "metadata_path requires a compiled completion_handoff with "
            "metadata_source=workspace_file on the sealed devops/clawops-dev "
            "route and a successful terminal capability attestation"
        )


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "project_id": task.project_id,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    if args.get("view", "summary") == "delivery":
        from hermes_cli.delivery_readback import read_task_delivery
        try:
            return json.dumps(read_task_delivery(args), ensure_ascii=False)
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
            return tool_error(f"kanban_show delivery: {exc}")
    if args.get("view") not in {None, "summary", "full", "contract", "delivery", "controller"}:
        return tool_error("kanban_show: view must be summary, full, contract, controller or delivery")
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            if args.get("view") == "controller":
                from hermes_cli.controller_readback import task_controller_readback
                return json.dumps(_bound_show_value(task_controller_readback(conn, tid)), ensure_ascii=False)
            comments = kb.list_comments(conn, tid)
            # A full card repeats policy bodies in its row, runs and worker
            # context. Tool truncation can then hide the actual work order.
            # Keep the worker's own execution contract intact and read pinned
            # policy text through managed_policy_read instead of duplicating it.
            is_execution = kb._grace_loop_stage_header(task.body or "") == "execution"
            contract_view = args.get("view") == "contract" or (
                args.get("view") is None and is_execution
                and os.environ.get("HERMES_KANBAN_TASK") == tid
            )
            if contract_view:
                contract = kb._grace_compiled_contract(task.body or "")
                if not isinstance(contract, dict):
                    return tool_error("kanban_show contract: task has no compiled Loop Contract")
                contract = dict(contract)
                policies = contract.get("policy_snapshots", [])
                policy_references = [
                    {
                        key: policy[key]
                        for key in ("policy_id", "version", "sha256")
                        if key in policy
                    }
                    for policy in policies
                    if isinstance(policy, dict)
                ]
                contract_json = json.dumps(
                    contract,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                try:
                    offset = int(args.get("offset", 0))
                    limit = int(args.get("limit", 12_000))
                except (TypeError, ValueError):
                    return tool_error(
                        "kanban_show contract: offset and limit must be integers"
                    )
                if offset < 0 or limit < 1 or limit > 12_000:
                    return tool_error(
                        "kanban_show contract: offset must be non-negative and "
                        "limit must be between 1 and 12000"
                    )
                if offset > len(contract_json):
                    return tool_error(
                        "kanban_show contract: offset exceeds contract length"
                    )
                common = _bound_show_value({
                    "view": "contract",
                    "task": {"id": task.id, "title": task.title,
                             "stage": kb._grace_loop_stage_header(task.body or ""),
                             "status": task.status, "current_run_id": task.current_run_id},
                    "policy_references": policy_references,
                    "policy_read_instruction": "The canonical contract pages include complete pinned policy snapshots; managed_policy_read may provide independent task-scoped read receipts when available.",
                    "latest_comment": (
                        {"author": comments[-1].author, "body": comments[-1].body,
                         "created_at": comments[-1].created_at} if comments else None
                    ),
                    "history_instruction": "Use view='full' explicitly for history and the original stored task body.",
                })
                if offset == 0 and len(contract_json) <= limit:
                    common["contract"] = contract
                    return json.dumps(common, ensure_ascii=False)

                end = min(len(contract_json), offset + limit)
                while True:
                    response = dict(common)
                    response.update({
                        "view": "contract_chunk",
                        "contract_json_chunk": contract_json[offset:end],
                        "offset": offset,
                        "next_offset": end,
                        "total_chars": len(contract_json),
                        "complete": end == len(contract_json),
                        "contract_json_sha256": hashlib.sha256(
                            contract_json.encode("utf-8")
                        ).hexdigest(),
                        "pagination_instruction": (
                            "Concatenate contract_json_chunk values in offset order; "
                            "call view='contract' with offset=next_offset until complete."
                        ),
                    })
                    output = json.dumps(response, ensure_ascii=False)
                    if len(output) <= 18_000 or end <= offset + 1:
                        return output
                    end = offset + max(1, (end - offset) // 2)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            if args.get("view") in {None, "summary"} and not str(
                task.body or ""
            ).startswith(
                (
                    "GRACE_LOOP_CONTRACT_STAGE: review",
                    "GRACE_LOOP_CONTRACT_STAGE: grace_review",
                )
            ):
                latest_run = runs[-1] if runs else None
                return json.dumps(_bound_show_value({
                    "view": "summary",
                    "task": {
                        "id": task.id,
                        "title": task.title,
                        "status": task.status,
                        "assignee": task.assignee,
                        "current_run_id": task.current_run_id,
                    },
                    "parent_count": len(parents),
                    "child_count": len(children),
                    "comment_count": len(comments),
                    "event_count": len(events),
                    "run_count": len(runs),
                    "latest_comment": (
                        {
                            "author": comments[-1].author,
                            "body": comments[-1].body,
                            "created_at": comments[-1].created_at,
                        }
                        if comments else None
                    ),
                    "latest_run": (
                        {
                            "id": latest_run.id,
                            "status": latest_run.status,
                            "outcome": latest_run.outcome,
                            "summary": latest_run.summary,
                            "error": latest_run.error,
                        }
                        if latest_run else None
                    ),
                    "instruction": "Use view='full' for bounded history or view='contract' for a compiled work order.",
                }), ensure_ascii=False)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            if str(task.body or "").startswith(
                (
                    "GRACE_LOOP_CONTRACT_STAGE: review",
                    "GRACE_LOOP_CONTRACT_STAGE: grace_review",
                )
            ):
                review_run = (
                    kb.get_run(conn, task.current_run_id)
                    if task.current_run_id is not None
                    else kb.latest_run(conn, tid)
                )
                review_source = (
                    (review_run.metadata or {}).get("workflow_review_source")
                    if review_run is not None
                    else None
                )
                sealed_contract = kb._grace_compiled_contract(task.body or "")
                sealed_contract_json = None
                review_contract = None
                policy_references = []
                review_contract_json = None
                if isinstance(sealed_contract, dict):
                    sealed_contract_json = json.dumps(
                        sealed_contract,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    review_contract = json.loads(sealed_contract_json)
                    for policy in review_contract.get("policy_snapshots", []):
                        if not isinstance(policy, dict):
                            continue
                        policy.pop("content", None)
                        policy_references.append(dict(policy))
                    review_contract_json = json.dumps(
                        review_contract,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                parent_evidence = []
                for parent_id in parents:
                    parent = kb.get_task(conn, parent_id)
                    candidate_runs = [
                        run
                        for run in kb.list_runs(conn, parent_id)
                        if run.outcome == "completed"
                        or (
                            run.outcome == "blocked"
                            and isinstance(run.metadata, dict)
                            and isinstance(
                                run.metadata.get("loop_contract_blocked_result"),
                                dict,
                            )
                        )
                    ]
                    candidate_runs.sort(key=lambda run: run.id, reverse=True)
                    latest = candidate_runs[0] if candidate_runs else None
                    if (
                        isinstance(review_source, dict)
                        and review_source.get("parent_execution_task_id") == parent_id
                    ):
                        latest = kb.reviewed_execution_run(conn, review_run, parent_id)
                    metadata = latest.metadata if latest and latest.metadata else {}
                    blocked_result = (
                        metadata.get("loop_contract_blocked_result")
                        if latest and latest.outcome == "blocked"
                        else None
                    )
                    blocked_metadata = (
                        blocked_result.get("metadata")
                        if isinstance(blocked_result, dict)
                        and isinstance(blocked_result.get("metadata"), dict)
                        else {}
                    )
                    blocked_top_level = (
                        blocked_result if isinstance(blocked_result, dict) else {}
                    )
                    evidence_metadata = {
                        **metadata,
                        **blocked_top_level,
                        **blocked_metadata,
                    }
                    controller_report = kb.grace_inline_content_package_report(
                        conn, parent_id, execution_run=latest,
                    )
                    controller_attachment_readback = (
                        kb.grace_content_package_attachment_readback(
                            conn, parent_id, execution_run=latest,
                        )
                    )
                    controller_content_package_readback = None
                    if isinstance(controller_report, dict):
                        body = controller_report.get("body")
                        controller_content_package_readback = {
                            "package_kind": controller_report.get("package_kind"),
                            "package_complete": controller_report.get("complete"),
                            "delivery_receipt_phase": "post_review",
                            "delivery_receipt_required_before_review": False,
                            "body_sha256": (
                                hashlib.sha256(body.encode("utf-8")).hexdigest()
                                if isinstance(body, str)
                                else None
                            ),
                            "attachment_readback": controller_attachment_readback,
                        }
                    review_evidence_keys = (
                        "artifacts",
                        "deliverables_completed",
                        "generated_images",
                        "package",
                        "policy_receipts",
                        "verification",
                        "external_actions_performed",
                        "external_effects",
                        "domain_memory_deltas",
                        "user_facing_report",
                        "objective_plan_request",
                    )
                    review_evidence = (
                        {
                            key: evidence_metadata[key]
                            for key in review_evidence_keys
                            if key in evidence_metadata
                        }
                        if isinstance(evidence_metadata, dict)
                        else {}
                    )
                    workspace_completion = kb.workspace_completion_evidence(metadata)
                    if workspace_completion is not None:
                        review_evidence["workspace_completion"] = workspace_completion
                    if controller_content_package_readback is not None:
                        review_evidence["controller_content_package_readback"] = (
                            controller_content_package_readback
                        )
                    attachments = [
                        {
                            "filename": attachment.filename,
                            "stored_path": attachment.stored_path,
                            "content_type": attachment.content_type,
                            "size": attachment.size,
                        }
                        for attachment in kb.list_attachments(conn, parent_id)
                    ]
                    objective_readback = None
                    plan_request = metadata.get("objective_plan_request")
                    if isinstance(plan_request, dict) and latest is not None:
                        request = conn.execute(
                            "SELECT objective_id FROM grace_objective_plan_requests "
                            "WHERE request_id=? AND execution_task_id=? AND execution_run_id=?",
                            (plan_request.get("request_id"), parent_id, latest.id),
                        ).fetchone()
                        objective = (
                            kb.get_grace_objective(conn, request["objective_id"])
                            if request is not None
                            else None
                        )
                        if objective is not None:
                            objective_readback = {
                                "objective": {
                                    key: objective[key]
                                    for key in (
                                        "objective_id", "status", "current_stage_key",
                                        "terminal_stage_key", "required_stage_keys", "revision",
                                    )
                                },
                                "stages": [dict(row) for row in conn.execute(
                                    "SELECT stage_key,position,status,delegation_id,"
                                    "execution_task_id,review_task_id,outcome_kind "
                                    "FROM grace_objective_stages WHERE objective_id=? "
                                    "ORDER BY position",
                                    (request["objective_id"],),
                                )],
                            }
                    backend_observation = metadata.get("backend_terminal_observation")
                    backend_result = (
                        backend_observation.get("delegated_result")
                        if isinstance(backend_observation, dict)
                        else None
                    )
                    backend_usage = metadata.get("backend_token_usage")
                    protocol_correlated = (
                        backend_result.get("protocol_correlated") is True
                        if isinstance(backend_result, dict)
                        else False
                    )
                    effect_records = kb.list_external_effects(conn, parent_id)
                    from hermes_cli.controller_readback import task_controller_readback
                    controller_readback = task_controller_readback(conn, parent_id)
                    from hermes_cli.controller_readback import compare_native_history_baseline, execution_history_baseline
                    native_contract = metadata.get('loop_contract')
                    native_snapshot = (native_contract.get('durable_evidence_snapshot')
                        if isinstance(native_contract, Mapping) else None)
                    native_snapshot = native_snapshot if isinstance(native_snapshot, Mapping) else {}
                    history_comparison = compare_native_history_baseline(
                        controller_readback,
                        execution_history_baseline(conn, parent_id, latest.id)
                        if latest else None,
                        execution_run_id=latest.id if latest else None,
                    )
                    if controller_readback["binding_verified"]:
                        objective_readback = {
                            "objective": controller_readback["objective"],
                            "stages": controller_readback["stages"],
                            "terminal_completion_phase": "post_review_and_delivery",
                            "terminal_completion_required_before_review": False,
                        }
                    parent_contract = kb._grace_compiled_contract(parent.body or "") if parent else None
                    effect_budget = (
                        parent_contract.get("external_effect_budget")
                        if isinstance(parent_contract, Mapping)
                        else None
                    )
                    sealed_zero_effect_budget = (
                        type(effect_budget) is int and effect_budget == 0
                    ) or (
                        isinstance(effect_budget, Mapping)
                        and type(effect_budget.get("max_effects")) is int
                        and effect_budget["max_effects"] == 0
                    )
                    controller_zero_effect_budget = False
                    if (
                        parent
                        and parent.executor_backend == "openclaw"
                        and isinstance(parent_contract, Mapping)
                        and type(metadata.get("external_effect_budget")) is int
                        and metadata["external_effect_budget"] == 0
                    ):
                        from proactive.loop_contract import contract_fingerprint

                        controller_zero_effect_budget = (
                            metadata.get("execution_card_fingerprint")
                            == contract_fingerprint(parent_contract)
                        )
                    parent_evidence.append({
                        "task_id": parent_id,
                        "status": parent.status if parent else "missing",
                        "summary": (
                            blocked_result.get("summary")
                            if isinstance(blocked_result, dict)
                            else latest.summary if latest else None
                        ),
                        "evidence_mode": (
                            "blocked_draft"
                            if isinstance(blocked_result, dict)
                            else "completed"
                        ),
                        "acceptance_evidence": (
                            blocked_result.get("acceptanceEvidence")
                            if isinstance(blocked_result, dict)
                            else metadata.get("acceptance_evidence")
                            if isinstance(metadata, dict)
                            else None
                        ),
                        "external_effects": (
                            blocked_result.get("externalEffects")
                            if isinstance(blocked_result, dict)
                            else metadata.get("external_effects")
                            if isinstance(metadata, dict)
                            else None
                        ),
                        "review_evidence": review_evidence,
                        "objective_readback": objective_readback,
                        "controller_readback": controller_readback,
                        "controller_history_comparison": history_comparison,
                        "user_facing_report": (
                            controller_report
                            if isinstance(controller_report, dict)
                            else evidence_metadata.get("user_facing_report")
                        ),
                        "controller_content_package_readback": (
                            controller_content_package_readback
                        ),
                        "declared_artifacts": (
                            blocked_result.get("artifacts")
                            if isinstance(blocked_result, dict)
                            else None
                        ),
                        "attachments": attachments,
                        "backend_run_id": latest.backend_run_id if latest else None,
                        "backend_receipt": (
                            {
                                "protocol_version": latest.protocol_version,
                                "delegation_id": metadata.get("delegation_id"),
                                "attempt_id": metadata.get("attempt_id"),
                                "backend_run_id": latest.backend_run_id,
                                "task_idempotency_key": parent.idempotency_key if parent else None,
                                "idempotency_key": metadata.get("start_idempotency_key"),
                                "backend_agent_id": latest.backend_agent_id,
                                "backend_status": latest.backend_status,
                                "result_digest": latest.result_digest,
                                "observed_at": latest.ended_at,
                                "protocol_correlated": protocol_correlated,
                                "actual_model": (
                                    backend_usage.get("model")
                                    if protocol_correlated
                                    and latest.backend_run_id
                                    and latest.backend_agent_id
                                    and isinstance(backend_usage, dict)
                                    and backend_usage.get("source") == "openclaw-transcript"
                                    else None
                                ),
                                "token_usage": backend_usage,
                            }
                            if latest and latest.protocol_version == "2.0"
                            else None
                        ),
                        "external_effect_ledger": (
                            {
                                "task_id": parent_id,
                                "scope": "task",
                                "observed_run_id": latest.id,
                                "checked_at": int(time.time()),
                                "records": effect_records,
                                "controller_zero_effect_check": (
                                    latest.status == "done"
                                    and (
                                        (
                                            sealed_zero_effect_budget
                                            and metadata.get("read_only_zero_external_effects") is True
                                        )
                                        or controller_zero_effect_budget
                                    )
                                    and metadata.get("external_effects") == []
                                    and not effect_records
                                ),
                            }
                            if latest else None
                        ),
                        "run_id": latest.id if latest else None,
                    })
                review_payload = {
                    "task": {
                        "id": task.id,
                        "title": task.title,
                        "status": task.status,
                        "current_run_id": task.current_run_id,
                    },
                    "parent_evidence": parent_evidence,
                    "review_contract": review_contract,
                    "review_contract_sha256": (
                        hashlib.sha256(review_contract_json.encode("utf-8")).hexdigest()
                        if review_contract_json is not None else None
                    ),
                    "sealed_contract_sha256": (
                        hashlib.sha256(sealed_contract_json.encode("utf-8")).hexdigest()
                        if sealed_contract_json is not None else None
                    ),
                    "policy_references": policy_references,
                    "policy_read_instruction": (
                        "Read every pinned policy independently with managed_policy_read; "
                        "policy content is intentionally omitted from this compact payload."
                    ),
                    "comments": [
                        {
                            "author": comment.author,
                            "body": comment.body,
                            "created_at": comment.created_at,
                        }
                        for comment in comments[-10:]
                    ],
                    "recent_events": [
                        {
                            "kind": event.kind,
                            "payload": event.payload,
                            "created_at": event.created_at,
                            "run_id": event.run_id,
                        }
                        for event in events[-10:]
                    ],
                    "runs": [_run_dict(run) for run in runs[-5:]],
                    "view": "grace_review_compact",
                }
                review_json = json.dumps(
                    review_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                try:
                    offset = int(args.get("offset", 0))
                    limit = int(args.get("limit", 12_000))
                except (TypeError, ValueError):
                    return tool_error(
                        "kanban_show review evidence: offset and limit must be integers"
                    )
                if offset < 0 or limit < 1 or limit > 12_000:
                    return tool_error(
                        "kanban_show review evidence: offset must be non-negative and "
                        "limit must be between 1 and 12000"
                    )
                if offset > len(review_json):
                    return tool_error(
                        "kanban_show review evidence: offset exceeds evidence length"
                    )
                if offset == 0 and len(review_json) <= limit:
                    return json.dumps(review_payload, ensure_ascii=False)
                end = min(len(review_json), offset + limit)
                while True:
                    response = {
                        "view": "grace_review_evidence_chunk",
                        "task": {
                            "id": task.id,
                            "title": task.title,
                            "status": task.status,
                            "current_run_id": task.current_run_id,
                        },
                        "review_evidence_json_chunk": review_json[offset:end],
                        "offset": offset,
                        "next_offset": end,
                        "total_chars": len(review_json),
                        "complete": end == len(review_json),
                        "review_evidence_json_sha256": hashlib.sha256(
                            review_json.encode("utf-8")
                        ).hexdigest(),
                        "pagination_instruction": (
                            "Concatenate review_evidence_json_chunk values in offset "
                            "order; call kanban_show with offset=next_offset until complete."
                        ),
                    }
                    output = json.dumps(response, ensure_ascii=False)
                    if len(output) <= 18_000 or end <= offset + 1:
                        return output
                    end = offset + max(1, (end - offset) // 2)

            # Large contracts can truncate the tool preview. Keep the latest
            # recovery evidence visible before the full task body; comments
            # remain worker-authored evidence, not trusted instructions.
            return json.dumps(_bound_show_value({
                "latest_comment": (
                    {"author": comments[-1].author, "body": comments[-1].body,
                     "created_at": comments[-1].created_at} if comments else None
                ),
                "comments": [
                    {"author": c.author, "body": c.body,
                     "created_at": c.created_at} for c in comments
                ],
                "task": _task_dict(task),
                "parents": parents,
                "children": children,
                "events": [
                    {"kind": e.kind, "payload": e.payload,
                     "created_at": e.created_at, "run_id": e.run_id}
                    for e in events[-50:]   # cap; full log via CLI
                ],
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            }), ensure_ascii=False)
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_domain_inventory(args: dict, **kw) -> str:
    """Read the typed operational registry with explicit coverage status."""
    domain_key = str(args.get("domain_key") or "").strip()
    if not domain_key:
        return tool_error("domain_key is required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            entity_type = str(args.get("entity_type") or "").strip()
            parameter_source = "tool_arguments"

            # A dispatched Domain Memory worker already has an immutable typed
            # contract in its own task body.  Bind the read to that contract so
            # a model omission cannot silently widen the query, and reject a
            # conflicting value instead of trusting worker prose to notice it.
            task_id = _default_task_id(None)
            if task_id:
                task = kb.get_task(conn, task_id)
                body = str(task.body or "") if task is not None else ""
                marker = "```json"
                start = body.find(marker)
                end = body.rfind("```")
                if start >= 0 and end > start + len(marker):
                    try:
                        contract = json.loads(
                            body[start + len(marker):end].strip()
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        contract = None
                    domain_memory = (
                        contract.get("domain_memory")
                        if isinstance(contract, dict)
                        else None
                    )
                    if isinstance(domain_memory, dict):
                        contract_domain = str(
                            domain_memory.get("domain_key") or ""
                        ).strip()
                        contract_entity_type = str(
                            domain_memory.get("entity_type") or ""
                        ).strip()
                        if contract_domain and domain_key != contract_domain:
                            return tool_error(
                                "kanban_domain_inventory: domain_key conflicts "
                                f"with the sealed task contract ({contract_domain})"
                            )
                        if (
                            entity_type
                            and contract_entity_type
                            and entity_type != contract_entity_type
                        ):
                            return tool_error(
                                "kanban_domain_inventory: entity_type conflicts "
                                "with the sealed task contract "
                                f"({contract_entity_type})"
                            )
                        if not entity_type and contract_entity_type:
                            entity_type = contract_entity_type
                            parameter_source = "sealed_task_contract"

            if not entity_type:
                return tool_error(
                    "kanban_domain_inventory: entity_type is required when "
                    "the current task has no typed Domain Memory contract"
                )

            report = kb.domain_inventory_report(
                conn,
                domain_key=domain_key,
                entity_type=entity_type,
                expected_total=args.get("expected_total"),
            )
            report["effective_query"] = {
                "domain_key": domain_key,
                "entity_type": entity_type,
                "expected_total": args.get("expected_total"),
                "parameter_source": parameter_source,
            }
            return json.dumps(report, ensure_ascii=False, sort_keys=True)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_domain_inventory: {e}")
    except Exception as e:
        logger.exception("kanban_domain_inventory failed")
        return tool_error(f"kanban_domain_inventory: {e}")


def _handle_domain_episode_reserve(args: dict, **kw) -> str:
    """Reserve the contract-authorized next episode under an atomic DB lock."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    run_id = _worker_run_id(tid)
    if run_id is None:
        return tool_error(
            "kanban_domain_episode_reserve requires a dispatcher-owned active run"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            role = kb.validate_grace_loop_worker_auth(
                conn,
                task_id=tid,
                run_id=str(run_id),
                claim_lock=os.environ.get("HERMES_KANBAN_CLAIM_LOCK", ""),
                worker_auth_token=os.environ.get(
                    "HERMES_KANBAN_WORKER_AUTH_TOKEN", ""
                ),
            )
            if role != "execution":
                return tool_error(
                    "kanban_domain_episode_reserve requires an authenticated "
                    "Grace execution worker"
                )
            receipt = kb.reserve_next_domain_episode(
                conn,
                task_id=tid,
                expected_run_id=run_id,
            )
            return _ok(reservation=receipt)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_domain_episode_reserve: {e}")
    except Exception as e:
        logger.exception("kanban_domain_episode_reserve failed")
        return tool_error(f"kanban_domain_episode_reserve: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    metadata_path = args.get("metadata_path")
    workspace_completion_source = None
    result = args.get("result")
    if metadata is not None and metadata_path is not None:
        return tool_error("provide only one of: metadata, metadata_path")
    if metadata_path is not None:
        try:
            _validate_workspace_file_completion_contract(tid)
            metadata, workspace_completion_source = _load_worker_completion_metadata(
                metadata_path
            )
        except ValueError as exc:
            return tool_error(f"kanban_complete: {exc}")
    if summary:
        summary = redact_sensitive_text(str(summary), force=True)
    if result:
        result = redact_sensitive_text(str(result), force=True)
    if metadata is not None and isinstance(metadata, dict):
        metadata.pop(WORKSPACE_COMPLETION_RECEIPT_KEY, None)
        meta_json = json.dumps(metadata)
        meta_json = redact_sensitive_text(meta_json, force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                    workspace_completion_source=workspace_completion_source,
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            promotion_results: list[dict] = []
            try:
                from proactive.grace_memory_promotion import (
                    process_due_promotions,
                )

                promotion_results = process_due_promotions(
                    board=board,
                    task_id=tid,
                    limit=1,
                )
            except Exception:
                # The transactional outbox already committed with the task.
                # Heartbeat retry will resume it; completion itself remains valid.
                logger.exception(
                    "immediate Grace memory promotion failed for %s; queued for retry",
                    tid,
                )
            payload = {
                "task_id": tid,
                "run_id": run.id if run else None,
            }
            if promotion_results:
                payload["memory_promotion"] = promotion_results[0]
            return _ok(**payload)
        finally:
            conn.close()
    except ValueError as e:
        message = str(e)
        if message == "behavior.pinned_version_not_verified":
            message += (": verify each pinned policy version and SHA, then record "
                        "metadata.policy_receipts[i].pinned_version_verified=true for each review receipt.")
        elif (
            "domain mutation requires at least one domain_memory_delta" in message
            or "domain mutation requires structured metadata.domain_memory_deltas" in message
        ):
            message += (
                ": pass metadata.domain_memory_deltas as a non-empty list (plural key). "
                "Each delta must explicitly include every contract artifact slot. "
                "For a reserved episode artifact, put the exact "
                "domain_episode_reservation:<reservation_id> value in both the "
                "artifact.evidence_ref and its enclosing delta.evidence_refs list."
            )
        elif "For text-only reviews, do not add page_hero" in message:
            # Frozen validators use this generic fallback; it does not classify
            # the task as text-only or authorize dropping required asset evidence.
            message = ("Grace review completion metadata lacks a consistent accepted verdict/evidence. "
                       "After verifying acceptance, set metadata.review_outcome='accepted' and retain "
                       "all contract-required evidence, including Page Hero/visual review fields. "
                       "Do not remove required evidence to satisfy this error.")
        return tool_error(f"kanban_complete: {message}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    reason = redact_sensitive_text(str(reason), force=True)
    kind = args.get("kind")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        if kind is not None and kind not in kb.VALID_BLOCK_KINDS:
            conn.close()
            return tool_error(
                f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)"
            )
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                kind=kind,
                expected_run_id=_worker_run_id(tid),
                review_outcome=args.get("review_outcome"),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            # Tell the worker where the task actually landed so it doesn't
            # assume it's sitting in 'blocked' when routing sent it elsewhere.
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "blocked",
                block_kind=kind,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_external_effect(args: dict, **kw) -> str:
    """Record read-back evidence that controls external create idempotency."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    try:
        kb, conn = _connect(board=args.get("board"))
        try:
            effect = kb.record_external_effect(
                conn,
                tid,
                platform=str(args.get("platform") or ""),
                state=str(args.get("state") or ""),
                effect_key=str(args.get("effect_key") or "create"),
                external_id=args.get("external_id"),
                details=args.get("details"),
                expected_run_id=_worker_run_id(tid),
            )
            return _ok(task_id=tid, external_effect=effect)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_external_effect: {e}")
    except Exception as e:
        logger.exception("kanban_external_effect failed")
        return tool_error(f"kanban_external_effect: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    body = redact_sensitive_text(str(body), force=True)
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    session_id = args.get("session_id") or os.environ.get("HERMES_SESSION_ID")
    priority = args.get("priority")
    # Resolve workspace. If the caller passed one explicitly, honor it.
    # Otherwise, a dispatcher-spawned worker (HERMES_KANBAN_TASK set)
    # inherits its own running task's workspace, so a worker editing a
    # dir:/worktree project that spawns a follow-up child keeps the child
    # in that project instead of a throwaway scratch dir. Orchestrators
    # (kanban toolset, no HERMES_KANBAN_TASK) and CLI/dashboard callers
    # fall back to scratch as before. Explicit None path stays None.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    project_id = args.get("project") or args.get("project_id")
    _inherit_workspace = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status") or "running"
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
    goal_max_turns = args.get("goal_max_turns")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Inherit the spawning worker's own task workspace when the
            # caller didn't specify one (see resolution note above).
            if _inherit_workspace:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.workspace_kind:
                        workspace_kind = _self_task.workspace_kind
                        workspace_path = _self_task.workspace_path
                        # Keep follow-up children inside the same project so the
                        # whole subtree shares one repo + branch convention.
                        if project_id is None and _self_task.project_id:
                            project_id = _self_task.project_id
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                project_id=project_id,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                initial_status=str(initial_status),
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
            )
            new_task = kb.get_task(conn, new_tid)
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
                subscribed=subscribed,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the calling session to task completion / block events.

    Returns True if a subscription row was written, False otherwise (no
    session context, config gate disabled, or best-effort failure). The
    caller surfaces this in the ``subscribed`` field of the kanban_create
    response so an orchestrator can decide whether to fall back to an
    explicit ``kanban_notify-subscribe`` or to polling.

    Gated by ``kanban.auto_subscribe_on_create`` in config.yaml (default
    True). Disable to mirror pre-feature behaviour, e.g. when the
    originating user/chat opted out via the per-platform notification
    toggle (see ``hermes dashboard``).

    Subscription paths:

    - **Gateway** (telegram/discord/slack/etc): ``HERMES_SESSION_PLATFORM``
      and ``HERMES_SESSION_CHAT_ID`` are set in ContextVars by the
      messaging gateway before agent dispatch. The notification poller
      already keys off these, so we just register a row.

    - **TUI** (herm desktop / herm TUI): the platform/chat_id ContextVars
      are intentionally cleared (TUI is a single-channel local UI, not
      a multi-tenant chat surface), but the agent subprocess inherits
      ``HERMES_SESSION_KEY`` from the parent session. We subscribe with
      ``platform="tui"`` and ``chat_id=<key>``; the TUI notification
      poller (``tui_gateway/server.py``) reads ``kanban_notify_subs``
      for these rows and posts the completion message into the running
      session.

    - **CLI / cron / test / unattached**: no persistent delivery channel,
      no-op.

    Failure mode: any exception inside the function is logged at WARNING
    with the offending exception + diagnostic env vars and swallowed.
    We never want a notification bookkeeping failure to fail the
    kanban_create that the agent is mid-conversation about.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # If config can't load we still default to True — this is the
        # user-friendly behaviour that mirrors the pre-gate implementation.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            # TUI / desktop fallback: platform/chat_id ContextVars are
            # cleared for TUI sessions, but the parent process exports
            # HERMES_SESSION_KEY into the subprocess env. Treat that
            # as a "tui" subscription so the TUI notification poller
            # (tui_gateway/server.py) can pick it up.
            #
            # HERMES_SESSION_ID is intentionally NOT a fallback here:
            # it is set by ACP / the agent subprocess for telemetry
            # regardless of whether the parent is a TUI or a CLI, so
            # treating it as a notification target would auto-subscribe
            # every CLI invocation, which is exactly the over-eager
            # behaviour that got #19718 reverted upstream. The TUI
            # poller keys on HERMES_SESSION_KEY.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False  # CLI / cron / test — no persistent channel
            platform = "tui"
            chat_id = session_key
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        notifier_profile = os.environ.get("HERMES_PROFILE")

        # Lazy-import to keep the module-level dependency light
        from hermes_cli import kanban_db as _kb
        _kb.add_notify_sub(
            conn, task_id=task_id,
            platform=platform, chat_id=chat_id,
            thread_id=thread_id, user_id=user_id,
            notifier_profile=notifier_profile,
        )
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, platform, bool(chat_id),
        )
        return False


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task back to ready."""
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            return _ok(task_id=str(tid), status="ready")
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "For post-delivery verification, use view='delivery', source_review_task_id, "
        "and exact message_ids. This read-only view authenticates the current worker, "
        "uses its live Kanban board, and compares provider receipts with the exact "
        "canonical text/image digests. No SQL, shell, state.db, or browser is needed. "
        "The source execution/review pair must be named in the current sealed allowed scope. "
        "Execution workers reading their own task receive a lossless, paginated canonical "
        "work order including pinned policy snapshots but without task history. "
        "Grace review cards return lossless canonical parent evidence, paginated when needed. "
        "Other summary views are bounded. Use view='full' explicitly to read bounded task state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "view": {
                "type": "string", "enum": ["summary", "full", "contract", "delivery", "controller"],
                "description": "An execution worker reading its own task defaults to the contract view; other reads default to summary. Use contract for the complete paginated work order including policy snapshots, full explicitly for bounded stored body/history, or delivery for provider-receipt verification.",
            },
            "source_review_task_id": {
                "type": "string",
                "description": "For delivery view: the exact accepted source review task named in allowed scope.",
            },
            "message_ids": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 100,
                "description": "For delivery view: exact provider message IDs to verify, without ranges.",
            },
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "offset": {
                "type": "integer", "minimum": 0,
                "description": "For contract or Grace-review evidence: zero-based offset into the complete canonical JSON.",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 12000,
                "description": "For contract or Grace-review evidence: maximum canonical JSON characters requested per page.",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_DOMAIN_INVENTORY_SCHEMA = {
    "name": "kanban_domain_inventory",
    "description": (
        "Read the complete typed operational registry for one business domain, "
        "such as solobizai or secondhand. Use this before answering which items, "
        "cases, posts, podcasts, or listings exist and what their status is. "
        "Returns every entity and artifact plus registry_total, expected_total, "
        "missing required artifact types, and an explicit coverage_status. "
        "Semantic memory and session search do not replace this inventory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "domain_key": {
                "type": "string",
                "description": "Stable domain key, for example solobizai or secondhand.",
            },
            "entity_type": {
                "type": "string",
                "description": (
                    "Required exact entity type such as SoloBizAiCase or "
                    "ResaleItem. A task-scoped typed Domain Memory contract "
                    "also binds and verifies this value at execution time."
                ),
            },
            "expected_total": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Optional independently verified expected count for an ad hoc check. "
                    "Omit to use the persisted certified baseline; if no baseline exists, "
                    "the result reports unknown_expected_total."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["domain_key", "entity_type"],
        "additionalProperties": False,
    },
}

KANBAN_DOMAIN_EPISODE_RESERVE_SCHEMA = {
    "name": "kanban_domain_episode_reserve",
    "description": (
        "Atomically reserve the next internal episode number authorized by the "
        "current sealed Grace Loop Contract. The worker never supplies or chooses "
        "the number: the controller verifies pinned accepted occupancy evidence, "
        "the certified Domain Registry and prior reservations, requires a contiguous "
        "sequence, then returns the durable reservation and immediate readback. "
        "Use this before generating any episode-labelled asset when the contract "
        "contains HERMES_DOMAIN_EPISODE_RESERVATION_V2. This performs no external "
        "platform action and grants no publication authority."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
        "additionalProperties": False,
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc), or use ``metadata_path`` "
        "for a large JSON object already written inside the current task "
        "workspace only when the compiled contract declares "
        "completion_handoff.metadata_source=workspace_file. At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``. For an execution contract "
                    "whose domain_memory mode is mutate, include a non-empty "
                    "metadata.domain_memory_deltas list (plural key). Each delta "
                    "must explicitly represent every artifact slot declared by "
                    "the contract. A reserved episode artifact must use the exact "
                    "domain_episode_reservation:<reservation_id> string in both "
                    "artifact.evidence_ref and the enclosing delta.evidence_refs list."
                ),
            },
            "metadata_path": {
                "type": "string",
                "description": (
                    "Optional path to a UTF-8 JSON object already written "
                    "inside the current task workspace. This is accepted only "
                    "when the compiled contract declares "
                    "completion_handoff.metadata_source=workspace_file on a "
                    "devops/clawops-dev route with terminal capability. Use this "
                    "instead of metadata for large structured handoffs; traversal, "
                    "symlink escapes, non-object JSON, and files over "
                    "1,000,000 bytes are rejected. Do not pass both metadata "
                    "and metadata_path."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk when the notifier runs; missing files "
                    "are silently skipped."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Stop work on this task and route it according to WHY you're stuck. "
        "Set ``kind`` to say which: 'dependency' (waiting on another task — "
        "goes to todo and auto-resumes when that task finishes, no human "
        "needed), 'needs_input' (you need a human decision/answer), "
        "'capability' (a hard wall: no access, missing credentials, an action "
        "no agent can do), or 'transient' (a flaky failure that may clear). "
        "``reason`` is shown to the human on the board. If a task keeps "
        "getting unblocked and re-blocked for the same reason, it is "
        "auto-escalated to triage. Use for genuine blockers only — don't "
        "block on things you can resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered or what stopped you, in one or "
                    "two sentences. Don't paste the whole conversation; the "
                    "human has the board and can ask follow-ups via comments."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["dependency", "needs_input", "capability", "transient"],
                "description": (
                    "Why you're blocked. 'dependency' waits in todo and "
                    "resumes automatically; the others surface to a human. "
                    "Omit only if none apply."
                ),
            },
            "review_outcome": {
                "type": "string", "enum": ["rejected"],
                "description": "Only for a formal Grace review: record a canonical rejected verdict on the blocked run. Never use for an accepted review.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_EXTERNAL_EFFECT_SCHEMA = {
    "name": "kanban_external_effect",
    "description": (
        "Record a read-only reconciliation or verified external effect for "
        "this exact task and platform. Grace correction runs must call this "
        "before opening a protected create route. Use absent_verified only "
        "after a real read-only search found no matching object; use existing, "
        "created, or verified as soon as the object is read back. Durable "
        "existing/created/verified evidence prevents duplicate creation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "platform": {
                "type": "string",
                "description": (
                    "Stable platform slug, such as facebook, shopee, tasker, "
                    "openclaw, or generic. Facebook group effects still require "
                    "platform=facebook and effect_key=group:<numeric-id>."
                ),
            },
            "state": {
                "type": "string",
                "enum": [
                    "absent_verified",
                    "existing",
                    "created",
                    "verified",
                    "not_joined_verified",
                    "join_started",
                    "joined",
                    "pending_approval",
                    "needs_questions",
                    "failed",
                ],
                "description": (
                    "Observed state. absent_verified unlocks one create route "
                    "for this correction run. Join states require a non-create "
                    "effect_key scoped to one exact group."
                ),
            },
            "effect_key": {
                "type": "string",
                "description": (
                    "Stable object key within the platform. Defaults to create "
                    "for product/draft idempotency; use group:<group_id> for "
                    "Facebook membership effects."
                ),
                "default": "create",
            },
            "external_id": {
                "type": "string",
                "description": (
                    "Platform object/product/draft id when the UI exposes one."
                ),
            },
            "details": {
                "type": "object",
                "description": (
                    "Small machine-readable readback facts such as title, "
                    "status, URL, image count, and published=false."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["platform", "state"],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the task to. When "
                    "set, the task becomes a git worktree under the project's "
                    "primary repo with a deterministic branch (project slug + "
                    "task id), instead of a random branch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker. The kanban lifecycle is already injected "
                    "automatically; use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Move a blocked Kanban task back to ready. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to return to ready.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
    max_result_size_chars=20_000,
)

registry.register(
    name="kanban_domain_inventory",
    toolset="kanban",
    schema=KANBAN_DOMAIN_INVENTORY_SCHEMA,
    handler=_handle_domain_inventory,
    check_fn=_check_kanban_mode,
    emoji="🗂️",
)

registry.register(
    name="kanban_domain_episode_reserve",
    toolset="kanban",
    schema=KANBAN_DOMAIN_EPISODE_RESERVE_SCHEMA,
    handler=_handle_domain_episode_reserve,
    check_fn=_check_kanban_mode,
    emoji="🔒",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_external_effect",
    toolset="kanban",
    schema=KANBAN_EXTERNAL_EFFECT_SCHEMA,
    handler=_handle_external_effect,
    check_fn=_check_kanban_mode,
    emoji="🧾",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)
