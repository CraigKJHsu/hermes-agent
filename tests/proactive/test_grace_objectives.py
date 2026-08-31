"""Durable Grace objective regression tests."""

from __future__ import annotations

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
    assert kb.complete_task(conn, execution_id, summary="done")
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
    assert kb.complete_task(
        conn,
        review_id,
        summary="accepted",
        metadata={"review_outcome": "accepted"},
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
            SELECT stage_key, delegation_id, status FROM grace_objective_stages
             WHERE objective_id = 'go_test'
             ORDER BY position ASC
            """
        ).fetchall()
        assert [(row["stage_key"], row["delegation_id"], row["status"]) for row in rows] == [
            ("prepare_asset", None, "planned"),
            ("publish_page", first["delegation_id"], "queued"),
            ("publish_page_r2", second["delegation_id"], "queued"),
            ("share_group", None, "planned"),
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


def test_terminal_retry_stage_becomes_new_terminal_and_supersedes_bound_terminal(tmp_path):
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
                "done",
                "superseded_by_retry",
            ),
            ("execute_external_action_r2", None, "planned", None),
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
