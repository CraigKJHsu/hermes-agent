"""Recover an exact user-authorized Grace Review retry after gateway restart."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from hermes_cli import kanban_db as kb


_MANAGED_POLICY_BLOCK_REASON = (
    "managed_policy_read 回報 Topic policy binding not found；"
    "runtime capability fault"
)
_MAX_CLOCK_SKEW_SECONDS = 5.0


def _runtime_source_block_reason(review_task_id: str) -> str:
    return (
        "Grace 實質驗收已通過，但 runtime integrity gate 回覆 "
        "`Kanban review runtime source changed`；請重啟 dispatcher/gateway "
        f"後重試同一卡 {review_task_id}，不要另建 delegation。"
    )



def is_runtime_source_block_reason(reason: str, review_task_id: str) -> bool:
    """Match only the known sole runtime-integrity fault, never composite blockers."""
    if reason == _runtime_source_block_reason(review_task_id):
        return True
    return re.fullmatch(
        r"Formal review evidence passed and the accepted decision is recorded in comment [0-9]+, "
        r"but kanban_complete is blocked by control-plane integrity error: "
        r"`Kanban review runtime source changed; restart the dispatcher/gateway before claiming "
        r"or completing workflow reviews`. Operator must restart dispatcher/gateway, "
        r"then reclaim and complete this accepted review; do not treat it as rejected\.",
        reason,
    ) is not None


def _state_path(value: str | Path | None) -> Path:
    return Path(
        value
        or os.getenv("HERMES_STATE_DB")
        or Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "state.db"
    ).expanduser()


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _native_call_id(call: dict[str, Any]) -> str | None:
    values: list[str] = []
    for field in ("id", "call_id"):
        if field not in call:
            continue
        value = call[field]
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        values.append(value)
    if not values or any(value != values[0] for value in values[1:]):
        return None
    return values[0]


def _strip_display_name(text: str) -> str:
    match = re.match(r"^\[[^\]\n]{1,100}\]\s+(.*)$", text, re.S)
    return match.group(1) if match is not None else text


def _explicit_retry(text: str, review_task_id: str) -> bool:
    task_id = re.escape(review_task_id)
    review_ref = rf"(?:Grace\s*)?Review\s*`?{task_id}`?"
    no_new_work = (
        r"(?:[，,]\s*(?:不要|不得)\s*(?:另建|建立(?:新(?:的)?)?)\s*"
        r"(?:delegation|委派|execution|執行))?"
    )
    chinese = (
        r"\s*(?:(?:請|麻煩)(?:幫我)?|幫我)?\s*(?:只\s*)?"
        r"(?:重試|重新執行|重新驗收|解除阻擋|繼續處理)\s*"
        rf"(?:原(?:本)?\s*)?{review_ref}{no_new_work}\s*[。.!！]?\s*"
    )
    english = (
        r"\s*(?:please\s+)?(?:only\s+)?(?:retry|re[-\s]?run|unblock)\s+"
        rf"(?:the\s+)?(?:original\s+)?Grace\s+Review\s+`?{task_id}`?"
        r"(?:\s*,\s*(?:do\s+not|don't)\s+create\s+(?:a\s+)?new\s+"
        r"(?:delegation|execution))?\s*[.!]?\s*"
    )
    return bool(
        re.fullmatch(chinese, text, re.IGNORECASE)
        or re.fullmatch(english, text, re.IGNORECASE)
    )


def _pending_call(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    chat_type: str,
    thread_id: str,
    user_id: str,
    freshness_seconds: float,
) -> dict[str, Any] | None:
    now = time.time()
    session = conn.execute(
        "SELECT source,user_id,session_key,chat_id,chat_type,thread_id "
        "FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if session is None:
        return None
    stored_lane = tuple(
        session[field]
        for field in (
            "source",
            "user_id",
            "session_key",
            "chat_id",
            "chat_type",
            "thread_id",
        )
    )
    if not all(isinstance(value, str) for value in stored_lane) or stored_lane != (
        platform,
        user_id,
        session_key,
        chat_id,
        chat_type,
        thread_id,
    ):
        return None
    candidate = conn.execute(
        "SELECT id,role,tool_calls,timestamp FROM messages "
        "WHERE session_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if (
        candidate is None
        or candidate["role"] != "assistant"
        or candidate["tool_calls"] is None
        or not _finite_number(candidate["timestamp"])
        or candidate["timestamp"] > now + _MAX_CLOCK_SKEW_SECONDS
    ):
        return None
    try:
        calls = json.loads(candidate["tool_calls"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(calls, list) or len(calls) != 1:
        return None
    call = calls[0] if isinstance(calls[0], dict) else {}
    if call.get("type") != "function":
        return None
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != (
        "clawops_retry_review"
    ):
        return None
    try:
        args = json.loads(function.get("arguments") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(args, dict):
        return None
    raw_review_id = args.get("review_task_id")
    raw_call_id = _native_call_id(call)
    if not isinstance(raw_review_id, str) or not isinstance(raw_call_id, str):
        return None
    review_id = raw_review_id.strip()
    call_id = raw_call_id.strip()
    if (
        set(args) != {"review_task_id"}
        or not review_id
        or not call_id
        or review_id != raw_review_id
        or call_id != raw_call_id
    ):
        return None

    unresolved_retry_calls: list[tuple[int, str]] = []
    seen_call_ids: set[str] = set()
    rows = conn.execute(
        "SELECT id,tool_calls FROM messages WHERE session_id=? "
        "AND role='assistant' AND active=1 AND tool_calls IS NOT NULL",
        (session_id,),
    ).fetchall()
    for row in rows:
        try:
            row_calls = json.loads(row["tool_calls"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(row_calls, list) or not row_calls:
            return None
        for row_call in row_calls:
            if not isinstance(row_call, dict):
                return None
            if row_call.get("type") != "function":
                return None
            row_function = row_call.get("function")
            if not isinstance(row_function, dict):
                return None
            row_name = row_function.get("name")
            if not isinstance(row_name, str) or not row_name:
                return None
            row_call_id = _native_call_id(row_call)
            if row_call_id is None or row_call_id in seen_call_ids:
                return None
            seen_call_ids.add(row_call_id)
            if row_name != "clawops_retry_review":
                continue
            tool_results = conn.execute(
                "SELECT id,tool_name FROM messages WHERE session_id=? "
                "AND role='tool' AND active=1 AND tool_call_id=? ORDER BY id",
                (session_id, row_call_id),
            ).fetchall()
            if not tool_results:
                unresolved_retry_calls.append((int(row["id"]), row_call_id))
                continue
            if (
                len(tool_results) != 1
                or int(tool_results[0]["id"]) <= int(row["id"])
                or tool_results[0]["tool_name"] != "clawops_retry_review"
            ):
                return None
    if unresolved_retry_calls != [(int(candidate["id"]), call_id)]:
        return None

    user = conn.execute(
        "SELECT id,role,content,timestamp FROM messages "
        "WHERE session_id=? AND active=1 AND id<? ORDER BY id DESC LIMIT 1",
        (session_id, int(candidate["id"])),
    ).fetchone()
    if user is None or user["role"] != "user":
        return None
    text = _strip_display_name(str(user["content"] or ""))
    if (
        not _explicit_retry(text, review_id)
        or not _finite_number(user["timestamp"])
        or user["timestamp"] > candidate["timestamp"]
    ):
        return None
    return {
        "session_id": session_id,
        "review_task_id": review_id,
        "call_id": call_id,
        "message_id": f"gateway-resume:{session_id}:{call_id}",
        "message_text": text,
        "user_timestamp": float(user["timestamp"]),
        "authorization_fresh": (
            float(user["timestamp"]) >= now - freshness_seconds
            and float(candidate["timestamp"]) >= now - freshness_seconds
        ),
    }


def _resolve_board(
    review_task_id: str,
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
) -> str | None:
    matches: list[str] = []
    for metadata in kb.list_boards(include_archived=False):
        board = str(metadata.get("slug") or kb.DEFAULT_BOARD)
        try:
            with kb.connect_closing(board=board) as conn:
                row = conn.execute(
                    "SELECT 1 FROM grace_delegations WHERE review_task_id=? "
                    "AND platform=? AND chat_id=? AND thread_id=? LIMIT 1",
                    (review_task_id, platform, chat_id, thread_id),
                ).fetchone()
        except (OSError, sqlite3.Error, ValueError):
            return None
        if row is not None:
            matches.append(board)
    return matches[0] if len(matches) == 1 else None


def _retry(
    pending: dict[str, Any],
    *,
    platform: str,
    chat_id: str,
    thread_id: str,
    user_id: str,
    owner_user_id: str,
    board: str | None,
) -> dict[str, Any] | None:
    review_id = pending["review_task_id"]
    resolved_board = _resolve_board(
        review_id,
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    if resolved_board is None or (board is not None and board != resolved_board):
        return None
    with kb.connect_closing(board=resolved_board) as conn, kb.write_txn(conn):
        row = conn.execute(
            """
            SELECT d.delegation_id,d.execution_task_id,d.state,
                   execution.status AS execution_status,
                   execution.body AS execution_body,
                   review.status AS review_status,
                   review.block_kind,review.current_run_id,
                   review.executor_profile AS review_executor_profile,
                   review.body AS review_body,
                   callback.user_id AS origin_user_id,
                   callback.platform AS callback_platform,
                   callback.chat_id AS callback_chat_id,
                   callback.thread_id AS callback_thread_id
              FROM grace_delegations d
              JOIN tasks execution ON execution.id=d.execution_task_id
              JOIN tasks review ON review.id=d.review_task_id
              JOIN task_links parent_link
                ON parent_link.parent_id=d.execution_task_id
               AND parent_link.child_id=d.review_task_id
              JOIN grace_loop_callbacks callback
                ON callback.review_task_id=d.review_task_id
               AND callback.execution_task_id=d.execution_task_id
               AND callback.session_id=d.session_id
             WHERE d.review_task_id=? AND d.platform=? AND d.chat_id=?
               AND d.thread_id=? AND d.session_id=? LIMIT 1
            """,
            (review_id, platform, chat_id, thread_id, pending["session_id"]),
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        if (
            value["review_executor_profile"] != "grace-policy-review"
            or kb._grace_loop_stage_header(str(value["review_body"] or ""))
            != "review"
            or kb._grace_loop_stage_header(str(value["execution_body"] or ""))
            != "execution"
            or (
                value["callback_platform"],
                value["callback_chat_id"],
                value["callback_thread_id"],
            )
            != (platform, chat_id, thread_id)
        ):
            return None
        receipt = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='grace_review_retry_authorized' "
            "AND json_extract(payload,'$.message_id')=? LIMIT 1",
            (review_id, pending["message_id"]),
        ).fetchone()
        if receipt is not None:
            try:
                receipt_payload = json.loads(receipt["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(receipt_payload, dict) or (
                receipt_payload.get("platform"),
                receipt_payload.get("chat_id"),
                receipt_payload.get("thread_id"),
                receipt_payload.get("user_id"),
                receipt_payload.get("message_id"),
                receipt_payload.get("review_task_id"),
                receipt_payload.get("execution_task_id"),
            ) != (
                platform,
                chat_id,
                thread_id,
                user_id,
                pending["message_id"],
                review_id,
                value["execution_task_id"],
            ):
                return None
            tool_result = receipt_payload.get("tool_result")
            expected_result = {
                "status": "queued",
                "task_created": False,
                "delegation_id": value["delegation_id"],
                "execution_task_id": value["execution_task_id"],
                "grace_review_task_id": review_id,
                "review_status": "ready",
            }
            return expected_result if tool_result == expected_result else None
        if not pending["authorization_fresh"]:
            return None
        authorized_user_id = owner_user_id or value["origin_user_id"]
        if not authorized_user_id or user_id != authorized_user_id:
            return None
        if value["state"] != "queued" or value["execution_status"] != "done":
            return None
        if value["review_status"] != "blocked" or value["current_run_id"] is not None:
            return None
        blocked = conn.execute(
            "SELECT created_at,payload FROM task_events WHERE task_id=? "
            "AND kind='blocked' ORDER BY id DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        try:
            payload = json.loads(blocked["payload"] or "{}") if blocked else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        blocked_at = blocked["created_at"] if blocked is not None else None
        if not _finite_number(blocked_at):
            return None
        reason = str(payload.get("reason") or "")
        if float(blocked_at) >= pending["user_timestamp"]:
            return None
        repaired_fault = ""
        if (
            value["block_kind"] == "capability"
            and payload.get("kind") == "capability"
            and reason == _MANAGED_POLICY_BLOCK_REASON
        ):
            from proactive.policy_registry import resolve_task_policy_snapshots

            task = kb.get_task(conn, review_id)
            resolve_task_policy_snapshots(task.body if task is not None else "")
            repaired_fault = "managed_policy_absent_binding"
        elif (
            value["block_kind"] in {"transient", "capability"}
            and payload.get("kind") == value["block_kind"]
            and is_runtime_source_block_reason(reason, review_id)
        ):
            kb._workflow_review_source(conn, review_id)
            repaired_fault = "review_runtime_reloaded"
        else:
            return None
        if not kb.unblock_task(conn, review_id):
            return None
        result = {
            "status": "queued",
            "task_created": False,
            "delegation_id": value["delegation_id"],
            "execution_task_id": value["execution_task_id"],
            "grace_review_task_id": review_id,
            "review_status": "ready",
        }
        kb._append_event(
            conn,
            review_id,
            "grace_review_retry_authorized",
            {
                "platform": platform,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "user_id": user_id,
                "message_id": pending["message_id"],
                "review_task_id": review_id,
                "execution_task_id": value["execution_task_id"],
                "repaired_fault": repaired_fault,
                "recovered_after_gateway_restart": True,
                "tool_result": result,
            },
        )
        return result


def recover_interrupted_review_retry(
    *,
    session_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    chat_type: str,
    thread_id: str,
    user_id: str,
    owner_user_id: str = "",
    state_db_path: str | Path | None = None,
    board: str | None = None,
    freshness_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Consume one exact unresolved retry call and persist its tool result."""
    if platform != "telegram" or not all(
        (session_id, session_key, chat_id, chat_type, user_id)
    ):
        return None
    if freshness_seconds is None:
        try:
            freshness_seconds = float(
                os.getenv("HERMES_AUTO_CONTINUE_FRESHNESS", 60 * 60)
            )
        except (TypeError, ValueError):
            freshness_seconds = 60 * 60
    if not _finite_number(freshness_seconds) or float(freshness_seconds) <= 0:
        return None
    effective_freshness = float(freshness_seconds)
    path = _state_path(state_db_path)
    if not path.is_file():
        return None
    from hermes_state import SessionDB

    committed_result: dict[str, Any] | None = None
    caught_errors = (
        OSError,
        sqlite3.Error,
        RuntimeError,
        TypeError,
        ValueError,
        KeyError,
    )
    for attempt in range(2):
        session_db = None
        try:
            session_db = SessionDB(db_path=path)

            def consume(state: sqlite3.Connection) -> dict[str, Any] | None:
                nonlocal committed_result
                pending = _pending_call(
                    state,
                    session_id=session_id,
                    session_key=session_key,
                    platform=platform,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    thread_id=thread_id,
                    user_id=user_id,
                    freshness_seconds=effective_freshness,
                )
                if pending is None:
                    return None
                result = _retry(
                    pending,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    user_id=user_id,
                    owner_user_id=owner_user_id,
                    board=board,
                )
                if result is None:
                    return None
                committed_result = result
                state.execute(
                    "INSERT INTO messages "
                    "(session_id,role,content,tool_call_id,tool_name,timestamp) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        session_id,
                        "tool",
                        json.dumps(result, ensure_ascii=False),
                        pending["call_id"],
                        "clawops_retry_review",
                        time.time(),
                    ),
                )
                state.execute(
                    "UPDATE sessions SET message_count=message_count+1 WHERE id=?",
                    (session_id,),
                )
                return result

            outcome = session_db._execute_write(consume)
            if outcome is None and committed_result is not None:
                return {
                    **committed_result,
                    "status": "state_write_pending",
                    "tool_result_durable": False,
                }
            return outcome
        except caught_errors:
            if committed_result is None:
                return None
            if attempt == 1:
                return {
                    **committed_result,
                    "status": "state_write_pending",
                    "tool_result_durable": False,
                }
        finally:
            if session_db is not None:
                session_db.close()
    return None
