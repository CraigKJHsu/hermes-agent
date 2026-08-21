import time

import pytest

from hermes_cli import kanban_db as kb


def _grace_loop_pair(conn):
    execution_id = kb.create_task(
        conn,
        title="execution",
        body="GRACE_LOOP_CONTRACT_STAGE: execution",
        assignee="clawops-content",
    )
    review_id = kb.create_task(
        conn,
        title="Grace review",
        body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
        assignee="default",
        parents=(execution_id,),
    )
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO grace_delegations (
            delegation_id, contract_fingerprint, request_instance_id,
            platform, chat_id, thread_id, session_key, session_id,
            resolved_route, approval_required, state,
            execution_task_id, review_task_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'telegram', 'chat-1', '2', ?, ?, '{}', 0,
                  'queued', ?, ?, ?, ?)
        """,
        (
            f"gd_{execution_id}",
            (execution_id.replace("t_", "") * 8)[:64],
            f"gri_{execution_id}",
            f"loop:{execution_id}",
            f"loop-session:{execution_id}",
            execution_id,
            review_id,
            now,
            now,
        ),
    )
    conn.commit()
    return execution_id, review_id


def test_review_required_block_is_rejected_when_grace_review_exists(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)

        with pytest.raises(ValueError, match="would deadlock"):
            kb.block_task(
                conn,
                execution_id,
                reason="review-required: deliverable needs human eyes",
                kind="needs_input",
            )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)
        violation = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'grace_loop_protocol_violation'",
            (execution_id,),
        ).fetchone()

    assert execution.status == "ready"
    assert review.status == "todo"
    assert violation is not None
    assert review_id in violation["payload"]


def test_genuine_block_is_allowed_when_grace_review_exists(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)

        assert kb.block_task(
            conn,
            execution_id,
            reason="Missing a KJ product-name decision",
            kind="needs_input",
        )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)

    assert execution.status == "blocked"
    assert review.status == "todo"


def test_embedded_stage_text_does_not_create_grace_loop_behavior(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        parent = kb.create_task(
            conn,
            title="ordinary parent",
            body="Documentation quotes GRACE_LOOP_CONTRACT_STAGE: execution",
            assignee="worker",
        )
        child = kb.create_task(
            conn,
            title="ordinary child",
            body="Evidence quotes GRACE_LOOP_CONTRACT_STAGE: grace_review",
            assignee="worker",
            parents=(parent,),
        )
        assert kb.complete_task(conn, parent, summary="ordinary result")
        claimed_child = kb.claim_task(conn, child)
        assert claimed_child is not None
        assert kb.block_task(
            conn,
            child,
            reason="ordinary dependency",
            kind="dependency",
            expected_run_id=claimed_child.current_run_id,
        )

        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, parent).result is None
        assert (
            conn.execute(
                "SELECT 1 FROM task_events "
                "WHERE task_id = ? AND kind = 'grace_correction_requested'",
                (parent,),
            ).fetchone()
            is None
        )


def test_completion_promotes_dependent_grace_review(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)

        assert kb.complete_task(
            conn,
            execution_id,
            summary="deliverables verified",
            metadata={"approval_needed": ["public deployment"]},
        )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)

    assert execution.status == "done"
    assert review.status == "ready"


def test_rejected_grace_review_reopens_execution_for_correction(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = 120 WHERE id = ?",
                (execution_id,),
            )
            kb._append_event(
                conn,
                execution_id,
                "runtime_finalization_requested",
                {
                    "original_limit_seconds": 1200,
                    "finalization_budget_seconds": 120,
                },
            )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="first attempt",
            result="stale result",
        )
        review = kb.claim_task(conn, review_id)
        assert review is not None
        kb.add_comment(
            conn,
            review_id,
            author="default",
            body="Only disable job 4a6d50ce6d18 and preserve read-only checks.",
        )

        assert kb.block_task(
            conn,
            review_id,
            reason="Scheduled distribution job is still enabled",
            kind="dependency",
            expected_run_id=review.current_run_id,
        )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)
        correction_comment = conn.execute(
            "SELECT author, body FROM task_comments "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        correction_event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'grace_correction_requested' "
            "ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()

        assert execution.status == "ready"
        assert execution.completed_at is None
        assert execution.result == "stale result"
        assert execution.max_runtime_seconds == 1200
        assert review.status == "todo"
        assert review.block_kind == "dependency"
        assert correction_comment["author"] == "Grace review"
        assert "Scheduled distribution job is still enabled" in correction_comment["body"]
        assert "CORRECTION_MODE: reconciliation_first" in correction_comment["body"]
        assert "kanban_external_effect" in correction_comment["body"]
        assert "4a6d50ce6d18" not in correction_comment["body"]
        assert correction_event is not None
        assert review_id in correction_event["payload"]
        assert "reconciliation_first" in correction_event["payload"]
        assert (
            conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? "
                "AND kind = 'runtime_finalization_cleared' "
                "ORDER BY id DESC LIMIT 1",
                (execution_id,),
            ).fetchone()
            is not None
        )
        assert kb.check_respawn_guard(conn, execution_id) is None

        # A dispatcher promotion pass must not immediately re-run the review:
        # its execution parent is open again and must finish correction first.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, review_id).status == "todo"

        assert kb.complete_task(
            conn,
            execution_id,
            summary="correction verified",
        )
        assert kb.get_task(conn, review_id).status == "ready"
        assert kb.check_respawn_guard(conn, execution_id) == "recent_success"


def test_rejected_saved_evidence_review_never_reopens_browser_mode(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = 600 WHERE id = ?",
                (execution_id,),
            )
            kb._append_event(
                conn,
                execution_id,
                "runtime_finalization_requested",
                {
                    "source": "saved_commerce_evidence_schema_resume",
                    "finalization_budget_seconds": 600,
                    "browser_allowed": False,
                },
            )
        assert kb.complete_task(
            conn,
            execution_id,
            summary="validated saved-evidence report",
        )
        review = kb.claim_task(conn, review_id)
        assert review is not None

        assert kb.block_task(
            conn,
            review_id,
            reason="review needs the complete structured parent report",
            kind="dependency",
            expected_run_id=review.current_run_id,
        )

        execution = kb.get_task(conn, execution_id)
        correction_comment = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        correction_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'grace_correction_requested' "
            "ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()

        assert execution.status == "ready"
        assert execution.max_runtime_seconds == 600
        assert "CORRECTION_MODE: saved_evidence_only" in (
            correction_comment["body"]
        )
        assert "Do not navigate, click, search" in correction_comment["body"]
        assert "saved_evidence_only" in correction_event["payload"]
        assert kb._runtime_finalization_state(conn, execution_id) is not None
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? "
            "AND kind = 'runtime_finalization_cleared'",
            (execution_id,),
        ).fetchone() is None


def test_grace_review_context_includes_cumulative_parent_evidence(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        first = kb.claim_task(conn, execution_id)
        assert first is not None
        assert kb.block_task(
            conn,
            execution_id,
            reason="Shopee still pending",
            kind="needs_input",
            expected_run_id=first.current_run_id,
        )
        kb.add_comment(
            conn,
            execution_id,
            author="clawops-browser",
            body=(
                "Facebook draft verified: title, AI disclosure, three images, "
                "and unpublished state were read back."
            ),
        )
        # The durable Facebook evidence must survive beyond the ordinary
        # worker-context comment tail.
        for index in range(kb._CTX_MAX_COMMENTS + 5):
            kb.add_comment(
                conn,
                execution_id,
                author="worker",
                body=f"later diagnostic note {index}",
            )
        assert kb.unblock_task(conn, execution_id)
        second = kb.claim_task(conn, execution_id)
        assert second is not None
        assert kb.complete_task(
            conn,
            execution_id,
            summary="Shopee product 50614873414 verified as unlisted.",
            metadata={
                "external_effects": [{
                    "platform": "shopee",
                    "state": "verified",
                    "external_id": "50614873414",
                    "details": {"published": False},
                }],
            },
            expected_run_id=second.current_run_id,
        )

        context = kb.build_worker_context(conn, review_id)

    assert "Cumulative evidence" in context
    assert "Facebook draft verified" in context
    assert "Shopee product 50614873414 verified as unlisted" in context
    assert "external effect ledger" in context
    assert '"external_id": "50614873414"' in context


def test_external_create_guard_requires_reconciliation_on_correction(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        initial = kb.claim_task(conn, execution_id)
        assert initial is not None
        assert kb.complete_task(
            conn,
            execution_id,
            summary="partial result",
            expected_run_id=initial.current_run_id,
        )
        review = kb.claim_task(conn, review_id)
        assert review is not None
        assert kb.block_task(
            conn,
            review_id,
            reason="Reconcile Facebook evidence",
            kind="dependency",
            expected_run_id=review.current_run_id,
        )
        correction = kb.claim_task(conn, execution_id)
        assert correction is not None

        create_url = "https://www.facebook.com/marketplace/create/item"
        denied = kb.reserve_external_create(
            conn,
            execution_id,
            create_url,
            expected_run_id=correction.current_run_id,
        )
        assert denied is not None
        assert "read-only lookup first" in denied

        effect = kb.record_external_effect(
            conn,
            execution_id,
            platform="facebook",
            state="absent_verified",
            details={"query": "Kolin KD-291M06", "matches": 0},
            expected_run_id=correction.current_run_id,
        )
        assert effect["state"] == "absent_verified"
        assert kb.reserve_external_create(
            conn,
            execution_id,
            create_url,
            expected_run_id=correction.current_run_id,
        ) is None
        with pytest.raises(ValueError, match="after durable create_started"):
            kb.record_external_effect(
                conn,
                execution_id,
                platform="facebook",
                state="absent_verified",
                expected_run_id=correction.current_run_id,
            )
        repeated = kb.reserve_external_create(
            conn,
            execution_id,
            create_url,
            expected_run_id=correction.current_run_id,
        )
        assert repeated is not None
        assert "already create_started" in repeated
        assert kb.block_task(
            conn,
            execution_id,
            reason="worker ended before creating an object",
            kind="needs_input",
            expected_run_id=correction.current_run_id,
        )
        assert kb.unblock_task(conn, execution_id)
        later_correction = kb.claim_task(conn, execution_id)
        assert later_correction is not None
        recovered = kb.record_external_effect(
            conn,
            execution_id,
            platform="facebook",
            state="absent_verified",
            expected_run_id=later_correction.current_run_id,
        )
        assert recovered["state"] == "absent_verified"
        assert recovered["run_id"] == later_correction.current_run_id


def test_terminal_external_effect_blocks_duplicate_create(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, _ = _grace_loop_pair(conn)
        run = kb.claim_task(conn, execution_id)
        assert run is not None
        kb.record_external_effect(
            conn,
            execution_id,
            platform="shopee",
            state="verified",
            external_id="50614873414",
            expected_run_id=run.current_run_id,
        )

        denied = kb.reserve_external_create(
            conn,
            execution_id,
            "https://seller.shopee.tw/portal/product/new",
            expected_run_id=run.current_run_id,
        )

    assert denied is not None
    assert "external_id=50614873414" in denied


def test_external_create_guard_rejects_stale_worker_run(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, _ = _grace_loop_pair(conn)
        stale_run = kb.claim_task(conn, execution_id)
        assert stale_run is not None
        assert kb.block_task(
            conn,
            execution_id,
            reason="retry",
            kind="needs_input",
            expected_run_id=stale_run.current_run_id,
        )
        assert kb.unblock_task(conn, execution_id)
        active_run = kb.claim_task(conn, execution_id)
        assert active_run is not None

        denied = kb.reserve_external_create(
            conn,
            execution_id,
            "https://www.facebook.com/marketplace/create/item",
            expected_run_id=stale_run.current_run_id,
        )

    assert denied is not None
    assert "not the active worker run" in denied


def test_external_create_url_requires_canonical_host():
    assert (
        kb.external_platform_for_url(
            "https://m.facebook.com/marketplace/create/item"
        )
        == "facebook"
    )
    assert (
        kb.external_platform_for_url(
            "https://seller.shopee.tw/portal/product/new"
        )
        == "shopee"
    )
    assert (
        kb.external_platform_for_url(
            "https://seller.shopee.tw.evil.example/portal/product/new"
        )
        is None
    )
