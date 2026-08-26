from __future__ import annotations

import json
import sqlite3

import pytest

from gateway.telegram_interaction_labels import (
    decorate_telegram_message,
    interaction_metadata,
    interaction_metadata_from_message_path,
)
from hermes_cli import kanban_db as kb
from hermes_cli.telegram_message_path import (
    actor,
    append_hop,
    backend_projection,
    begin_backend_attempt,
    bind_message_path,
    build_telegram_message_path,
    merge_message_paths,
    normalize_message_path,
    record_outbound_delivery,
)


@pytest.fixture(autouse=True)
def _isolated_trace_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


def _path():
    return build_telegram_message_path(
        chat_id="123456",
        thread_id="42",
        chat_type="private",
        user_id="kj-user",
        inbound_message_id="9001",
        reply_to_message_id="8999",
        session_key="agent:main:telegram:private:123456:42",
        session_id="session-1",
        observed_at="2026-08-24T00:00:00+00:00",
    )


def test_gateway_path_is_deterministic_and_backend_projection_is_not_routable():
    first = _path()
    second = _path()
    assert first["trace_id"] == second["trace_id"]
    assert first["user_id_sha256"] != "kj-user"

    projected = backend_projection(first)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert projected["trace_id"] == first["trace_id"]
    assert "123456" not in encoded
    assert "9001" not in encoded
    assert "agent:main:telegram" not in encoded
    assert projected["privacy"]["raw_user_message"] == "not_disclosed"


def test_default_profile_is_rendered_as_grace_review_role():
    legacy = interaction_metadata("review", ["default", "Grace", "你"])
    rendered = decorate_telegram_message("完成", legacy)
    assert "default" not in rendered
    assert "Grace 驗收" in rendered

    traced = interaction_metadata_from_message_path(
        _path(), "review", actor_id="default"
    )
    rendered = decorate_telegram_message("完成", traced)
    assert rendered.startswith("🟣 Agent 驗收｜Grace 驗收 → 你")


def test_repeatable_backend_ids_advance_without_stale_delivery_regression():
    first = bind_message_path(
        _path(),
        run_id="run-1",
        openclaw_backend_agent_id="missioncrew-research",
        openclaw_backend_run_id="backend-1",
        openclaw_backend_session_key="session-1",
    )
    second = begin_backend_attempt(
        first,
        run_id="run-2",
        backend_agent_id="missioncrew-research",
    )
    second = bind_message_path(
        second,
        openclaw_backend_run_id="backend-2",
        openclaw_backend_session_key="session-2",
    )

    advanced = merge_message_paths(first, second)
    assert advanced["run_id"] == "run-2"
    assert advanced["openclaw_backend_run_id"] == "backend-2"
    stale_receipt = record_outbound_delivery(first, ["late-outbound"])
    merged = merge_message_paths(advanced, stale_receipt)
    assert merged["run_id"] == "run-2"
    assert merged["openclaw_backend_run_id"] == "backend-2"
    assert "late-outbound" in merged["outbound_message_ids"]


def test_hops_are_immutable_and_backend_agent_failover_is_historicized():
    original = append_hop(
        _path(),
        stage="openclaw_admission_attempt",
        from_actor=actor("clawops", "clawops"),
        to_actor=actor("backend-a", "openclaw_backend"),
        status="attempted",
        identifiers={"run_id": "run-1"},
        observed_at="2026-08-24T00:01:00+00:00",
    )
    stale = append_hop(
        original,
        stage="openclaw_admission_attempt",
        from_actor=actor("clawops", "clawops"),
        to_actor=actor("backend-a", "openclaw_backend"),
        status="observed",
        identifiers={"run_id": "run-1"},
        observed_at="2026-08-24T00:02:00+00:00",
    )
    admission_hop = next(
        hop for hop in stale["hops"] if hop["stage"] == "openclaw_admission_attempt"
    )
    assert admission_hop["status"] == "attempted"
    assert admission_hop["observed_at"] == "2026-08-24T00:01:00+00:00"

    first = bind_message_path(
        original,
        run_id="run-1",
        openclaw_backend_agent_id="backend-a",
    )
    failed_over = begin_backend_attempt(
        first,
        run_id="run-2",
        backend_agent_id="backend-b",
    )
    assert failed_over["openclaw_backend_agent_id"] == "backend-b"
    assert failed_over["openclaw_backend_agent_ids"] == ["backend-a"]


def test_delegation_persists_and_binds_the_canonical_path(tmp_path):
    db_path = tmp_path / "kanban.db"
    with kb.connect_closing(db_path) as conn:
        execution_id = kb.create_task(conn, title="execution", initial_status="blocked")
        review_id = kb.create_task(conn, title="review")
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint="a" * 64,
            request_instance_id="message-9001",
            platform="telegram",
            chat_id="123456",
            thread_id="42",
            session_key="agent:main:telegram:private:123456:42",
            session_id="session-1",
            resolved_route={"assignment": {"agent": "openclaw"}},
            approval_required=False,
            telegram_message_path=_path(),
        )
        stored = normalize_message_path(delegation["telegram_message_path"])
        assert stored["delegation_id"] == delegation["delegation_id"]
        assert kb.claim_grace_delegation_build(
            conn,
            delegation_id=delegation["delegation_id"],
            build_owner="builder",
        )
        queued = kb.mark_grace_delegation_queued(
            conn,
            delegation_id=delegation["delegation_id"],
            build_owner="builder",
            execution_task_id=execution_id,
            review_task_id=review_id,
        )
        queued_path = normalize_message_path(queued["telegram_message_path"])
        assert queued_path["execution_task_id"] == execution_id
        assert queued_path["review_task_id"] == review_id
        assert queued_path["hops"][-1]["stage"] == "grace_delegation"
        assert kb.telegram_message_path_for_task(conn, review_id)["trace_id"] == stored[
            "trace_id"
        ]
        first_chunk = record_outbound_delivery(
            queued_path, ["out-1"], interaction_kind="execution"
        )
        second_chunk = record_outbound_delivery(
            queued_path, ["out-2"], interaction_kind="execution"
        )
        kb.update_grace_delegation_telegram_message_path(
            conn,
            delegation_id=delegation["delegation_id"],
            telegram_message_path=first_chunk,
        )
        merged = kb.update_grace_delegation_telegram_message_path(
            conn,
            delegation_id=delegation["delegation_id"],
            telegram_message_path=second_chunk,
        )
        assert merged["outbound_message_ids"] == ["out-1", "out-2"]


def test_existing_delegation_table_gets_additive_message_path_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    kb.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE grace_delegations DROP COLUMN telegram_message_path")
    conn.commit()
    conn.close()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    kb.init_db(db_path)

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(grace_delegations)")}
    conn.close()
    assert "telegram_message_path" in columns


def test_pending_approval_trace_persists_prompt_receipt_before_delegation(tmp_path):
    db_path = tmp_path / "approval.db"
    path = _path()
    with kb.connect_closing(db_path) as conn:
        challenge = kb.create_grace_approval_challenge(
            conn,
            contract_fingerprint="c" * 64,
            request_instance_id="request-1",
            platform="telegram",
            chat_id="123456",
            thread_id="42",
            session_key=path["session_key"],
            session_id=path["session_id"],
            user_id_sha256="u" * 64,
            requested_message_id=path["inbound_message_id"],
            action_summary="Publish one listing",
            approval_platform="facebook",
            approval_scope='{"listing_id":"1"}',
            telegram_message_path=path,
        )
        receipt = record_outbound_delivery(
            path, ["approval-prompt-1"], interaction_kind="direct"
        )
        merged = kb.update_grace_approval_challenge_telegram_message_path(
            conn,
            telegram_message_path=receipt,
        )
        stored = kb.get_grace_approval_challenge(conn, challenge["token"])

    assert merged["outbound_message_ids"] == ["approval-prompt-1"]
    assert normalize_message_path(stored["telegram_message_path"])[
        "outbound_message_ids"
    ] == ["approval-prompt-1"]
