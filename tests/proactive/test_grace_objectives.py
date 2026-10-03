"""Durable Grace objective regression tests."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from proactive.prompt_policy import active_objectives_prompt


def _create_objective(conn, *, objective_id: str = "go_test") -> dict:
    return kb.create_grace_objective(
        conn,
        objective_id=objective_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        session_key="agent:main:telegram:group:chat-1:4641",
        title="Publish Page then share to Group",
        objective="Publish the accepted Page post and share that post to the Group.",
        original_request_sha256="a" * 64,
        required_stage_keys=("prepare_asset", "publish_page", "share_group"),
        terminal_stage_key="share_group",
        acceptance_criteria=("Page post id verified", "Group result verified"),
        current_stage_key="prepare_asset",
        next_action="Prepare the corrected asset.",
    )


def _bind_queued_delegation(conn, execution_id: str, review_id: str, *, suffix: str) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO grace_delegations (
            delegation_id, contract_fingerprint, request_instance_id,
            platform, chat_id, thread_id, session_key, session_id,
            resolved_route, approval_required, state,
            execution_task_id, review_task_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'telegram', 'chat-1', '4641', ?, ?, '{}', 0,
                  'queued', ?, ?, ?, ?)
        """,
        (
            f"gd-{suffix}",
            (suffix.encode().hex() + "0" * 64)[:64],
            f"request-{suffix}",
            "agent:main:telegram:group:chat-1:4641",
            "session-1",
            execution_id,
            review_id,
            now,
            now,
        ),
    )


def _accepted_callback(conn, *, objective_id: str, stage_key: str, requested_mode: str):
    execution_id = kb.create_task(conn, title=f"execute {stage_key}")
    execution = kb.claim_task(conn, execution_id, claimer=f"execute-{stage_key}")
    assert execution is not None and execution.current_run_id is not None
    assert kb.complete_task(
        conn,
        execution_id,
        summary="done",
        expected_run_id=execution.current_run_id,
    )
    review_id = kb.create_task(conn, title=f"review {stage_key}", parents=(execution_id,))
    kb.add_grace_loop_callback(
        conn,
        review_task_id=review_id,
        execution_task_id=execution_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        session_key="agent:main:telegram:group:chat-1:4641",
        session_id="session-1",
        contract_fingerprint="e" * 64,
        completion_mode=requested_mode,
        objective_id=objective_id,
        stage_key=stage_key,
    )
    _bind_queued_delegation(conn, execution_id, review_id, suffix=stage_key)
    review = kb.claim_task(conn, review_id, claimer=f"review-{stage_key}")
    assert review is not None and review.current_run_id is not None
    assert kb.complete_task(
        conn,
        review_id,
        summary="accepted",
        metadata={"review_outcome": "accepted"},
        expected_run_id=review.current_run_id,
    )
    callback = kb.list_due_grace_loop_callbacks(conn)[0]
    assert kb.claim_grace_loop_callback(
        conn,
        review_task_id=review_id,
        event_id=callback["event_id"],
        lease_owner="owner-a",
    )
    return review_id, callback["event_id"]


def _blocked_callback(conn, *, objective_id: str, stage_key: str, requested_mode: str):
    execution_id = kb.create_task(conn, title=f"execute {stage_key}")
    review_id = kb.create_task(conn, title=f"review {stage_key}", parents=(execution_id,))
    kb.add_grace_loop_callback(
        conn,
        review_task_id=review_id,
        execution_task_id=execution_id,
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        session_key="agent:main:telegram:group:chat-1:4641",
        session_id="session-1",
        contract_fingerprint="d" * 64,
        completion_mode=requested_mode,
        objective_id=objective_id,
        stage_key=stage_key,
    )
    _bind_queued_delegation(conn, execution_id, review_id, suffix=f"blocked-{stage_key}")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (review_id,))
    assert kb.block_task(
        conn,
        review_id,
        reason="required execution evidence is missing",
        kind="capability",
    )
    callback = kb.list_due_grace_loop_callbacks(conn)[0]
    assert kb.claim_grace_loop_callback(
        conn,
        review_task_id=review_id,
        event_id=callback["event_id"],
        lease_owner="owner-a",
    )
    return review_id, callback["event_id"]


def test_objective_authoritatively_forces_intermediate_stage(tmp_path):
    with kb.connect_closing(tmp_path / "objective.db") as conn:
        _create_objective(conn)
        review_id, _event_id = _accepted_callback(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset",
            requested_mode="terminal",
        )
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["completion_mode"] == "intermediate"
        assert callback["objective_id"] == "go_test"
        assert callback["stage_key"] == "prepare_asset"


def test_accepted_intermediate_fallback_finishes_delivery_without_closing_objective(tmp_path):
    with kb.connect_closing(tmp_path / "fallback.db") as conn:
        _create_objective(conn)
        review_id, event_id = _accepted_callback(
            conn, objective_id="go_test", stage_key="prepare_asset", requested_mode="intermediate",
        )
        assert not kb.finish_grace_loop_callback(
            conn, review_task_id=review_id, event_id=event_id, lease_owner="owner-a",
        )
        kb.record_grace_intermediate_callback_without_structured_continuation(
            conn, review_task_id=review_id, event_id=event_id, lease_owner="owner-a",
            platform="telegram", chat_id="chat-1", thread_id="4641", session_id="session-1",
            reason="Accepted read-only evidence; publication still incomplete",
        )
        assert not kb.finish_grace_loop_callback(
            conn, review_task_id=review_id, event_id=event_id, lease_owner="other-owner",
        )
        assert kb.finish_grace_loop_callback(
            conn, review_task_id=review_id, event_id=event_id, lease_owner="owner-a",
        )
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["state"] == "delivered"
        assert callback["last_event_id"] == event_id
        assert kb.get_grace_objective(conn, "go_test")["status"] == "blocked"


def test_terminal_review_blocker_records_objective_terminal_blocked(tmp_path):
    with kb.connect_closing(tmp_path / "objective-terminal-blocked.db") as conn:
        _create_objective(conn)
        review_id, event_id = _blocked_callback(
            conn,
            objective_id="go_test",
            stage_key="share_group",
            requested_mode="terminal",
        )
        kb.record_grace_loop_callback_blocker_outcome(
            conn,
            review_task_id=review_id,
            event_id=event_id,
            lease_owner="owner-a",
            outcome_kind="terminal_blocked",
            payload={
                "summary": "Grace review fail-closed.",
                "reason": "destination readback is missing",
                "next_action": "Run read-only reconciliation before any retry.",
            },
        )

        objective = kb.get_grace_objective(conn, "go_test")
        stage = conn.execute(
            """
            SELECT status, outcome_kind, evidence
              FROM grace_objective_stages
             WHERE objective_id = 'go_test' AND stage_key = 'share_group'
            """
        ).fetchone()
        callback = kb.get_grace_loop_callback(conn, review_id)

        assert objective["status"] == "blocked"
        assert objective["waiting_for"] == "destination readback is missing"
        assert stage["status"] == "done"
        assert stage["outcome_kind"] == "terminal_blocked"
        assert callback["outcome_kind"] == "terminal_blocked"


def test_intermediate_review_blocker_records_objective_intermediate_blocked(tmp_path):
    with kb.connect_closing(tmp_path / "objective-intermediate-blocked.db") as conn:
        _create_objective(conn)
        review_id, event_id = _blocked_callback(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset",
            requested_mode="terminal",
        )
        kb.record_grace_loop_callback_blocker_outcome(
            conn,
            review_task_id=review_id,
            event_id=event_id,
            lease_owner="owner-a",
            outcome_kind="intermediate_blocked",
            payload={
                "summary": "Grace review fail-closed.",
                "reason": "structured recovery evidence is missing",
                "next_action": "Create a fresh recovery stage.",
            },
        )

        objective = kb.get_grace_objective(conn, "go_test")
        stage = conn.execute(
            """
            SELECT status, outcome_kind, evidence
              FROM grace_objective_stages
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """
        ).fetchone()
        callback = kb.get_grace_loop_callback(conn, review_id)

        assert objective["status"] == "blocked"
        assert objective["waiting_for"] == "structured recovery evidence is missing"
        assert objective["current_stage_key"] == "prepare_asset"
        assert stage["status"] == "done"
        assert stage["outcome_kind"] == "intermediate_blocked"
        assert callback["outcome_kind"] == "intermediate_blocked"


def test_retry_stage_supersedes_previous_bound_retry_stage(tmp_path):
    with kb.connect_closing(tmp_path / "objective-retry-supersedes.db") as conn:
        _create_objective(conn)
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'queued',
                   delegation_id = 'gd-prepare',
                   execution_task_id = 't-exec',
                   review_task_id = 't-review'
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """
        )

        objective = kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            next_action="Retry preparation.",
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            delegation_id="gd-retry",
        )
        prior = conn.execute(
            """
            SELECT status, outcome_kind, evidence
              FROM grace_objective_stages
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """
        ).fetchone()

        assert objective["current_stage_key"] == "prepare_asset_r2"
        assert prior["status"] == "done"
        assert prior["outcome_kind"] == "superseded_by_retry"
        assert "prepare_asset_r2" in prior["evidence"]


def test_later_retry_supersedes_open_retry_sibling_and_callback(tmp_path):
    with kb.connect_closing(tmp_path / "objective-retry-sibling.db") as conn:
        _create_objective(conn)
        first_execution = kb.create_task(conn, title="first execution")
        first_review = kb.create_task(
            conn, title="first review", parents=(first_execution,),
        )
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (first_execution,))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (first_review,))
        kb.add_grace_loop_callback(
            conn,
            review_task_id=first_review,
            execution_task_id=first_execution,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            contract_fingerprint="b" * 64,
            completion_mode="intermediate",
            objective_id="go_test",
            stage_key="prepare_asset",
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'queued', delegation_id = 'gd-first',
                   execution_task_id = ?, review_task_id = ?
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """,
            (first_execution, first_review),
        )
        kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
        )

        second_execution = kb.create_task(conn, title="second execution")
        second_review = kb.create_task(
            conn, title="second review", parents=(second_execution,),
        )
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (second_execution,))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (second_review,))
        kb.add_grace_loop_callback(
            conn,
            review_task_id=second_review,
            execution_task_id=second_execution,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-2",
            contract_fingerprint="c" * 64,
            completion_mode="intermediate",
            objective_id="go_test",
            stage_key="prepare_asset_r2",
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'queued', delegation_id = 'gd-second',
                   execution_task_id = ?, review_task_id = ?
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset_r2'
            """,
            (second_execution, second_review),
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            delegation_id="gd-second",
        )
        objective = kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r3",
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r3",
            delegation_id="gd-third",
        )
        prior = conn.execute(
            """
            SELECT status, outcome_kind
              FROM grace_objective_stages
             WHERE objective_id = 'go_test'
               AND stage_key = 'prepare_asset_r2'
            """
        ).fetchone()
        callback = kb.get_grace_loop_callback(conn, second_review)

        assert objective["current_stage_key"] == "prepare_asset_r3"
        assert (prior["status"], prior["outcome_kind"]) == (
            "done", "superseded_by_retry",
        )
        assert callback["state"] == "cancelled"
        assert callback["lease_event_id"] is None
        assert "prepare_asset_r3" in callback["last_error"]
        assert kb.list_due_grace_loop_callbacks(conn) == []


def test_later_retry_supersedes_terminal_cancelled_sibling(tmp_path):
    with kb.connect_closing(tmp_path / "objective-cancelled-retry.db") as conn:
        _create_objective(conn)
        execution_id = kb.create_task(conn, title="cancelled execution")
        review_id = kb.create_task(
            conn, title="cancelled review", parents=(execution_id,),
        )
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id IN (?,?)",
            (execution_id, review_id),
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            contract_fingerprint="f" * 64,
            completion_mode="intermediate",
            objective_id="go_test",
            stage_key="prepare_asset",
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='cancelled',"
            "outcome_kind='cancelled' WHERE review_task_id=?",
            (review_id,),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',"
            "outcome_kind='cancelled',evidence=?,execution_task_id=?,"
            "review_task_id=?,completed_at=123 WHERE objective_id='go_test' "
            "AND stage_key='prepare_asset'",
            (json.dumps({"reason": "preserve this cancellation"}), execution_id, review_id),
        )
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET stage_key='wrong_stage' "
            "WHERE review_task_id=?",
            (review_id,),
        )
        with pytest.raises(ValueError, match="not safe to supersede"):
            kb._bind_grace_objective_stage(
                conn,
                objective_id="go_test",
                stage_key="prepare_asset_r2",
                delegation_id="gd-retry",
            )
        conn.execute(
            "UPDATE grace_loop_callbacks SET stage_key='prepare_asset' "
            "WHERE review_task_id=?",
            (review_id,),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET evidence='[]' "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'"
        )
        with pytest.raises(ValueError, match="not an object"):
            kb._bind_grace_objective_stage(
                conn,
                objective_id="go_test",
                stage_key="prepare_asset_r2",
                delegation_id="gd-retry",
            )
        conn.execute(
            "UPDATE grace_objective_stages SET evidence=? "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'",
            (json.dumps({"reason": "preserve this cancellation"}),),
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            delegation_id="gd-retry",
        )

        stage = conn.execute(
            "SELECT outcome_kind,evidence,completed_at FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'"
        ).fetchone()
        callback = kb.get_grace_loop_callback(conn, review_id)
        evidence = json.loads(stage["evidence"])
        assert stage["outcome_kind"] == "superseded_by_retry"
        assert stage["completed_at"] == 123
        assert evidence["reason"] == "preserve this cancellation"
        assert evidence["superseded_by_retry"]["next_stage_key"] == "prepare_asset_r2"
        assert callback["state"] == "cancelled"
        assert callback["outcome_kind"] == "superseded_by_retry"


def test_historical_cancelled_stage_repair_requires_verified_successor(tmp_path):
    with kb.connect_closing(tmp_path / "objective-history-repair.db") as conn:
        _create_objective(conn)
        source_execution = kb.create_task(conn, title="source execution")
        source_review = kb.create_task(
            conn, title="source review", parents=(source_execution,),
        )
        successor_execution = kb.create_task(conn, title="successor execution")
        successor_review = kb.create_task(
            conn, title="successor review", parents=(successor_execution,),
        )
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id IN (?,?,?,?)",
            (source_execution, source_review, successor_execution, successor_review),
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=source_review,
            execution_task_id=source_execution,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            contract_fingerprint="1" * 64,
            completion_mode="intermediate",
            objective_id="go_test",
            stage_key="prepare_asset",
        )
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=successor_review,
            execution_task_id=successor_execution,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-2",
            contract_fingerprint="2" * 64,
            completion_mode="intermediate",
            objective_id="go_test",
            stage_key="prepare_asset_r2",
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='cancelled',"
            "outcome_kind='cancelled',last_error='original callback cancellation' "
            "WHERE review_task_id=?",
            (source_review,),
        )
        kb._append_event(
            conn, successor_review, "completed", {"summary": "accepted successor"},
        )
        successor_event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='pending',"
            "outcome_kind='continued',outcome_event_id=? WHERE review_task_id=?",
            (successor_event_id, successor_review),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',"
            "outcome_kind='cancelled',evidence=?,execution_task_id=?,"
            "review_task_id=? WHERE objective_id='go_test' "
            "AND stage_key='prepare_asset'",
            (json.dumps({"reason": "original cancellation"}), source_execution, source_review),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',"
            "outcome_kind='continued',execution_task_id=?,review_task_id=? "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset_r2'",
            (successor_execution, successor_review),
        )
        receipt = {
            "authorization_id": "user-confirmation",
            "reason": "repair history",
            "objective_id": "go_test",
            "stage_successors": {"prepare_asset": "prepare_asset_r2"},
        }
        with pytest.raises(ValueError, match="duplicate normalized stage keys"):
            kb.repair_superseded_cancelled_grace_objective_stages(
                conn,
                objective_id="go_test",
                stage_successors={
                    "prepare_asset": "prepare_asset_r2",
                    " prepare_asset ": "prepare_asset_r3",
                },
                repair_receipt=receipt,
            )
        with pytest.raises(ValueError, match="evidence changed"):
            kb.repair_superseded_cancelled_grace_objective_stages(
                conn,
                objective_id="go_test",
                stage_successors={"prepare_asset": "prepare_asset_r2"},
                repair_receipt=receipt,
            )
        assert conn.execute(
            "SELECT outcome_kind FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'"
        ).fetchone()[0] == "cancelled"
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='cancelled' "
            "WHERE review_task_id=?",
            (successor_review,),
        )
        with pytest.raises(ValueError, match="evidence changed"):
            kb.repair_superseded_cancelled_grace_objective_stages(
                conn,
                objective_id="go_test",
                stage_successors={"prepare_asset": "prepare_asset_r2"},
                repair_receipt=receipt,
            )
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='delivered' WHERE review_task_id=?",
            (successor_review,),
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET stage_key='wrong_stage' "
            "WHERE review_task_id=?",
            (successor_review,),
        )
        with pytest.raises(ValueError, match="evidence changed"):
            kb.repair_superseded_cancelled_grace_objective_stages(
                conn,
                objective_id="go_test",
                stage_successors={"prepare_asset": "prepare_asset_r2"},
                repair_receipt=receipt,
            )
        conn.execute(
            "UPDATE grace_loop_callbacks SET stage_key='prepare_asset_r2' "
            "WHERE review_task_id=?",
            (successor_review,),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET evidence='[]' "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'"
        )
        with pytest.raises(ValueError, match="evidence is invalid"):
            kb.repair_superseded_cancelled_grace_objective_stages(
                conn,
                objective_id="go_test",
                stage_successors={"prepare_asset": "prepare_asset_r2"},
                repair_receipt=receipt,
            )
        conn.execute(
            "UPDATE grace_objective_stages SET evidence=? "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'",
            (json.dumps({"reason": "original cancellation"}),),
        )
        result = kb.repair_superseded_cancelled_grace_objective_stages(
            conn,
            objective_id="go_test",
            stage_successors={"prepare_asset": "prepare_asset_r2"},
            repair_receipt=receipt,
        )

        stage = conn.execute(
            "SELECT outcome_kind,evidence FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'"
        ).fetchone()
        callback = kb.get_grace_loop_callback(conn, source_review)
        evidence = json.loads(stage["evidence"])
        assert result["repaired_stages"][0]["successor_stage_key"] == "prepare_asset_r2"
        assert stage["outcome_kind"] == "superseded_by_retry"
        assert evidence["reason"] == "original cancellation"
        assert evidence["superseded_by_retry"]["repair_receipt"] == receipt
        assert callback["outcome_kind"] == "superseded_by_retry"
        assert callback["last_error"] == "original callback cancellation"


def test_historical_cancelled_stage_repair_rejects_active_lease(tmp_path):
    path = tmp_path / "objective-history-repair-lease.db"
    with kb.connect_closing(path) as conn:
        _create_objective(conn)
        execution_id = kb.create_task(conn, title="source execution")
        review_id = kb.create_task(conn, title="source review", parents=(execution_id,))
        successor_execution = kb.create_task(conn, title="successor execution")
        successor_review = kb.create_task(
            conn, title="successor review", parents=(successor_execution,),
        )
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id IN (?,?,?,?)",
            (execution_id, review_id, successor_execution, successor_review),
        )
        kb.add_grace_loop_callback(
            conn, review_task_id=review_id, execution_task_id=execution_id,
            platform="telegram", chat_id="chat-1", thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641", session_id="session-1",
            contract_fingerprint="3" * 64, completion_mode="intermediate",
            objective_id="go_test", stage_key="prepare_asset",
        )
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
        )
        kb.add_grace_loop_callback(
            conn, review_task_id=successor_review,
            execution_task_id=successor_execution, platform="telegram",
            chat_id="chat-1", thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-2", contract_fingerprint="4" * 64,
            completion_mode="intermediate", objective_id="go_test",
            stage_key="prepare_asset_r2",
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='cancelled',outcome_kind='cancelled',"
            "lease_owner='active-owner' WHERE review_task_id=?",
            (review_id,),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',outcome_kind='cancelled',"
            "execution_task_id=?,review_task_id=? WHERE objective_id='go_test' "
            "AND stage_key='prepare_asset'",
            (execution_id, review_id),
        )
        kb._append_event(
            conn, successor_review, "completed", {"summary": "accepted successor"},
        )
        successor_event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='delivered',"
            "outcome_kind='continued',outcome_event_id=? WHERE review_task_id=?",
            (successor_event_id, successor_review),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',outcome_kind='continued',"
            "execution_task_id=?,review_task_id=? WHERE objective_id='go_test' "
            "AND stage_key='prepare_asset_r2'",
            (successor_execution, successor_review),
        )
        with pytest.raises(ValueError, match="evidence changed"):
            kb.repair_superseded_cancelled_grace_objective_stages(
                conn,
                objective_id="go_test",
                stage_successors={"prepare_asset": "prepare_asset_r2"},
                repair_receipt={
                    "authorization_id": "confirmed",
                    "reason": "repair",
                    "objective_id": "go_test",
                    "stage_successors": {"prepare_asset": "prepare_asset_r2"},
                },
            )
        assert conn.execute(
            "SELECT outcome_kind FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'"
        ).fetchone()[0] == "cancelled"


def test_reselecting_existing_retry_repairs_open_older_sibling(tmp_path):
    with kb.connect_closing(tmp_path / "objective-reselect-retry.db") as conn:
        _create_objective(conn)
        execution_id = kb.create_task(conn, title="old execution")
        review_id = kb.create_task(
            conn, title="old review", parents=(execution_id,),
        )
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (execution_id,))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (review_id,))
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            contract_fingerprint="d" * 64,
            completion_mode="intermediate",
            objective_id="go_test",
            stage_key="prepare_asset",
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'queued', delegation_id = 'gd-old',
                   execution_task_id = ?, review_task_id = ?
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """,
            (execution_id, review_id),
        )
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            delegation_id="gd-retry",
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'queued', outcome_kind = NULL,
                   evidence = NULL, completed_at = NULL
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """
        )
        conn.execute(
            """
            UPDATE grace_loop_callbacks
               SET state = 'pending', last_error = NULL
             WHERE review_task_id = ?
            """,
            (review_id,),
        )

        objective = kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            next_action="Resume accepted retry.",
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            delegation_id="gd-retry",
        )
        prior = conn.execute(
            """
            SELECT status, outcome_kind
              FROM grace_objective_stages
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """
        ).fetchone()
        callback = kb.get_grace_loop_callback(conn, review_id)

        assert objective["current_stage_key"] == "prepare_asset_r2"
        assert objective["next_action"] == "Resume accepted retry."
        assert (prior["status"], prior["outcome_kind"]) == (
            "done", "superseded_by_retry",
        )
        assert callback["state"] == "cancelled"
        assert "prepare_asset_r2" in callback["last_error"]


def test_later_retry_cannot_supersede_an_active_execution(tmp_path):
    with kb.connect_closing(tmp_path / "objective-active-retry.db") as conn:
        _create_objective(conn)
        execution_id = kb.create_task(conn, title="active execution")
        review_id = kb.create_task(
            conn, title="active review", parents=(execution_id,),
        )
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (execution_id,))
        conn.execute(
            "INSERT INTO task_runs(task_id,profile,status,started_at) "
            "VALUES (?,'default','running',1)",
            (execution_id,),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='queued',delegation_id='gd-active',"
            "execution_task_id=?,review_task_id=? "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'",
            (execution_id, review_id),
        )

        with pytest.raises(ValueError, match="still has in-flight work"):
            with kb.write_txn(conn):
                kb.ensure_grace_objective_stage(
                    conn, objective_id="go_test", stage_key="prepare_asset_r2",
                )
                kb._bind_grace_objective_stage(
                    conn, objective_id="go_test", stage_key="prepare_asset_r2",
                    delegation_id="gd-retry",
                )

        objective = kb.get_grace_objective(conn, "go_test")
        prior = conn.execute(
            "SELECT status,outcome_kind FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset'"
        ).fetchone()
        assert objective["current_stage_key"] == "prepare_asset"
        assert (prior["status"], prior["outcome_kind"]) == ("queued", None)
        assert conn.execute(
            "SELECT 1 FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='prepare_asset_r2'"
        ).fetchone() is None


@pytest.mark.parametrize("older_stage", ["prepare_asset", "prepare_asset_r2"])
@pytest.mark.parametrize("newer_status", ["queued", "done"])
def test_binding_older_retry_cannot_supersede_newer_sibling(
    tmp_path, older_stage, newer_status,
):
    with kb.connect_closing(tmp_path / "objective-no-retry-rollback.db") as conn:
        _create_objective(conn)
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
        )
        kb._bind_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
            delegation_id="gd-r2",
        )
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r3",
        )
        kb._bind_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r3",
            delegation_id="gd-r3",
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = ?
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset_r3'
            """,
            (newer_status,),
        )

        with pytest.raises(ValueError, match="cannot move backward"):
            kb._bind_grace_objective_stage(
                conn, objective_id="go_test", stage_key=older_stage,
                delegation_id=("gd-r2" if older_stage.endswith("_r2") else "gd-root"),
            )

        objective = kb.get_grace_objective(conn, "go_test")
        current = conn.execute(
            """
            SELECT status, delegation_id
              FROM grace_objective_stages
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset_r3'
            """
        ).fetchone()
        assert objective["current_stage_key"] == "prepare_asset_r3"
        assert (current["status"], current["delegation_id"]) == (
            newer_status, "gd-r3",
        )


def test_bound_retry_cancels_delivered_prior_callback_with_new_event(tmp_path):
    with kb.connect_closing(tmp_path / "objective-delivered-stale.db") as conn:
        _create_objective(conn)
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
        )
        execution_id = kb.create_task(conn, title="prior execution")
        review_id = kb.create_task(
            conn, title="prior review", parents=(execution_id,),
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            contract_fingerprint="e" * 64,
            completion_mode="intermediate",
            objective_id="go_test",
            stage_key="prepare_asset_r2",
        )
        _bind_queued_delegation(
            conn, execution_id, review_id, suffix="delivered-stale",
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'queued', delegation_id = 'gd-delivered-stale',
                   execution_task_id = ?, review_task_id = ?
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset_r2'
            """,
            (execution_id, review_id),
        )
        kb._bind_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r2",
            delegation_id="gd-delivered-stale",
        )
        with kb.write_txn(conn):
            kb._append_event(conn, review_id, "blocked", {"reason": "first"})
            first_event_id = conn.execute(
                "SELECT MAX(id) FROM task_events WHERE task_id = ?", (review_id,),
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE grace_loop_callbacks
                   SET state = 'delivered', last_event_id = ?, delivered_at = ?
                 WHERE review_task_id = ?
                """,
                (first_event_id, int(time.time()), review_id),
            )
            kb._append_event(conn, review_id, "gave_up", {"reason": "later"})
            conn.execute(
                "UPDATE tasks SET status='blocked' WHERE id IN (?, ?)",
                (execution_id, review_id),
            )

        assert [
            item["review_task_id"]
            for item in kb.list_due_grace_loop_callbacks(conn)
        ] == [review_id]
        kb.ensure_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r3",
        )
        kb._bind_grace_objective_stage(
            conn, objective_id="go_test", stage_key="prepare_asset_r3",
            delegation_id="gd-r3",
        )

        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["state"] == "cancelled"
        assert callback["last_event_id"] == first_event_id
        assert callback["delivered_at"] is not None
        assert kb.list_due_grace_loop_callbacks(conn) == []


def test_delegation_reservation_binds_declared_objective_stage(tmp_path):
    with kb.connect_closing(tmp_path / "objective-reserve.db") as conn:
        _create_objective(conn)
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="b" * 64,
            request_instance_id="request-objective-stage",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            resolved_route={"backend": "hermes"},
            approval_required=False,
            objective_id="go_test",
            stage_key="prepare_asset",
        )
        assert delegation["objective_id"] == "go_test"
        assert delegation["stage_key"] == "prepare_asset"
        stage = conn.execute(
            """
            SELECT status, delegation_id FROM grace_objective_stages
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """
        ).fetchone()
        assert stage["status"] == "queued"
        assert stage["delegation_id"] == delegation["delegation_id"]


def test_delegation_reservation_declares_retry_stage_when_requested_stage_is_bound(tmp_path):
    with kb.connect_closing(tmp_path / "objective-reserve-retry.db") as conn:
        _create_objective(conn)
        first = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="b" * 64,
            request_instance_id="request-objective-stage",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            resolved_route={"backend": "hermes"},
            approval_required=False,
            objective_id="go_test",
            stage_key="publish_page",
        )

        second = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="c" * 64,
            request_instance_id="request-objective-stage-recovery",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-2",
            resolved_route={"backend": "hermes"},
            approval_required=False,
            objective_id="go_test",
            stage_key="publish_page",
        )
        replay = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="c" * 64,
            request_instance_id="request-objective-stage-recovery",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-2",
            resolved_route={"backend": "hermes"},
            approval_required=False,
            objective_id="go_test",
            stage_key="publish_page",
        )

        assert first["stage_key"] == "publish_page"
        assert second["stage_key"] == "publish_page_r2"
        assert replay["delegation_id"] == second["delegation_id"]
        assert replay["stage_key"] == "publish_page_r2"
        rows = conn.execute(
            """
            SELECT stage_key, delegation_id, status, outcome_kind FROM grace_objective_stages
             WHERE objective_id = 'go_test'
             ORDER BY position ASC
            """
        ).fetchall()
        assert [
            (
                row["stage_key"],
                row["delegation_id"],
                row["status"],
                row["outcome_kind"],
            )
            for row in rows
        ] == [
            ("prepare_asset", None, "planned", None),
            (
                "publish_page",
                first["delegation_id"],
                "done",
                "superseded_by_retry",
            ),
            ("publish_page_r2", second["delegation_id"], "queued", None),
            ("share_group", None, "planned", None),
        ]


def test_objective_can_append_recovery_stage_before_terminal(tmp_path):
    with kb.connect_closing(tmp_path / "objective-append-stage.db") as conn:
        _create_objective(conn)
        objective = kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_retry_1",
            next_action="Retry read-only preflight after runtime recovery.",
        )

        assert objective["current_stage_key"] == "prepare_retry_1"
        assert objective["next_action"] == (
            "Retry read-only preflight after runtime recovery."
        )
        assert objective["required_stage_keys"] == (
            '["prepare_asset","publish_page","prepare_retry_1","share_group"]'
        )

        stages = conn.execute(
            """
            SELECT stage_key, position, status FROM grace_objective_stages
             WHERE objective_id = 'go_test'
             ORDER BY position ASC
            """
        ).fetchall()
        assert [row["stage_key"] for row in stages] == [
            "prepare_asset",
            "publish_page",
            "prepare_retry_1",
            "share_group",
        ]
        assert stages[2]["status"] == "planned"


def test_adding_recovery_stage_reactivates_blocked_objective(tmp_path):
    with kb.connect_closing(tmp_path / "objective-reactivate-stage.db") as conn:
        _create_objective(conn)
        now = int(time.time())
        conn.execute(
            """
            UPDATE grace_objectives
               SET status = 'blocked',
                   waiting_for = 'prior terminal blocker',
                   completed_at = ?,
                   updated_at = ?
             WHERE objective_id = 'go_test'
            """,
            (now, now),
        )

        objective = kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_recovery_1",
            next_action="Recover durable evidence before retry.",
        )

        assert objective["status"] == "active"
        assert objective["current_stage_key"] == "prepare_recovery_1"
        assert objective["waiting_for"] == ""
        assert objective["completed_at"] is None


def test_stage_mode_declares_new_recovery_stage_family(tmp_path):
    with kb.connect_closing(tmp_path / "objective-stage-mode-recovery.db") as conn:
        _create_objective(conn)
        mode = kb.grace_objective_stage_mode(
            conn,
            objective_id="go_test",
            stage_key="prepare_canonical_url_per_group",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
        )

        objective = kb.get_grace_objective(conn, "go_test")
        stages = conn.execute(
            """
            SELECT stage_key, status FROM grace_objective_stages
             WHERE objective_id = 'go_test'
             ORDER BY position ASC
            """
        ).fetchall()

        assert mode == "intermediate"
        assert objective["current_stage_key"] == "prepare_canonical_url_per_group"
        assert [row["stage_key"] for row in stages] == [
            "prepare_asset",
            "publish_page",
            "prepare_canonical_url_per_group",
            "share_group",
        ]


def test_stage_mode_rejects_undeclared_non_recovery_stage_family(tmp_path):
    with kb.connect_closing(tmp_path / "objective-stage-mode-invalid.db") as conn:
        _create_objective(conn)
        with pytest.raises(
            ValueError,
            match="stage is not declared in required_stage_keys",
        ):
            kb.grace_objective_stage_mode(
                conn,
                objective_id="go_test",
                stage_key="execute_social_action",
                platform="telegram",
                chat_id="chat-1",
                thread_id="4641",
            )


def test_stage_mode_validates_recovery_without_persisting(tmp_path):
    with kb.connect_closing(tmp_path / "objective-stage-mode-readonly.db") as conn:
        _create_objective(conn)

        mode = kb.grace_objective_stage_mode(
            conn,
            objective_id="go_test",
            stage_key="repair_audio_brief_episode_07",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            ensure_stage=False,
        )

        objective = kb.get_grace_objective(conn, "go_test")
        stage = conn.execute(
            "SELECT 1 FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='repair_audio_brief_episode_07'"
        ).fetchone()
        assert mode == "intermediate"
        assert objective["current_stage_key"] == "prepare_asset"
        assert stage is None


def test_delegation_reservation_rolls_back_new_stage_on_failure(tmp_path):
    with kb.connect_closing(tmp_path / "objective-stage-rollback.db") as conn:
        _create_objective(conn)
        conn.execute(
            "CREATE TRIGGER reject_test_delegation BEFORE INSERT ON grace_delegations "
            "BEGIN SELECT RAISE(ABORT, 'test delegation rejection'); END"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="test delegation rejection"):
            kb.reserve_grace_delegation(
                conn,
                contract_fingerprint="9" * 64,
                request_instance_id="request-stage-rollback",
                platform="telegram",
                chat_id="chat-1",
                thread_id="4641",
                session_key="agent:main:telegram:group:chat-1:4641",
                session_id="session-stage-rollback",
                resolved_route={"backend": "openclaw"},
                approval_required=False,
                objective_id="go_test",
                stage_key="repair_audio_brief_episode_07",
            )

        objective = kb.get_grace_objective(conn, "go_test")
        stage = conn.execute(
            "SELECT 1 FROM grace_objective_stages "
            "WHERE objective_id='go_test' AND stage_key='repair_audio_brief_episode_07'"
        ).fetchone()
        delegation = conn.execute(
            "SELECT 1 FROM grace_delegations WHERE contract_fingerprint=?",
            ("9" * 64,),
        ).fetchone()
        assert objective["current_stage_key"] == "prepare_asset"
        assert stage is None
        assert delegation is None


def test_appending_recovery_stage_supersedes_accepted_prior_prepare(tmp_path):
    with kb.connect_closing(tmp_path / "objective-supersede-stage.db") as conn:
        _create_objective(conn)
        execution_id = kb.create_task(conn, title="prepare execution")
        assert kb.complete_task(conn, execution_id, summary="fail closed")
        review_id = kb.create_task(
            conn,
            title="prepare review",
            parents=(execution_id,),
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted fail-closed",
            metadata={"review_outcome": "accepted"},
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'queued',
                   delegation_id = 'gd-old',
                   execution_task_id = ?,
                   review_task_id = ?
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """,
            (execution_id, review_id),
        )

        objective = kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_retry_2",
            next_action="Retry preflight.",
        )

        assert objective["current_stage_key"] == "prepare_retry_2"
        rows = conn.execute(
            """
            SELECT stage_key, status, outcome_kind FROM grace_objective_stages
             WHERE objective_id = 'go_test'
             ORDER BY position ASC
            """
        ).fetchall()
        assert [(row["stage_key"], row["status"], row["outcome_kind"]) for row in rows] == [
            ("prepare_asset", "done", "superseded_by_retry"),
            ("publish_page", "planned", None),
            ("prepare_retry_2", "planned", None),
            ("share_group", "planned", None),
        ]


def test_available_stage_key_skips_done_or_bound_retry_stage(tmp_path):
    with kb.connect_closing(tmp_path / "objective-next-retry-stage.db") as conn:
        _create_objective(conn)
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'done',
                   delegation_id = 'gd-used',
                   outcome_kind = 'intermediate_blocked'
             WHERE objective_id = 'go_test' AND stage_key = 'prepare_asset'
            """
        )

        assert (
            kb.available_grace_objective_stage_key(
                conn,
                objective_id="go_test",
                stage_key="prepare_asset",
            )
            == "prepare_asset_r2"
        )
        objective = kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key="prepare_asset_r2",
            next_action="Retry again.",
        )

        assert objective["current_stage_key"] == "prepare_asset_r2"
        stages = conn.execute(
            """
            SELECT stage_key FROM grace_objective_stages
             WHERE objective_id = 'go_test'
             ORDER BY position ASC
            """
        ).fetchall()
        assert [row["stage_key"] for row in stages] == [
            "prepare_asset",
            "publish_page",
            "prepare_asset_r2",
            "share_group",
        ]


def test_available_stage_key_reuses_exact_unbound_stage_with_retry_suffix(tmp_path):
    with kb.connect_closing(tmp_path / "objective-exact-suffixed-stage.db") as conn:
        _create_objective(conn)
        stage_key = "package_schema_repair_case_r14"
        kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key=stage_key,
        )

        assert kb.available_grace_objective_stage_key(
            conn,
            objective_id="go_test",
            stage_key=stage_key,
        ) == stage_key


@pytest.mark.parametrize(
    ("used_suffix", "next_suffix"),
    (("r14", "r15"), ("r25", "r26"), ("r99", "r100")),
)
def test_bound_suffixed_stage_advances_declared_retry_family(
    tmp_path,
    used_suffix,
    next_suffix,
):
    with kb.connect_closing(tmp_path / "objective-advance-suffixed-stage.db") as conn:
        _create_objective(conn)
        stage_key = f"package_schema_repair_case_{used_suffix}"
        kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_test",
            stage_key=stage_key,
        )
        conn.execute(
            "UPDATE grace_objective_stages "
            "SET status='done', delegation_id='gd-used' "
            "WHERE objective_id='go_test' AND stage_key=?",
            (stage_key,),
        )

        retry_stage_key = kb.available_grace_objective_stage_key(
            conn,
            objective_id="go_test",
            stage_key=stage_key,
        )

        assert retry_stage_key == f"package_schema_repair_case_{next_suffix}"
        assert kb.grace_objective_stage_mode(
            conn,
            objective_id="go_test",
            stage_key=retry_stage_key,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            ensure_stage=False,
        ) == "intermediate"


def test_retry_stage_key_uses_root_family_instead_of_nesting(tmp_path):
    with kb.connect_closing(tmp_path / "objective-root-retry-stage.db") as conn:
        _create_objective(conn)
        now = int(time.time())
        for position, stage_key in enumerate(
            ("prepare_asset_r2", "prepare_asset_r2_r2"),
            start=3,
        ):
            conn.execute(
                """
                INSERT INTO grace_objective_stages (
                    objective_id, stage_key, position, status, delegation_id,
                    created_at, updated_at
                ) VALUES ('go_test', ?, ?, 'done', ?, ?, ?)
                """,
                (stage_key, position, f"gd-{stage_key}", now, now),
            )

        assert (
            kb.available_grace_objective_stage_key(
                conn,
                objective_id="go_test",
                stage_key="prepare_asset_r2_r2",
            )
            == "prepare_asset_r3"
        )


def test_delegation_stage_match_accepts_same_root_retry():
    assert kb._grace_objective_stage_matches_request(
        "prepare_asset_r3",
        "prepare_asset",
    )
    assert kb._grace_objective_stage_matches_request(
        "prepare_asset_r3",
        "prepare_asset_r2",
    )
    assert not kb._grace_objective_stage_matches_request(
        "prepare_asset",
        "prepare_asset_r2",
    )


def test_terminal_retry_declaration_preserves_bound_source_until_binding(tmp_path):
    with kb.connect_closing(tmp_path / "objective-terminal-retry.db") as conn:
        kb.create_grace_objective(
            conn,
            objective_id="go_terminal",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            title="Execute external action",
            objective="Publish to verified destinations.",
            original_request_sha256="a" * 64,
            required_stage_keys=("execute_external_action",),
            terminal_stage_key="execute_external_action",
            acceptance_criteria=("External result verified",),
            current_stage_key="execute_external_action",
            next_action="Execute external action.",
        )
        first = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="1" * 64,
            request_instance_id="request-terminal",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            resolved_route={"backend": "hermes"},
            approval_required=False,
            objective_id="go_terminal",
            stage_key="execute_external_action",
        )

        mode = kb.grace_objective_stage_mode(
            conn,
            objective_id="go_terminal",
            stage_key="execute_external_action_r2",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
        )
        objective = kb.get_grace_objective(conn, "go_terminal")
        stages = conn.execute(
            """
            SELECT stage_key, delegation_id, status, outcome_kind
              FROM grace_objective_stages
             WHERE objective_id = 'go_terminal'
             ORDER BY position ASC
            """
        ).fetchall()

        assert first["stage_key"] == "execute_external_action"
        assert mode == "terminal"
        assert objective["terminal_stage_key"] == "execute_external_action_r2"
        assert objective["current_stage_key"] == "execute_external_action_r2"
        assert [
            (row["stage_key"], row["delegation_id"], row["status"], row["outcome_kind"])
            for row in stages
        ] == [
            (
                "execute_external_action",
                first["delegation_id"],
                "queued",
                None,
            ),
            ("execute_external_action_r2", None, "planned", None),
        ]

    # A declaration is not an execution; source history remains until binding.

def test_terminal_retry_stage_family_can_advance_from_existing_retry(tmp_path):
    with kb.connect_closing(tmp_path / "objective-terminal-family-retry.db") as conn:
        kb.create_grace_objective(
            conn,
            objective_id="go_terminal",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            title="Execute external action",
            objective="Publish to verified destinations.",
            original_request_sha256="a" * 64,
            required_stage_keys=("execute_external_action",),
            terminal_stage_key="execute_external_action",
            acceptance_criteria=("External result verified",),
            current_stage_key="execute_external_action",
            next_action="Execute external action.",
        )
        kb.ensure_grace_objective_stage(
            conn,
            objective_id="go_terminal",
            stage_key="execute_external_action_r2",
            next_action="Retry terminal action.",
        )

        mode = kb.grace_objective_stage_mode(
            conn,
            objective_id="go_terminal",
            stage_key="execute_external_action_r3",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
        )
        objective = kb.get_grace_objective(conn, "go_terminal")
        stages = conn.execute(
            """
            SELECT stage_key, status, outcome_kind
              FROM grace_objective_stages
             WHERE objective_id = 'go_terminal'
             ORDER BY position ASC
            """
        ).fetchall()

        assert mode == "terminal"
        assert objective["terminal_stage_key"] == "execute_external_action_r3"
        assert objective["current_stage_key"] == "execute_external_action_r3"
        assert [row["stage_key"] for row in stages] == [
            "execute_external_action",
            "execute_external_action_r2",
            "execute_external_action_r3",
        ]


def test_delegation_reservation_retries_bound_terminal_stage(tmp_path):
    with kb.connect_closing(tmp_path / "objective-terminal-reserve-retry.db") as conn:
        kb.create_grace_objective(
            conn,
            objective_id="go_terminal",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            title="Execute external action",
            objective="Publish to verified destinations.",
            original_request_sha256="a" * 64,
            required_stage_keys=("execute_external_action",),
            terminal_stage_key="execute_external_action",
            acceptance_criteria=("External result verified",),
            current_stage_key="execute_external_action",
            next_action="Execute external action.",
        )
        first = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="1" * 64,
            request_instance_id="request-terminal",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-1",
            resolved_route={"backend": "hermes"},
            approval_required=False,
            objective_id="go_terminal",
            stage_key="execute_external_action",
        )
        second = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="2" * 64,
            request_instance_id="request-terminal-retry",
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key="agent:main:telegram:group:chat-1:4641",
            session_id="session-2",
            resolved_route={"backend": "hermes"},
            approval_required=False,
            objective_id="go_terminal",
            stage_key="execute_external_action",
        )

        objective = kb.get_grace_objective(conn, "go_terminal")
        old_stage = conn.execute(
            """
            SELECT status, outcome_kind
              FROM grace_objective_stages
             WHERE objective_id = 'go_terminal'
               AND stage_key = 'execute_external_action'
            """
        ).fetchone()
        new_stage = conn.execute(
            """
            SELECT status, delegation_id
              FROM grace_objective_stages
             WHERE objective_id = 'go_terminal'
               AND stage_key = 'execute_external_action_r2'
            """
        ).fetchone()

        assert first["stage_key"] == "execute_external_action"
        assert second["stage_key"] == "execute_external_action_r2"
        assert objective["terminal_stage_key"] == "execute_external_action_r2"
        assert old_stage["status"] == "done"
        assert old_stage["outcome_kind"] == "superseded_by_retry"
        assert new_stage["status"] == "queued"
        assert new_stage["delegation_id"] == second["delegation_id"]


def test_terminal_stage_cannot_close_before_required_stages(tmp_path):
    with kb.connect_closing(tmp_path / "objective-close.db") as conn:
        _create_objective(conn)
        review_id, event_id = _accepted_callback(
            conn,
            objective_id="go_test",
            stage_key="share_group",
            requested_mode="intermediate",
        )
        callback = kb.get_grace_loop_callback(conn, review_id)
        assert callback["completion_mode"] == "terminal"
        with pytest.raises(ValueError, match="required stages complete"):
            kb.record_grace_loop_callback_outcome(
                conn,
                review_task_id=review_id,
                event_id=event_id,
                platform="telegram",
                chat_id="chat-1",
                thread_id="4641",
                session_id="session-1",
                lease_owner="owner-a",
                outcome_kind="closed",
                payload={"summary": "all done"},
            )


def test_terminal_stage_can_resume_existing_incomplete_objective_stage(tmp_path):
    with kb.connect_closing(tmp_path / "objective-resume-existing.db") as conn:
        _create_objective(conn)
        execution_id = kb.create_task(conn, title="existing publish execution")
        review_id = kb.create_task(
            conn,
            title="existing publish review",
            parents=(execution_id,),
        )
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO grace_delegations (
                delegation_id, contract_fingerprint, request_instance_id,
                platform, chat_id, thread_id, session_key, session_id,
                resolved_route, approval_required, state,
                execution_task_id, review_task_id, objective_id, stage_key,
                created_at, updated_at
            ) VALUES (
                'gd-existing-publish', ?, 'request-existing-publish',
                'telegram', 'chat-1', '4641', ?, 'session-old', '{}', 0,
                'queued', ?, ?, 'go_test', 'publish_page', ?, ?
            )
            """,
            (
                "f" * 64,
                "agent:main:telegram:group:chat-1:4641",
                execution_id,
                review_id,
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET delegation_id = 'gd-existing-publish', status = 'queued',
                   execution_task_id = ?, review_task_id = ?, updated_at = ?
             WHERE objective_id = 'go_test' AND stage_key = 'publish_page'
            """,
            (execution_id, review_id, now),
        )
        terminal_review_id, event_id = _accepted_callback(
            conn,
            objective_id="go_test",
            stage_key="share_group",
            requested_mode="terminal",
        )

        kb.record_grace_loop_callback_outcome(
            conn,
            review_task_id=terminal_review_id,
            event_id=event_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_id="session-1",
            lease_owner="owner-a",
            outcome_kind="continued",
            payload={
                "delegation_id": "gd-existing-publish",
                "execution_task_id": execution_id,
                "review_task_id": review_id,
                "next_action": "Resolve the existing Page verification blocker.",
            },
        )

        objective = kb.get_grace_objective(conn, "go_test")
        assert objective["status"] == "active"
        assert objective["current_stage_key"] == "publish_page"
        terminal_stage = conn.execute(
            """
            SELECT status, outcome_kind FROM grace_objective_stages
             WHERE objective_id = 'go_test' AND stage_key = 'share_group'
            """
        ).fetchone()
        assert terminal_stage["status"] == "done"
        assert terminal_stage["outcome_kind"] == "continued"


def test_terminal_stage_closes_only_after_all_prior_stages(tmp_path):
    with kb.connect_closing(tmp_path / "objective-complete.db") as conn:
        _create_objective(conn)
        conn.execute(
            """
            UPDATE grace_objective_stages
               SET status = 'done', completed_at = ?, updated_at = ?
             WHERE objective_id = 'go_test' AND stage_key IN ('prepare_asset', 'publish_page')
            """,
            (int(time.time()), int(time.time())),
        )
        review_id, event_id = _accepted_callback(
            conn,
            objective_id="go_test",
            stage_key="share_group",
            requested_mode="terminal",
        )
        kb.record_grace_loop_callback_outcome(
            conn,
            review_task_id=review_id,
            event_id=event_id,
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_id="session-1",
            lease_owner="owner-a",
            outcome_kind="closed",
            payload={"summary": "Page and Group evidence verified"},
        )
        objective = kb.get_grace_objective(conn, "go_test")
        assert objective["status"] == "completed"
        assert objective["completed_at"] is not None


def test_active_objective_is_injected_outside_compaction_history(tmp_path, monkeypatch):
    db_path = tmp_path / "objective-prompt.db"
    with kb.connect_closing(db_path) as conn:
        _create_objective(conn, objective_id="go_prompt")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    prompt = active_objectives_prompt(
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
    )
    assert "[Trusted active Grace objectives]" in prompt
    assert "go_prompt" in prompt
    assert "not historical compaction text" in prompt
    assert '"current_stage_key": "prepare_asset"' in prompt


def test_active_objective_prompt_isolates_stale_sibling_pin(tmp_path, monkeypatch):
    from proactive.behavior_profiles.registry import BehaviorProfileError
    from hermes_cli import objective_workflow

    db_path = tmp_path / "objective-prompt-stale-sibling.db"
    with kb.connect_closing(db_path) as conn:
        _create_objective(conn, objective_id="go_current")
        _create_objective(conn, objective_id="go_stale")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    def fake_progress(_conn, objective_id):
        if objective_id == "go_stale":
            raise BehaviorProfileError("behavior.manifest_mismatch")
        return {"complete": False, "verified": True}

    monkeypatch.setattr(objective_workflow, "progress", fake_progress)
    prompt = active_objectives_prompt(
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
    )
    rendered = {
        row["objective_id"]: row
        for row in json.loads(prompt.rsplit("\n", 1)[1])
    }

    assert rendered["go_current"]["publication_progress"] == {
        "complete": False,
        "verified": True,
    }
    assert "control_plane_blocker" not in rendered["go_current"]
    assert rendered["go_stale"]["publication_progress"] is None
    assert rendered["go_stale"]["control_plane_blocker"] == {
        "advance_allowed": False,
        "code": "behavior.manifest_mismatch",
        "operator_action_required": True,
    }
    assert "do not plan, delegate, approve, execute, or otherwise advance it" in prompt


def test_active_objective_prompt_does_not_isolate_unclassified_failure(
    tmp_path, monkeypatch
):
    from hermes_cli import objective_workflow

    db_path = tmp_path / "objective-prompt-unclassified-failure.db"
    with kb.connect_closing(db_path) as conn:
        _create_objective(conn, objective_id="go_broken")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        objective_workflow,
        "progress",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("unexpected storage failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected storage failure"):
        active_objectives_prompt(
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
        )


@pytest.mark.parametrize('fault', ['valid', 'valid_native', 'valid_triage', 'successor_ready', 'successor_event', 'native_effect', 'native_card', 'callback_fingerprint', 'successor_fingerprint', 'snapshot_fingerprint', 'stale_event', 'lease', 'effect', 'successor_pending', 'foreign_topic', 'successor_foreign_topic', 'accepted_review', 'current_cursor', 'task_binding'])
def test_historical_failed_callback_repair_preserves_rejection_and_retry_history(tmp_path, fault):
    from proactive.loop_contract import contract_fingerprint
    with kb.connect_closing(tmp_path / 'failed-handoff.db') as conn:
        _create_objective(conn)
        cards = [kb.create_task(conn, title=name) for name in ('source', 'source review', 'repair', 'repair review')]
        se, sr, ne, nr = cards
        events = []
        for stage, execution, review, suffix in [('prepare_asset', se, sr, 'old'), ('publish_page', ne, nr, 'repair')]:
            _bind_queued_delegation(conn, execution, review, suffix=suffix)
            contract = {'external_effect_budget': 0, 'external_targets': [], 'objective_ref': {'stage_key': stage}}
            fingerprint = contract_fingerprint(contract)
            conn.execute('UPDATE grace_delegations SET objective_id=?,stage_key=?,contract_snapshot=?,contract_fingerprint=? WHERE delegation_id=?', ('go_test', stage, json.dumps(contract), fingerprint, f'gd-{suffix}'))
            conn.execute('UPDATE grace_objective_stages SET status=?,delegation_id=?,execution_task_id=?,review_task_id=? WHERE objective_id=? AND stage_key=?', ('queued', f'gd-{suffix}', execution, review, 'go_test', stage))
            kb.add_grace_loop_callback(conn, review_task_id=review, execution_task_id=execution, platform='telegram', chat_id='chat-1', thread_id='4641', session_key='agent:main:telegram:group:chat-1:4641', session_id='session-1', contract_fingerprint=fingerprint, completion_mode='intermediate', objective_id='go_test', stage_key=stage)
            conn.execute('UPDATE tasks SET status=? WHERE id IN (?,?)', ('ready', execution, review))
            assert kb.block_task(conn, review, reason='review rejected', kind='capability')
            conn.execute('UPDATE task_runs SET metadata=? WHERE task_id=?', (json.dumps({'review_outcome': 'rejected'}), review))
            events.append(conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=? AND kind=?', (review, 'blocked')).fetchone()[0])
            conn.execute('UPDATE tasks SET status=? WHERE id=?', ('done', execution))
        error = 'Grace callback delivery failed: ValueError: Objective current stage changed without an exact callback successor'
        conn.execute('UPDATE grace_loop_callbacks SET state=?,attempts=3,last_error=?,attempt_event_id=? WHERE review_task_id=?', ('attention', error, events[0], sr))
        conn.execute('UPDATE grace_objective_stages SET status=?,outcome_kind=?,completed_at=123 WHERE objective_id=? AND stage_key=?', ('done', 'intermediate_blocked', 'go_test', 'publish_page'))
        conn.execute('UPDATE grace_loop_callbacks SET state=?,outcome_kind=?,outcome_event_id=? WHERE review_task_id=?', ('delivered', 'intermediate_blocked', events[1], nr))
        conn.execute('UPDATE grace_objectives SET status=?,current_stage_key=? WHERE objective_id=?', ('blocked', 'publish_page', 'go_test'))
        if fault in ('valid_native', 'native_effect', 'native_card'):
            card = {'external_effect_budget': 0, 'objective_ref': {'stage_key': 'prepare_asset'}}
            body = 'GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n' + json.dumps(card) + '\n```'
            conn.execute('UPDATE tasks SET body=? WHERE id=?', (body, se))
            native_fingerprint = 'b'*64
            conn.execute('UPDATE grace_loop_callbacks SET contract_fingerprint=? WHERE review_task_id=?', (native_fingerprint, sr))
            kb._synthesize_ended_run(conn, se, outcome='completed', summary='native normalization phase', metadata={'contract_fingerprint': native_fingerprint, 'execution_card_fingerprint': contract_fingerprint(card), 'external_effect_budget': 1 if fault == 'native_effect' else 0})
            if fault == 'native_card': conn.execute('UPDATE tasks SET body=? WHERE id=?', (body.replace('0', '1', 1), se))
        if fault == 'valid_triage': conn.execute('UPDATE tasks SET status=? WHERE id=?', ('triage', nr))
        elif fault == 'successor_ready': conn.execute('UPDATE tasks SET status=? WHERE id=?', ('ready', nr))
        elif fault == 'successor_event': kb._append_event(conn, nr, 'unblocked', {})
        elif fault == 'callback_fingerprint': conn.execute('UPDATE grace_loop_callbacks SET contract_fingerprint=? WHERE review_task_id=?', ('0'*64, sr))
        elif fault == 'successor_fingerprint': conn.execute('UPDATE grace_loop_callbacks SET contract_fingerprint=? WHERE review_task_id=?', ('0'*64, nr))
        elif fault == 'snapshot_fingerprint': conn.execute('UPDATE grace_delegations SET contract_fingerprint=? WHERE delegation_id=?', ('0'*64, 'gd-old'))
        elif fault == 'stale_event': kb._append_event(conn, sr, 'blocked', {'reason':'newer rejection'}, run_id=conn.execute('SELECT MAX(id) FROM task_runs WHERE task_id=?',(sr,)).fetchone()[0])
        elif fault == 'lease': conn.execute('UPDATE grace_loop_callbacks SET lease_owner=? WHERE review_task_id=?', ('active', sr))
        elif fault == 'effect': conn.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,created_at,updated_at) VALUES (?, 'facebook', 'unexpected', 'verified', 1, 1)", (se,))
        elif fault == 'successor_pending': conn.execute('UPDATE grace_loop_callbacks SET state=? WHERE review_task_id=?', ('pending', nr))
        elif fault == 'foreign_topic': conn.execute('UPDATE grace_loop_callbacks SET thread_id=? WHERE review_task_id=?', ('other', sr))
        elif fault == 'successor_foreign_topic': conn.execute('UPDATE grace_loop_callbacks SET thread_id=? WHERE review_task_id=?', ('other', nr))
        elif fault == 'accepted_review': conn.execute('UPDATE task_runs SET metadata=? WHERE task_id=?', (json.dumps({'review_outcome':'accepted'}), sr))
        elif fault == 'current_cursor': conn.execute('UPDATE grace_objectives SET current_stage_key=? WHERE objective_id=?', ('share_group', 'go_test'))
        elif fault == 'task_binding': conn.execute('UPDATE grace_delegations SET review_task_id=? WHERE delegation_id=?', (nr, 'gd-old'))
        before = [dict(r) for r in conn.execute('SELECT * FROM task_runs ORDER BY id')]
        receipt = {'authorization_id':'owner-system-repair', 'reason':'archive superseded failed delivery', 'objective_id':'go_test', 'stage_successors':{'prepare_asset':'publish_page'}}
        args = dict(objective_id='go_test', stage_successors=receipt['stage_successors'], repair_receipt=receipt)
        if fault not in ('valid', 'valid_native', 'valid_triage'):
            with pytest.raises(ValueError): kb.repair_superseded_cancelled_grace_objective_stages(conn, **args)
            assert kb.get_grace_loop_callback(conn, sr)['state'] == 'attention'
        else:
            kb.repair_superseded_cancelled_grace_objective_stages(conn, **args)
            cb = kb.get_grace_loop_callback(conn, sr)
            assert cb['state'] == 'cancelled' and cb['outcome_kind'] == 'superseded_by_retry'
            assert cb['attempts'] == 3 and cb['last_error'] == error
            assert kb.get_grace_objective(conn, 'go_test')['current_stage_key'] == 'publish_page'
        assert before == [dict(r) for r in conn.execute('SELECT * FROM task_runs ORDER BY id')]
