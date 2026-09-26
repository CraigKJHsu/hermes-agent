"""Operator-only recovery of a misclassified, zero-effect standalone review.

This restores the original correction flow; it never accepts business evidence,
creates successor cards, or supplies a new authorization contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time

from hermes_cli import kanban_db as kb


def recover_correction(
    conn: sqlite3.Connection,
    *,
    review_task_id: str,
    execution_run_id: int,
    review_run_id: int,
    blocked_event_id: int,
    platform: str,
    chat_id: str,
    thread_id: str,
    contract_sha256: str,
    operator_evidence: str,
) -> dict:
    if (
        os.getenv("HERMES_KANBAN_TASK")
        or os.getenv("HERMES_SESSION_INTERNAL", "").strip().lower() == "true"
        or os.getenv("HERMES_SESSION_SOURCE", "").strip().lower() == "cron"
    ):
        raise ValueError(
            "Recovery is restricted to a local operator, not workers or callbacks"
        )
    if not operator_evidence.strip():
        raise ValueError(
            "Recovery requires an operator's correction classification evidence"
        )
    with kb.write_txn(conn):
        rows = conn.execute(
            "SELECT * FROM grace_delegations WHERE review_task_id=?",
            (review_task_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("Recovery requires one exact delegation")
        delegation = dict(rows[0])
        if (
            delegation["state"] != "queued"
            or (delegation["platform"], delegation["chat_id"], delegation["thread_id"])
            != (platform, chat_id, thread_id)
            or delegation["objective_id"]
            or delegation["stage_key"]
        ):
            raise ValueError("Recovery requires the exact standalone Topic delegation")
        snapshot = delegation["contract_snapshot"] or ""
        if hashlib.sha256(snapshot.encode()).hexdigest() != contract_sha256:
            raise ValueError("Sealed contract changed")
        contract = json.loads(snapshot)
        if (
            type(contract.get("external_effect_budget")) is not int
            or contract["external_effect_budget"] != 0
            or contract.get("external_targets")
            or contract.get("objective_ref")
            or delegation["approval_required"]
        ):
            raise ValueError("Recovery is limited to the existing zero-effect contract")
        execution_id = delegation["execution_task_id"]
        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_task_id)
        parent = kb.latest_run(conn, execution_id)
        rejection = kb.latest_run(conn, review_task_id)
        if not (
            execution
            and review
            and parent
            and rejection
            and execution.status == "done"
            and not execution.current_run_id
            and review.status == "blocked"
            and not review.current_run_id
            and review.block_kind == "needs_input"
            and parent.id == execution_run_id
            and parent.status == "done"
            and parent.outcome == "completed"
            and parent.ended_at
            and rejection.id == review_run_id
            and rejection.status == "blocked"
            and rejection.outcome == "blocked"
            and rejection.ended_at
            and (rejection.metadata or {}).get("review_outcome") == "rejected"
        ):
            raise ValueError("Task or run state changed; recovery cannot reopen it")
        links = [
            tuple(row)
            for row in conn.execute(
                "SELECT parent_id,child_id FROM task_links "
                "WHERE parent_id IN (?,?) OR child_id IN (?,?)",
                (execution_id, review_task_id, execution_id, review_task_id),
            )
        ]
        if links != [(execution_id, review_task_id)]:
            raise ValueError("Original review dependency graph changed")
        source = (rejection.metadata or {}).get("workflow_review_source") or {}
        if (
            source.get("parent_execution_task_id") != execution_id
            or source.get("parent_execution_run_id") != execution_run_id
            or source.get("parent_execution_evidence_sha256")
            != kb.workflow_review_evidence_hash(parent)
            or source.get("parent_task_body_sha256")
            != hashlib.sha256((execution.body or "").encode()).hexdigest()
            or source.get("review_task_body_sha256")
            != hashlib.sha256((review.body or "").encode()).hexdigest()
        ):
            raise ValueError("Review source evidence changed")
        event = conn.execute(
            "SELECT * FROM task_events WHERE id=? AND task_id=? AND run_id=? AND kind='blocked'",
            (blocked_event_id, review_task_id, review_run_id),
        ).fetchone()
        latest = conn.execute(
            "SELECT id FROM task_events WHERE task_id=? AND kind IN "
            "('blocked','dependency_wait','block_loop_detected','completed') ORDER BY id DESC LIMIT 1",
            (review_task_id,),
        ).fetchone()
        payload = json.loads(event["payload"] or "{}") if event else {}
        reason = payload.get("reason")
        if (
            not event
            or not latest
            or latest["id"] != blocked_event_id
            or not reason
            or payload.get("kind") != "needs_input"
        ):
            raise ValueError("Blocker event changed")
        callback = kb.get_grace_loop_callback(conn, review_task_id)
        if (
            not callback
            or callback["state"] != "delivered"
            or callback["last_event_id"] != blocked_event_id
            or callback["execution_task_id"] != execution_id
            or callback["contract_fingerprint"] != delegation["contract_fingerprint"]
            or callback.get("objective_id")
            or callback.get("stage_key")
            or (callback["platform"], callback["chat_id"], callback["thread_id"])
            != (platform, chat_id, thread_id)
            or (
                callback.get("lease_expires")
                and callback["lease_expires"] > time.time()
            )
        ):
            raise ValueError("Original blocker callback is not fully delivered")
        # block_task normally seals only the verdict and source binding. An
        # explicit review effect record must nevertheless stop zero-effect recovery.
        if (
            (parent.metadata or {}).get("external_effects") != []
            or (rejection.metadata or {}).get("external_effects", []) != []
            or conn.execute(
                "SELECT 1 FROM task_external_effects WHERE task_id IN (?,?) LIMIT 1",
                (execution_id, review_task_id),
            ).fetchone()
        ):
            raise ValueError("External effects cannot be ruled out")
        corrections = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? AND kind='grace_correction_requested' "
            "AND json_valid(payload) AND json_extract(payload,'$.review_task_id')=?",
            (execution_id, review_task_id),
        ).fetchone()[0]
        if corrections + 1 >= kb.BLOCK_RECURRENCE_LIMIT:
            raise ValueError("Original correction retry limit is exhausted")
        if (
            kb._grace_loop_stage_header(execution.body or "") != "execution"
            or kb._grace_loop_stage_header(review.body or "") != "review"
            or review.block_recurrences != 1
            or payload.get("recurrences") != review.block_recurrences
        ):
            raise ValueError("Original formal rejection classification changed")
        kb._guard_behavior_task(conn, execution_id)
        kb._guard_behavior_task(conn, review_task_id)
        # Reclassify the SAME rejected event, rather than recording a second
        # rejection through block_task. Preserve both cards' retry counters and
        # the original immutable review run. Future rejections still pass through
        # the normal bounded correction flow and its durable event count.
        reopened = conn.execute(
            "UPDATE tasks SET status='ready',completed_at=NULL,claim_lock=NULL,"
            "claim_expires=NULL,worker_pid=NULL,current_run_id=NULL,last_heartbeat_at=NULL "
            "WHERE id=? AND status='done' AND current_run_id IS NULL",
            (execution_id,),
        )
        waiting = conn.execute(
            "UPDATE tasks SET status='todo',block_kind='dependency',claim_lock=NULL,"
            "claim_expires=NULL,worker_pid=NULL,last_heartbeat_at=NULL "
            "WHERE id=? AND status='blocked' AND current_run_id IS NULL",
            (review_task_id,),
        )
        if reopened.rowcount != 1 or waiting.rowcount != 1:
            raise ValueError("Original-card correction transition failed")
        reset = conn.execute(
            "UPDATE grace_loop_callbacks SET state='pending',lease_event_id=NULL,"
            "lease_owner=NULL,lease_expires=NULL,attempts=0,attempt_event_id=NULL,"
            "last_error=NULL,outcome_event_id=NULL,outcome_kind=NULL,outcome_payload=NULL,"
            "user_report_event_id=NULL,user_report_digest=NULL,user_report_delivered_at=NULL,"
            "user_report_chunk_count=NULL,user_report_next_chunk=0,user_report_total_chunks=NULL "
            "WHERE review_task_id=? AND state='delivered' AND lease_owner IS NULL",
            (review_task_id,),
        )
        if reset.rowcount != 1:
            raise ValueError("Original correction callback transition failed")
        kb.add_comment(
            conn,
            execution_id,
            "Codex operator recovery",
            "Original Grace rejection returned for correction within its sealed scope.\n"
            "CORRECTION_MODE: reconciliation_first\n"
            "Preserve the original source bytes required by the contract and recompute "
            "only affected delivery digests. No new Objective, successor, authorization "
            "or external action is granted.\n\nOriginal Grace review reason: " + reason,
        )
        kb._append_event(
            conn,
            execution_id,
            "grace_correction_requested",
            {
                "review_task_id": review_task_id,
                "reason": reason,
                "mode": "reconciliation_first",
                "recurrences": corrections + 1,
                "original_blocked_event_id": blocked_event_id,
                "requested_by": "codex_local_operator",
            },
        )
        kb._append_event(
            conn,
            review_task_id,
            "dependency_wait",
            {
                "reason": reason,
                "kind": "dependency",
                "original_blocked_event_id": blocked_event_id,
            },
            run_id=review_run_id,
        )
        if (
            kb.get_task(conn, execution_id).status != "ready"
            or kb.get_task(conn, review_task_id).status != "todo"
        ):
            raise ValueError("Original-card correction readback failed")
        kb._append_event(
            conn,
            review_task_id,
            "operator_review_correction_recovered",
            {
                "original_blocked_event_id": blocked_event_id,
                "original_review_run_id": review_run_id,
                "original_execution_run_id": execution_run_id,
                "contract_sha256": contract_sha256,
                "operator_evidence": operator_evidence,
                "original_kind": "needs_input",
                "corrected_kind": "dependency",
            },
        )
        return {
            "execution_task_id": execution_id,
            "review_task_id": review_task_id,
            "execution_status": "ready",
            "review_status": "todo",
            "correction_attempt": corrections + 1,
        }
