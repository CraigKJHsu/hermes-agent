import time
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

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


def test_controller_state_conflict_is_not_misclassified_as_human_input(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        assert kb.complete_task(conn, execution_id, summary="package ready")
        review = kb.claim_task(conn, review_id)
        assert review is not None

        assert kb.block_task(
            conn,
            review_id,
            reason="Controller state conflict: canonical report disagrees with schema",
            kind="needs_input",
            expected_run_id=review.current_run_id,
        )

        blocked = kb.get_task(conn, review_id)
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
            "ORDER BY id DESC LIMIT 1",
            (review_id,),
        ).fetchone()

    assert blocked.status == "blocked"
    assert blocked.block_kind == "capability"
    assert '"kind": "capability"' in event["payload"]


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
            review_outcome="rejected",
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
        assert review.status == "todo"
        assert review.block_kind == "dependency"
        assert review.block_recurrences == 1
        rejected_run = kb.latest_run(conn, review_id)
        assert rejected_run.status == "blocked"
        assert rejected_run.metadata["review_outcome"] == "rejected"
        assert correction_comment["author"] == "Grace review"
        assert "Scheduled distribution job is still enabled" in correction_comment["body"]
        assert "CORRECTION_MODE: reconciliation_first" in correction_comment["body"]
        assert "kanban_external_effect" in correction_comment["body"]
        assert "do not create a dummy effect record" in correction_comment["body"]
        assert "read_only_zero_external_effects=true" in correction_comment["body"]
        assert "4a6d50ce6d18" not in correction_comment["body"]
        assert correction_event is not None
        assert review_id in correction_event["payload"]
        assert "reconciliation_first" in correction_event["payload"]
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


def test_capability_rejected_review_records_verdict_without_acceptance(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        assert kb.complete_task(conn, execution_id, summary="diagnostic evidence")
        review = kb.claim_task(conn, review_id)
        assert review is not None
        assert kb.block_task(
            conn, review_id, reason="Controller receipt missing",
            kind="capability", expected_run_id=review.current_run_id,
            review_outcome="rejected",
        )
        rejected = kb.latest_run(conn, review_id)
        assert kb.get_task(conn, review_id).status == "blocked"
        assert rejected.status == "blocked"
        assert rejected.metadata["review_outcome"] == "rejected"
        assert not kb.grace_review_accepted(rejected.metadata)

        with pytest.raises(ValueError, match="Only a formal Grace review"):
            kb.block_task(
                conn, execution_id, reason="Not a review",
                kind="capability", review_outcome="rejected",
            )
        with pytest.raises(ValueError, match="requires a blocker reason"):
            kb.block_task(
                conn, review_id, kind="capability", review_outcome="rejected",
            )


def test_repeated_grace_review_correction_parks_without_reopening_parent(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        assert kb.complete_task(conn, execution_id, summary="attempt 1")

        review = kb.claim_task(conn, review_id)
        assert review is not None
        assert kb.block_task(
            conn,
            review_id,
            reason="same unresolved correction 1",
            kind="dependency",
            expected_run_id=review.current_run_id,
        )
        assert kb.get_task(conn, execution_id).status == "ready"
        assert kb.get_task(conn, review_id).block_recurrences == 1
        assert kb.complete_task(conn, execution_id, summary="attempt 2")

        review = kb.claim_task(conn, review_id)
        assert review is not None
        assert kb.block_task(
            conn,
            review_id,
            reason="same unresolved correction 2",
            kind="dependency",
            expected_run_id=review.current_run_id,
            review_outcome="rejected",
        )

        execution = kb.get_task(conn, execution_id)
        review = kb.get_task(conn, review_id)
        loop_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='block_loop_detected' ORDER BY id DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        correction_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? "
            "AND kind='grace_correction_requested'",
            (execution_id,),
        ).fetchone()[0]

    assert execution.status == "done"
    assert review.status == "triage"
    assert review.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    with kb.connect_closing(db_path) as conn:
        assert kb.latest_run(conn, review_id).metadata["review_outcome"] == "rejected"
    assert correction_count == kb.BLOCK_RECURRENCE_LIMIT - 1
    assert loop_event is not None
    assert '"scope": "grace_review_correction"' in loop_event["payload"]


def test_grace_review_correction_reopens_blocked_execution(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        execution = kb.claim_task(conn, execution_id)
        assert execution is not None
        assert kb.block_task(
            conn,
            execution_id,
            reason="deterministic renderer was not used",
            kind="capability",
            expected_run_id=execution.current_run_id,
        )
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=? AND status='todo'",
            (review_id,),
        )

        assert kb.block_task(
            conn,
            review_id,
            reason="retry with the required deterministic renderer",
            kind="dependency",
        )

        assert kb.get_task(conn, execution_id).status == "ready"
        assert kb.get_task(conn, review_id).status == "todo"
        correction = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='grace_correction_requested' ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
        assert correction is not None
        assert '"review_task_id":' in correction["payload"]


def test_stale_review_run_cannot_reopen_completed_execution(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        assert kb.complete_task(conn, execution_id, summary="verified")
        review = kb.claim_task(conn, review_id)
        assert review is not None

        assert not kb.block_task(
            conn,
            review_id,
            reason="stale reviewer correction",
            kind="dependency",
            expected_run_id=review.current_run_id + 1,
        )

        assert kb.get_task(conn, execution_id).status == "done"
        assert kb.get_task(conn, review_id).status == "running"
        assert conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? "
            "AND kind='grace_correction_requested'",
            (execution_id,),
        ).fetchone() is None


def test_repeated_identical_completion_artifacts_reuse_existing_rows(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    body = tmp_path / "package.md"
    image = tmp_path / "hero.png"
    body.write_text("same package", encoding="utf-8")
    image.write_bytes(b"same image bytes")
    with kb.connect_closing(db_path) as conn:
        execution_id, review_id = _grace_loop_pair(conn)
        metadata = {"artifacts": [str(body), str(image)]}
        assert kb.complete_task(
            conn, execution_id, summary="first", metadata=metadata,
        )
        first = kb.list_attachments(conn, execution_id)
        review = kb.claim_task(conn, review_id)
        assert review is not None
        assert kb.block_task(
            conn,
            review_id,
            reason="one correction",
            kind="dependency",
            expected_run_id=review.current_run_id,
        )
        assert kb.complete_task(
            conn, execution_id, summary="second", metadata=metadata,
        )
        second = kb.list_attachments(conn, execution_id)

    assert [(row.id, row.stored_path) for row in second] == [
        (row.id, row.stored_path) for row in first
    ]


def test_changed_completion_artifacts_preserve_each_prior_version(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    source = tmp_path / "package.md"
    versions = [b"first", b"second", b"third"]
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title="versioned artifact")
        stored_paths = []
        for content in versions:
            source.write_bytes(content)
            preserved, records = kb._persist_completion_artifacts(
                conn, task_id, [str(source)],
            )
            assert len(preserved) == len(records) == 1
            record = records[0]
            kb.add_attachment(
                conn,
                task_id,
                filename=record["filename"],
                stored_path=record["stored_path"],
                content_type=record["content_type"],
                size=record["size"],
                uploaded_by="test",
            )
            stored_paths.append(record["stored_path"])

    assert len(set(stored_paths)) == len(versions)
    assert [Path(path).read_bytes() for path in stored_paths] == versions


def test_completion_aborts_when_existing_artifact_cannot_be_preserved(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    source = tmp_path / "package.md"
    source.write_text("must survive", encoding="utf-8")
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title="artifact preservation")

        def fail_copy(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(kb.shutil, "copy2", fail_copy)
        with pytest.raises(RuntimeError, match="failed to preserve completion artifact"):
            kb.complete_task(
                conn,
                task_id,
                summary="must not complete",
                metadata={"artifacts": [str(source)]},
            )

        assert kb.get_task(conn, task_id).status != "done"
        assert source.read_text(encoding="utf-8") == "must survive"
        assert kb.list_attachments(conn, task_id) == []


def test_completion_rejects_oversized_artifact_without_loading_it(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")
    monkeypatch.setattr(kb, "_MAX_COMPLETION_ARTIFACT_BYTES", 4)
    with kb.connect_closing(db_path) as conn:
        task_id = kb.create_task(conn, title="bounded artifact")
        with pytest.raises(RuntimeError, match="failed to preserve completion artifact"):
            kb.complete_task(
                conn,
                task_id,
                metadata={"artifacts": [str(source)]},
            )
        assert kb.get_task(conn, task_id).status != "done"
        assert kb.list_attachments(conn, task_id) == []


def test_corrected_content_package_reads_only_latest_run_artifacts(tmp_path):
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    body_path = tmp_path / "package.md"
    image_path = tmp_path / "hero.png"
    delivery = {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_with_attachment",
        "body_field": "acceptance_evidence.inline_content_package",
        "asset_filenames": [image_path.name],
    }

    def metadata(body, color):
        body_path.write_text(body, encoding="utf-8")
        Image.new("RGB", (1200, 800), color).save(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        return {
            "artifacts": [str(body_path), str(image_path)],
            "acceptance_evidence": {"inline_content_package": body},
            "user_facing_report": {
                "kind": "content_package",
                "delivery": "inline_with_attachment",
                "complete": True,
                "title": "Package",
                "body": body,
                "observed_at": int(time.time()),
                "assets": [{
                    "filename": image_path.name,
                    "label": "hero",
                    "path": str(image_path),
                    "sha256": digest,
                }],
            },
        }

    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(
            conn,
            title="execution",
            body="GRACE_LOOP_CONTRACT_STAGE: execution\n```json\n"
            + json.dumps({"user_facing_delivery": delivery})
            + "\n```",
        )
        review_id = kb.create_task(
            conn,
            title="review",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review",
            parents=(execution_id,),
        )
        now = int(time.time())
        conn.execute(
            "INSERT INTO grace_delegations (delegation_id,contract_fingerprint,"
            "request_instance_id,platform,chat_id,thread_id,session_key,session_id,"
            "resolved_route,approval_required,state,execution_task_id,review_task_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,0,'queued',?,?,?,?)",
            (
                f"gd_{execution_id}", "a" * 64, f"gri_{execution_id}",
                "telegram", "chat-1", "2", "loop:correction", "session:correction",
                "{}", execution_id, review_id, now, now,
            ),
        )
        assert kb.complete_task(
            conn, execution_id, summary="first", metadata=metadata("first", "red"),
        )
        review = kb.claim_task(conn, review_id)
        assert review is not None
        assert kb.block_task(
            conn,
            review_id,
            reason="correct image",
            kind="dependency",
            expected_run_id=review.current_run_id,
        )
        second_metadata = metadata("second", "blue")
        expected_digest = second_metadata["user_facing_report"]["assets"][0]["sha256"]
        assert kb.complete_task(
            conn, execution_id, summary="second", metadata=second_metadata,
        )

        readback = kb.grace_content_package_attachment_readback(conn, execution_id)
        report = kb.grace_inline_content_package_report(conn, execution_id)

        current = next(
            row for row in kb.list_attachments(conn, execution_id)
            if row.filename == image_path.name
            and hashlib.sha256(Path(row.stored_path).read_bytes()).hexdigest()
            == expected_digest
        )
        duplicate_id = kb.add_attachment(
            conn,
            execution_id,
            filename=current.filename,
            stored_path=current.stored_path,
            content_type=current.content_type,
            size=current.size,
            uploaded_by="current-version-replay",
        )
        latest = kb.latest_run(conn, execution_id)
        latest_metadata = dict(latest.metadata)
        latest_metadata["attachment_manifest"] = kb.task_attachment_manifest(
            conn, execution_id,
        )
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(latest_metadata), latest.id),
        )
        replay_readback = kb.grace_content_package_attachment_readback(
            conn, execution_id,
        )

    assert readback is not None
    assert readback["assets"][0]["sha256"] == expected_digest
    assert len(readback["assets"][0]["superseded_attachment_ids"]) == 1
    assert report is not None and report["body"] == "second"
    assert duplicate_id in replay_readback["assets"][0]["duplicate_attachment_ids"]


def test_review_context_pins_parent_attempt(tmp_path):
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
        unavailable = kb.build_worker_context(conn, review_id)
        assert "Controller-pinned review source is unavailable" in unavailable
        assert kb.claim_task(conn, review_id) is not None
        context = kb.build_worker_context(conn, review_id)

    assert "Exact parent run for workflow review" in context
    assert "Facebook draft verified" not in context
    assert "Shopee product 50614873414 verified as unlisted" in context
    assert "external_effects" in context
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
