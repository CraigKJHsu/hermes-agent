"""Read digest-bound delivery evidence through the active worker's own board.

No SQL, database path, Topic, or interpreter is selected by the model. This
module never initializes a database, writes a receipt, or contacts a provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any

from hermes_cli import kanban_db as kb
from hermes_cli.grace_review_metadata import grace_review_accepted
from hermes_cli.user_facing_report import (
    render_user_facing_report_chunks,
    user_facing_report_digest,
)


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _delegation(conn: sqlite3.Connection, task_id: str) -> tuple[dict, dict]:
    rows = conn.execute(
        "SELECT * FROM grace_delegations WHERE execution_task_id=? OR review_task_id=?",
        (task_id, task_id),
    ).fetchall()
    _require(len(rows) == 1, "Exactly one sealed delegation is required")
    row = dict(rows[0])
    raw = row.get("contract_snapshot")
    _require(isinstance(raw, str) and raw, "Sealed contract snapshot is unavailable")
    _require(
        hashlib.sha256(raw.encode()).hexdigest() == row["contract_fingerprint"],
        "Sealed contract fingerprint does not match",
    )
    contract = json.loads(raw)
    _require(isinstance(contract, dict), "Sealed contract must be an object")
    identity = contract.get("identity", {})
    _require(
        isinstance(identity, dict)
        and all(str(identity.get(k) or "") for k in ("platform", "chat_id", "project"))
        and all(str(identity.get(k) or "") == str(row.get(k) or "")
                for k in ("platform", "chat_id", "thread_id")),
        "Delegation lane does not match its sealed identity",
    )
    projects = conn.execute(
        "SELECT project_namespace FROM tasks WHERE id IN (?,?)",
        (row["execution_task_id"], row["review_task_id"]),
    ).fetchall()
    _require(len(projects) == 2 and all(p[0] == identity["project"] for p in projects),
             "Sealed project does not match persisted task ownership")
    return row, contract


def read_task_delivery(args: dict[str, Any]) -> dict[str, Any]:
    """Return exact selected receipts, with source and active-run authorization."""
    current_id = os.getenv("HERMES_KANBAN_TASK", "").strip()
    _require(current_id, "Delivery readback requires an active task worker")
    _require(args.get("task_id", current_id) in (None, "", current_id),
             "Delivery readback is bound to the current task")
    board = os.getenv("HERMES_KANBAN_BOARD", "").strip() or None
    _require(not args.get("board") or args["board"] == (board or "default"),
             "Delivery readback cannot switch the worker's board")
    review_id = args.get("source_review_task_id")
    _require(isinstance(review_id, str) and re.fullmatch(r"t_[0-9a-f]+", review_id),
             "An exact source_review_task_id is required")
    message_ids = args.get("message_ids")
    _require(
        isinstance(message_ids, list) and 0 < len(message_ids) <= 100
        and all(isinstance(v, str) and re.fullmatch(r"[1-9][0-9]{0,19}", v)
                for v in message_ids)
        and len(set(message_ids)) == len(message_ids),
        "message_ids must contain 1-100 distinct exact provider message IDs",
    )
    path = kb.kanban_db_path(board=board)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        role = kb.validate_grace_loop_worker_auth(
            conn, task_id=current_id,
            run_id=os.getenv("HERMES_KANBAN_RUN_ID", ""),
            claim_lock=os.getenv("HERMES_KANBAN_CLAIM_LOCK", ""),
            worker_auth_token=os.getenv("HERMES_KANBAN_WORKER_AUTH_TOKEN", ""),
        )
        _require(role in ("execution", "review"), "Active delegated worker authentication failed")
        caller, contract = _delegation(conn, current_id)
        result = read_delivery_evidence(conn, contract, review_id, message_ids)
        verification_ids = (caller["execution_task_id"], caller["review_task_id"])
        effect_count = conn.execute(
            "SELECT count(*) FROM task_external_effects WHERE task_id IN (?,?)", verification_ids,
        ).fetchone()[0]
        approval_count = conn.execute(
            "SELECT count(*) FROM grace_approval_challenges WHERE contract_fingerprint=?",
            (caller["contract_fingerprint"],),
        ).fetchone()[0]
        result.update(verification_external_effect_count=effect_count,
                      verification_approval_challenge_count=approval_count)
        return result


def read_delivery_evidence(
    conn: sqlite3.Connection, contract: dict[str, Any], review_id: str,
    message_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Read accepted delivery for a trusted caller contract, without worker credentials.

    Internal predelegation callers supply the contract; this is not a model tool.
    The authenticated worker wrapper above remains the public readback boundary.
    """
    source, source_contract = _delegation(conn, review_id)
    execution_id = source["execution_task_id"]
    _require(source["review_task_id"] == review_id, "Source must name the review task")
    identity = contract["identity"]
    _require(
        all(str(identity.get(k) or "") == str(source_contract["identity"].get(k) or "")
            for k in ("platform", "chat_id", "thread_id", "project", "board")),
        "Source belongs to another Topic, project, or board",
    )
    scope = contract.get("scope")
    _require(isinstance(scope, dict), "Sealed allowed scope is missing")
    allowed = scope.get("allowed", [])
    _require(isinstance(allowed, list) and all(isinstance(v, str) for v in allowed),
             "Sealed allowed scope is missing")
    named_ids = set(re.findall(r"(?<![\w])t_[0-9a-f]+(?![\w])", "\n".join(allowed)))
    _require({execution_id, review_id} <= named_ids,
             "Source execution/review pair is not named in the sealed allowed scope")
    _require(conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
        (execution_id, review_id),
    ).fetchone(), "Source does not have a direct execution/review link")
    execution = kb.latest_run(conn, execution_id)
    review = kb.latest_run(conn, review_id)
    _require(
        execution and review and execution.outcome == review.outcome == "completed"
        and execution.ended_at <= review.ended_at
        and grace_review_accepted(review.metadata),
        "Latest source execution/review is not an accepted completed pair",
    )
    callback = kb.get_grace_loop_callback(conn, review_id)
    _require(callback and callback["execution_task_id"] == execution_id,
             "Source delivery callback is missing")
    _require(
        callback["contract_fingerprint"] == source["contract_fingerprint"]
        and all(str(callback.get(k) or "") == str(identity.get(k) or "")
                for k in ("platform", "chat_id", "thread_id")),
        "Callback does not match the selected contract and lane",
    )
    event_id = callback.get("user_report_event_id")
    event = conn.execute(
        "SELECT task_id,run_id,kind FROM task_events WHERE id=?", (event_id,),
    ).fetchone()
    _require(event and event["task_id"] == review_id and event["run_id"] == review.id
             and event["kind"] == "completed", "Receipt is not bound to the latest accepted review event")
    task = kb.get_task(conn, execution_id)
    compiled = kb._grace_compiled_contract(task.body)
    _require(compiled and compiled.get("user_facing_delivery") == source_contract.get("user_facing_delivery"),
             "Source delivery contract differs from its sealed snapshot")
    # The canonical builder hashes and decodes current attachment bytes;
    # comparing its freshly computed report digest below rejects replacements.
    report = kb.grace_inline_content_package_report(conn, execution_id)
    _require(report, "Canonical delivery report or original assets cannot be verified")
    digest = user_facing_report_digest(report)
    _require(digest == callback.get("user_report_digest"), "Canonical report does not match receipt digest")
    chunks = render_user_facing_report_chunks(report)
    items = [("text", text) for text in chunks]
    items.extend(("image", asset) for asset in report.get("assets", []))
    payload = json.loads(callback.get("outcome_payload") or "{}")
    checkpoint_question = (
        "完整發布包已交付並通過 Grace 審查。要繼續進行 "
        "Facebook Page 發布前檢查嗎？"
    )
    system_checkpoint = (
        callback.get("outcome_kind") == "approval_blocked"
        and report.get("package_kind") == "full_publication_package"
        and bool(source.get("objective_id"))
        and callback.get("objective_id") == source["objective_id"]
        and isinstance(payload, dict)
        and payload.get("action") == "confirm_content_package"
        and payload.get("platform") == "internal"
        and payload.get("scope") == {
            "execution_task_id": execution_id,
            "review_task_id": review_id,
            "review_event_id": event_id,
        }
        and payload.get("exact_question") == checkpoint_question
    )
    # Gateway sends this question after the complete package. Its receipt
    # proves delivery, not publication approval or Objective completion.
    if system_checkpoint:
        items.append(("checkpoint", checkpoint_question))
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM grace_user_report_chunk_deliveries "
        "WHERE review_task_id=? AND event_id=? AND report_digest=? ORDER BY chunk_index",
        (review_id, event_id, digest),
    )]
    if message_ids is None:
        message_ids = [row["message_id"] for row in rows]
    selected = [row for row in rows if row["message_id"] in message_ids]
    _require({row["message_id"] for row in selected} == set(message_ids)
             and len(selected) == len(message_ids),
             "One or more requested messages lack an unambiguous receipt for this report")
    _require(all(row["state"] == "sent" for row in selected),
             "Requested messages include receipts not yet confirmed sent")
    output = []
    for row in selected:
        index = row["chunk_index"]
        _require(type(index) is int and 0 <= index < len(items)
                 and row["total_chunks"] == len(items), "Receipt chunk mapping is inconsistent")
        kind, item = items[index]
        entry = {**row, "platform": identity["platform"], "chat_id": identity["chat_id"],
                 "thread_id": identity.get("thread_id", ""), "kind": kind}
        if kind in {"text", "checkpoint"}:
            entry.update(text=item, text_sha256=hashlib.sha256(item.encode()).hexdigest())
        else:
            from PIL import Image
            with Image.open(item["path"]) as image:
                dimensions = list(image.size)
            entry.update(submitted_asset=item, dimensions=dimensions,
                         remote_media_bytes_verified=False)
        output.append(entry)
    delivery_complete = (
        len(rows) == len(items)
        and [row["chunk_index"] for row in rows] == list(range(len(items)))
        and all(row["state"] == "sent" and row["message_id"]
                and row["total_chunks"] == len(items) for row in rows)
        and len({row["message_id"] for row in rows}) == len(items)
        and callback.get("user_report_next_chunk") == callback.get("user_report_total_chunks") == len(items)
    )
    callback_current = (
        callback.get("state") == "delivered"
        and callback.get("last_event_id") == callback.get("outcome_event_id") == event_id
        and bool(callback.get("user_report_delivered_at"))
    )
    callback_closed = callback_current and callback.get("outcome_kind") == "closed"
    _require(delivery_complete, "Report does not have a complete consistent sent receipt set")
    _require(callback_current and (callback_closed or system_checkpoint),
             "Selected report is not the callback's current accepted delivery outcome")
    return {
        "success": True, "view": "delivery", "observed_at": int(time.time()),
        "evidence_source": "live_kanban_provider_confirmed_receipts",
        "source_execution_task_id": execution_id, "source_review_task_id": review_id,
        "source_review_run_id": review.id, "event_id": event_id,
        "report_digest": digest, "body_sha256": hashlib.sha256(report["body"].encode()).hexdigest(),
        "title": report["title"], "body": report["body"], "messages": output,
        "requested_messages_confirmed_sent": all(row["state"] == "sent" for row in selected),
        "whole_report_delivery_complete": delivery_complete,
        "callback_state": callback.get("state"), "callback_outcome": callback.get("outcome_kind"),
        "callback_closed_for_report": callback_closed,
        "callback_report_delivery_verified": True,
        "callback_error": callback.get("last_error"),
        "current_telegram_ui_verified": False, "human_read_verified": False,
        "evidence_limit": "Provider receipts confirm sending the digest-bound content and submitted images; "
                          "they do not verify current UI visibility, remote-downloaded bytes, or human reading.",
    }
