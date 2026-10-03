from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from proactive.backend_poll_worker import poll_due_backend_runs


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _queued_backend_run(
    executor_backend: str = "openclaw",
    *,
    max_runtime_seconds: int = 120,
) -> tuple[str, int, int]:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Poll async OpenClaw",
            executor_backend=executor_backend,
            max_runtime_seconds=max_runtime_seconds,
        )
        task = kb.claim_task(conn, task_id, claimer="router")
        assert task is not None and task.current_run_id is not None
        run_id = int(task.current_run_id)
        assert kb.record_backend_lifecycle(
            conn,
            task_id,
            expected_run_id=run_id,
            status="queued",
            backend_run_id=f"backend-{task_id}",
            backend_agent_id="readonly-agent",
            protocol_version="2.0",
            next_poll_seconds=0,
        )
        run = kb.get_run(conn, run_id)
        assert run is not None and run.backend_next_poll_at is not None
        return task_id, run_id, int(run.backend_next_poll_at)


def test_poll_worker_records_running_observation_and_releases_lease(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()
    with kb.connect() as conn:
        conn.execute(
            "UPDATE tasks SET claim_expires = 1, last_heartbeat_at = NULL WHERE id = ?",
            (task_id,),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET claim_expires = 1,
                   last_heartbeat_at = NULL
             WHERE id = ?
            """,
            (run_id,),
        )

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="poll-worker",
        now=due_at,
    )

    assert result.as_dict() == {
        "claimed": 1,
        "observed": 1,
        "terminal": 0,
        "retried": 0,
        "errors": [],
    }
    with kb.connect() as conn:
        run = kb.get_run(conn, run_id)
        assert run is not None
        assert run.task_id == task_id
        assert run.backend_status == "running"
        assert run.backend_poll_count == 1
        assert run.backend_poll_owner is None
        assert run.backend_next_poll_at is not None
        assert run.backend_next_poll_at > due_at
        task = kb.get_task(conn, task_id)
        assert task is not None and task.claim_expires is not None
        assert task.claim_expires > 1
        assert task.last_heartbeat_at is not None
        assert run.claim_expires == task.claim_expires
        assert run.last_heartbeat_at == task.last_heartbeat_at
        assert any(
            event.kind == "backend_heartbeat"
            for event in kb.list_events(conn, task_id)
        )


def test_unowned_lifecycle_write_cannot_clear_active_poll_lease(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()
    with kb.connect() as conn:
        claimed = kb.claim_due_backend_polls(
            conn,
            owner="active-poller",
            executor_backends=("openclaw",),
            now=due_at,
            lease_seconds=30,
        )
        assert [run.id for run in claimed] == [run_id]
        assert not kb.record_backend_lifecycle(
            conn,
            task_id,
            expected_run_id=run_id,
            status="running",
            backend_run_id=claimed[0].backend_run_id,
            backend_agent_id=claimed[0].backend_agent_id,
            protocol_version=claimed[0].protocol_version,
            next_poll_seconds=2,
        )
        still_owned = kb.get_run(conn, run_id)
        assert still_owned is not None
        assert still_owned.backend_status == "queued"
        assert still_owned.backend_poll_owner == "active-poller"
        assert still_owned.backend_poll_lease_until == due_at + 30


def test_poll_worker_retries_adapter_failure_without_losing_run(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()

    def fail(_run):
        raise TimeoutError("backend unavailable")

    result = poll_due_backend_runs(
        adapters={"openclaw": fail},
        owner="poll-worker",
        now=due_at,
    )

    assert result.claimed == 1
    assert result.retried == 1
    assert result.observed == 0
    assert result.errors == ("TimeoutError: backend unavailable",)
    with kb.connect() as conn:
        run = kb.get_run(conn, run_id)
        assert run is not None
        assert run.backend_status == "queued"
        assert run.backend_poll_count == 1
        assert run.backend_poll_owner is None
        assert run.backend_last_error == "TimeoutError: backend unavailable"
        circuit = conn.execute(
            """
            SELECT consecutive_failures, last_error
              FROM execution_backend_circuits
             WHERE backend_id = 'openclaw'
            """
        ).fetchone()
        assert circuit is not None
        assert circuit["consecutive_failures"] == 1
        assert circuit["last_error"] == "TimeoutError: backend unavailable"
        retry_events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "backend_poll_retry"
        ]
        assert len(retry_events) == 1
        assert retry_events[0].run_id == run_id
        assert retry_events[0].payload == {
            "executor_backend": "openclaw",
            "backend_status": "queued",
            "poll_count": 1,
            "retry_at": run.backend_next_poll_at,
            "error": "TimeoutError: backend unavailable",
        }


def test_poll_worker_retries_transient_terminal_backend_failure(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()

    def overloaded(run):
        return {
            "status": "blocked",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
            "result_digest": "empty-result-digest",
            "delegated_result": {
                "status": "blocked",
                "artifacts": [
                    {
                        "type": "openclaw_result",
                        "value": {
                            "evidence": {
                                "backendRunStatus": "error",
                                "backendError": (
                                    "FailoverError: The AI service is "
                                    "temporarily overloaded. Please try again "
                                    "in a moment."
                                ),
                                "resultContractError": (
                                    "Loop Contract result is empty or missing."
                                ),
                            },
                            "result": {},
                        },
                    }
                ],
            },
        }

    def terminal_handler(_run, _observation):
        pytest.fail("transient backend failures must not reach terminal review")

    result = poll_due_backend_runs(
        adapters={"openclaw": overloaded},
        terminal_handlers={"openclaw": terminal_handler},
        owner="poll-worker",
        now=due_at,
    )

    assert result.claimed == 1
    assert result.observed == 0
    assert result.terminal == 0
    assert result.retried == 1
    assert "Transient backend terminal error" in result.errors[0]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert run is not None
        assert run.backend_status == "queued"
        assert run.backend_poll_owner is None
        assert run.backend_last_error == result.errors[0]
        assert run.metadata["same_poll_error_count"] == 1
        assert (
            run.metadata["last_transient_terminal_observation"]["status"]
            == "blocked"
        )


def test_poll_worker_only_claims_runs_for_registered_adapters(kanban_home):
    openclaw_task_id, openclaw_run_id, due_at = _queued_backend_run("openclaw")
    codex_task_id, codex_run_id, codex_due_at = _queued_backend_run("codex")

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="openclaw-only-poller",
        now=max(due_at, codex_due_at),
    )

    assert result.claimed == 1
    with kb.connect() as conn:
        openclaw_run = kb.get_run(conn, openclaw_run_id)
        codex_run = kb.get_run(conn, codex_run_id)
        assert openclaw_run is not None and openclaw_run.backend_poll_count == 1
        assert codex_run is not None and codex_run.backend_poll_count == 0
        assert codex_run.backend_poll_owner is None
        assert kb.get_task(conn, openclaw_task_id) is not None
        assert kb.get_task(conn, codex_task_id) is not None


def test_poll_worker_uses_supplied_clock_for_every_claimed_run(kanban_home):
    first_task_id, first_run_id, first_due = _queued_backend_run()
    second_task_id, second_run_id, second_due = _queued_backend_run()
    supplied_now = max(first_due, second_due) + 100
    with kb.connect() as conn:
        conn.execute(
            """
            UPDATE task_runs
               SET backend_next_poll_at = ?
             WHERE id IN (?, ?)
            """,
            (supplied_now, first_run_id, second_run_id),
        )

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="synthetic-clock-poller",
        limit=2,
        now=supplied_now,
    )

    assert result.claimed == 2
    assert result.observed == 2
    with kb.connect() as conn:
        assert kb.get_task(conn, first_task_id).status == "running"
        assert kb.get_task(conn, second_task_id).status == "running"


def test_poll_worker_defers_adapter_while_backend_circuit_is_open(kanban_home):
    _task_id, run_id, due_at = _queued_backend_run("openclaw")
    with kb.connect() as conn:
        for offset in range(3):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error="bridge outage",
                cooldown_seconds=300,
                now=due_at + offset,
            )

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda _run: pytest.fail(
                "open circuit must not invoke its adapter"
            )
        },
        owner="open-circuit-poller",
        now=due_at + 2,
    )

    assert result.claimed == 1
    assert result.observed == 0
    assert result.retried == 1
    assert result.errors == ()
    with kb.connect() as conn:
        run = kb.get_run(conn, run_id)
        assert run is not None
        assert run.backend_last_error == "Backend circuit is open; poll deferred."


def test_poll_worker_derives_no_progress_limit_for_legacy_loop_contract_run(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Legacy ambiguous loop admission",
            executor_backend="openclaw",
            max_runtime_seconds=900,
        )
        task = kb.claim_task(conn, task_id, claimer="router")
        assert task is not None and task.current_run_id is not None
        run_id = int(task.current_run_id)
        assert kb.merge_active_run_metadata(
            conn,
            task_id,
            expected_run_id=run_id,
            metadata={
                "max_poll_iterations": 0,
                "loop_contract": {
                    "stop_rules": {
                        "no_progress": ["Same poll error twice"],
                    },
                },
            },
        )
        assert kb.record_backend_lifecycle(
            conn,
            task_id,
            expected_run_id=run_id,
            status="queued",
            protocol_version="2.0",
            next_poll_seconds=0,
        )
        run = kb.get_run(conn, run_id)
        assert run is not None and run.backend_next_poll_at is not None
        due_at = int(run.backend_next_poll_at)

    def unavailable(_run):
        raise TimeoutError("OpenClaw Loop Contract admission remains ambiguous.")

    first = poll_due_backend_runs(
        adapters={"openclaw": unavailable},
        owner="legacy-loop-poller-1",
        now=due_at,
    )
    assert first.retried == 1

    with kb.connect() as conn:
        run = kb.get_run(conn, run_id)
        assert run is not None
        assert run.metadata["same_poll_error_count"] == 1
        due_at = int(run.backend_next_poll_at)

    second = poll_due_backend_runs(
        adapters={"openclaw": unavailable},
        owner="legacy-loop-poller-2",
        now=due_at,
    )
    assert second.retried == 0

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "blocked"
        assert run is not None and run.status == "blocked"
        assert "no_progress rule reached" in run.summary


def test_poll_worker_uses_full_io_lease_for_task_claim_and_half_open_probe(
    kanban_home, monkeypatch
):
    _task_id, _run_id, due_at = _queued_backend_run(
        "openclaw",
        max_runtime_seconds=120,
    )
    with kb.connect() as conn:
        for _ in range(3):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error="bridge outage",
                cooldown_seconds=1,
                now=due_at,
            )

    clock_now = due_at + 2
    monkeypatch.setattr(kb.time, "time", lambda: clock_now)
    observed: dict[str, int] = {}
    original_renew_claim = kb.renew_external_backend_claim
    original_claim_probe = kb.claim_backend_circuit_probe

    def observe_claim_renewal(conn, task_id, **kwargs):
        observed["task_ttl"] = int(kwargs["ttl_seconds"])
        return original_renew_claim(conn, task_id, **kwargs)

    def observe_probe(conn, backend_id, **kwargs):
        observed["probe_lease"] = int(kwargs["lease_seconds"])
        return original_claim_probe(conn, backend_id, **kwargs)

    monkeypatch.setattr(
        kb,
        "renew_external_backend_claim",
        observe_claim_renewal,
    )
    monkeypatch.setattr(
        kb,
        "claim_backend_circuit_probe",
        observe_probe,
    )

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="half-open-poller",
        lease_seconds=30,
        now=clock_now,
    )

    assert result.observed == 1
    assert observed == {
        "task_ttl": 150,
        "probe_lease": 150,
    }


def test_stale_successful_poll_does_not_close_newly_opened_circuit(kanban_home):
    _task_id, _run_id, due_at = _queued_backend_run("openclaw")

    def concurrent_failure_then_running(run):
        with kb.connect() as conn:
            for offset in range(3):
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=False,
                    error="concurrent bridge outage",
                    now=due_at + offset,
                )
        return {
            "status": "running",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
        }

    result = poll_due_backend_runs(
        adapters={"openclaw": concurrent_failure_then_running},
        owner="stale-success-poller",
        now=due_at,
    )

    assert result.observed == 1
    assert result.errors == ()
    with kb.connect() as conn:
        assert kb.backend_circuit_states(
            conn,
            now=due_at + 2,
        )["openclaw"] == "open"
        snapshot = kb.backend_circuit_snapshot(conn, "openclaw")
        assert snapshot["consecutive_failures"] == 3
        assert snapshot["last_error"] == "concurrent bridge outage"


def test_stale_terminal_success_does_not_close_newly_opened_circuit(kanban_home):
    task_id, _run_id, due_at = _queued_backend_run("openclaw")

    def concurrent_failure_then_terminal(run):
        with kb.connect() as conn:
            for offset in range(3):
                kb.record_backend_circuit_outcome(
                    conn,
                    "openclaw",
                    succeeded=False,
                    error="concurrent terminal outage",
                    now=due_at + offset,
                )
        return {
            "status": "succeeded",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
            "result_digest": "terminal-digest",
        }

    def finish(run, observation):
        with kb.connect() as conn:
            outcome = kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=True,
                expected_generation=int(observation["circuit_generation"]),
            )
            assert outcome["applied"] is False
            assert kb.complete_task(
                conn,
                run.task_id,
                result="terminal accepted",
                expected_run_id=run.id,
            )
        return {"accepted": True}

    result = poll_due_backend_runs(
        adapters={"openclaw": concurrent_failure_then_terminal},
        terminal_handlers={"openclaw": finish},
        owner="stale-terminal-poller",
        now=due_at,
    )

    assert result.terminal == 1
    assert result.errors == ()
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done"
        assert kb.backend_circuit_states(
            conn,
            now=due_at + 2,
        )["openclaw"] == "open"


def test_stale_adapter_failure_does_not_override_newer_circuit_success(kanban_home):
    _task_id, _run_id, due_at = _queued_backend_run("openclaw")

    def concurrent_success_then_failure(_run):
        with kb.connect() as conn:
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=True,
                now=due_at,
            )
        raise TimeoutError("stale failure")

    result = poll_due_backend_runs(
        adapters={"openclaw": concurrent_success_then_failure},
        owner="stale-failure-poller",
        now=due_at,
    )

    assert result.retried == 1
    assert result.errors == ("TimeoutError: stale failure",)
    with kb.connect() as conn:
        snapshot = kb.backend_circuit_snapshot(conn, "openclaw")
        assert snapshot["state"] == "closed"
        assert snapshot["consecutive_failures"] == 0
        assert snapshot["generation"] == 1


def test_poll_worker_keeps_bound_run_until_cleanup_after_iteration_limit(
    kanban_home,
):
    task_id, run_id, due_at = _queued_backend_run()
    with kb.connect() as conn:
        assert kb.merge_active_run_metadata(
            conn,
            task_id,
            expected_run_id=run_id,
            metadata={"max_poll_iterations": 1},
        )

    first = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="iteration-limit-poller",
        now=due_at,
    )
    assert first.observed == 1
    with kb.connect() as conn:
        active = kb.get_run(conn, run_id)
        assert active is not None and active.backend_next_poll_at is not None
        next_due = int(active.backend_next_poll_at)

    second = poll_due_backend_runs(
        adapters={
            "openclaw": lambda _run: pytest.fail(
                "iteration limit must stop before another backend call"
            )
        },
        owner="iteration-limit-poller",
        now=next_due,
    )

    assert second.observed == 0
    assert second.retried == 1
    assert "max_iterations reached" in second.errors[0]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        pending = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert pending is not None
        assert pending.metadata["stop_rule_cleanup_pending"] is True


def test_poll_worker_keeps_bound_run_until_cleanup_after_runtime_deadline(
    kanban_home, monkeypatch
):
    task_id, run_id, due_at = _queued_backend_run(max_runtime_seconds=1)
    with kb.connect() as conn:
        run = kb.get_run(conn, run_id)
        assert run is not None
        expired_at = int(run.started_at) + 1
    monkeypatch.setattr(kb.time, "time", lambda: expired_at)

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda _run: pytest.fail(
                "runtime deadline must stop before backend I/O"
            )
        },
        owner="runtime-deadline-poller",
        now=due_at,
    )

    assert result.observed == 0
    assert result.retried == 1
    assert "max_runtime_seconds reached" in result.errors[0]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        pending = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert pending is not None
        assert pending.metadata["stop_rule_cleanup_pending"] is True


def test_poll_worker_keeps_bound_run_until_cleanup_after_no_progress(
    kanban_home,
):
    task_id, run_id, due_at = _queued_backend_run()
    with kb.connect() as conn:
        assert kb.merge_active_run_metadata(
            conn,
            task_id,
            expected_run_id=run_id,
            metadata={
                "max_poll_iterations": 5,
                "no_progress_error_limit": 2,
            },
        )

    def fail(_run):
        raise TimeoutError("same bridge timeout")

    first = poll_due_backend_runs(
        adapters={"openclaw": fail},
        owner="no-progress-poller",
        now=due_at,
    )
    assert first.retried == 1
    with kb.connect() as conn:
        active = kb.get_run(conn, run_id)
        assert active is not None and active.backend_next_poll_at is not None
        next_due = int(active.backend_next_poll_at)

    second = poll_due_backend_runs(
        adapters={"openclaw": fail},
        owner="no-progress-poller",
        now=next_due,
    )

    assert second.retried == 1
    assert second.errors == ("TimeoutError: same bridge timeout",)
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        final = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert final is not None and final.outcome is None
        assert final.metadata["stop_rule_cleanup_pending"] is True


def test_poll_worker_escalates_after_bounded_cleanup_attempts(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()
    with kb.connect() as conn:
        assert kb.merge_active_run_metadata(
            conn,
            task_id,
            expected_run_id=run_id,
            metadata={
                "stop_rule_cleanup_pending": True,
                "stop_rule_reason": "max_runtime_seconds reached",
                "cleanup_attempt_count": 3,
                "cleanup_attempt_limit": 3,
                "cleanup_deadline_at": due_at + 300,
            },
        )

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda _run: pytest.fail(
                "exhausted cleanup must not call the backend again"
            )
        },
        owner="cleanup-exhaustion-poller",
        now=due_at,
    )

    assert result.observed == 0
    assert "cleanup exhausted" in result.errors[0]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "blocked"
        assert run is not None
        assert run.backend_run_id is not None
        assert run.metadata["cleanup_attempt_count"] == 3


def test_successful_poll_resets_consecutive_error_count(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()
    with kb.connect() as conn:
        assert kb.merge_active_run_metadata(
            conn,
            task_id,
            expected_run_id=run_id,
            metadata={
                "max_poll_iterations": 5,
                "no_progress_error_limit": 2,
            },
        )

    def fail(_run):
        raise TimeoutError("intermittent bridge timeout")

    first = poll_due_backend_runs(
        adapters={"openclaw": fail},
        owner="reset-no-progress-poller",
        now=due_at,
    )
    assert first.retried == 1
    with kb.connect() as conn:
        first_retry = kb.get_run(conn, run_id)
        assert first_retry is not None
        second_due = int(first_retry.backend_next_poll_at)

    second = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="reset-no-progress-poller",
        now=second_due,
    )
    assert second.observed == 1
    with kb.connect() as conn:
        progressed = kb.get_run(conn, run_id)
        assert progressed is not None
        assert progressed.metadata["same_poll_error_count"] == 0
        third_due = int(progressed.backend_next_poll_at)

    third = poll_due_backend_runs(
        adapters={"openclaw": fail},
        owner="reset-no-progress-poller",
        now=third_due,
    )

    assert third.retried == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        final = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert final is not None
        assert final.metadata["same_poll_error_count"] == 1


def test_transient_terminal_error_identity_uses_the_matched_backend_message():
    from proactive.backend_poll_worker import _transient_terminal_error

    first = _transient_terminal_error({
        "status": "blocked",
        "error": "invalid_terminal_result",
        "message": "Temporarily overloaded on backend alpha",
    })
    second = _transient_terminal_error({
        "status": "blocked",
        "error": "invalid_terminal_result",
        "message": "Temporarily overloaded on backend beta",
    })

    assert first == "Temporarily overloaded on backend alpha"
    assert second == "Temporarily overloaded on backend beta"
    assert first != second


def test_successful_poll_bookkeeping_uses_one_outer_transaction(
    kanban_home, monkeypatch
):
    _task_id, _run_id, due_at = _queued_backend_run()
    transaction_states = []
    original_lifecycle = kb.record_backend_lifecycle
    original_merge = kb.merge_active_run_metadata
    original_circuit = kb.record_backend_circuit_outcome

    def observe_lifecycle(conn, *args, **kwargs):
        transaction_states.append(("lifecycle", conn.in_transaction))
        return original_lifecycle(conn, *args, **kwargs)

    def observe_merge(conn, *args, **kwargs):
        transaction_states.append(("merge", conn.in_transaction))
        return original_merge(conn, *args, **kwargs)

    def observe_circuit(conn, *args, **kwargs):
        transaction_states.append(("circuit", conn.in_transaction))
        return original_circuit(conn, *args, **kwargs)

    monkeypatch.setattr(kb, "record_backend_lifecycle", observe_lifecycle)
    monkeypatch.setattr(kb, "merge_active_run_metadata", observe_merge)
    monkeypatch.setattr(kb, "record_backend_circuit_outcome", observe_circuit)

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="atomic-bookkeeping-poller",
        now=due_at,
    )

    assert result.observed == 1
    assert transaction_states == [
        ("lifecycle", True),
        ("merge", True),
        ("circuit", True),
    ]


def test_poll_worker_extends_lease_across_slow_adapter_io(
    kanban_home, monkeypatch
):
    _task_id, run_id, due_at = _queued_backend_run(max_runtime_seconds=120)
    clock = {"now": due_at}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])

    def slow(run):
        clock["now"] += 60
        return {
            "status": "running",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
        }

    result = poll_due_backend_runs(
        adapters={"openclaw": slow},
        owner="slow-io-poller",
        lease_seconds=30,
        now=due_at,
    )

    assert result.observed == 1
    assert result.errors == ()
    with kb.connect() as conn:
        run = kb.get_run(conn, run_id)
        assert run is not None and run.backend_status == "running"


def test_malformed_poll_control_blocks_only_its_run(kanban_home):
    bad_task_id, bad_run_id, bad_due = _queued_backend_run()
    good_task_id, good_run_id, good_due = _queued_backend_run()
    with kb.connect() as conn:
        assert kb.merge_active_run_metadata(
            conn,
            bad_task_id,
            expected_run_id=bad_run_id,
            metadata={"max_poll_iterations": "unknown"},
        )

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "running",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
            }
        },
        owner="metadata-validation-poller",
        now=max(bad_due, good_due),
    )

    assert result.claimed == 2
    assert result.observed == 1
    assert len(result.errors) == 1
    assert "max_poll_iterations" in result.errors[0]
    with kb.connect() as conn:
        bad_task = kb.get_task(conn, bad_task_id)
        good_task = kb.get_task(conn, good_task_id)
        good_run = kb.get_run(conn, good_run_id)
        assert bad_task is not None and bad_task.status == "blocked"
        assert good_task is not None and good_task.status == "running"
        assert good_run is not None and good_run.backend_status == "running"


def test_terminal_handler_receives_a_fresh_poll_lease(
    kanban_home, monkeypatch
):
    task_id, run_id, due_at = _queued_backend_run(max_runtime_seconds=120)
    clock = {"now": due_at}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])

    def terminal(run):
        clock["now"] += 60
        return {
            "status": "succeeded",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
            "result_digest": "terminal-digest",
        }

    def handle(run, _observation):
        clock["now"] += 100
        with kb.connect() as conn:
            assert (
                kb.claim_due_backend_polls(
                    conn,
                    owner="competing-poller",
                    executor_backends=("openclaw",),
                    now=clock["now"],
                )
                == []
            )
            assert kb.complete_task(
                conn,
                run.task_id,
                result="accepted",
                expected_run_id=run.id,
            )
        return {"accepted": True}

    result = poll_due_backend_runs(
        adapters={"openclaw": terminal},
        terminal_handlers={"openclaw": handle},
        owner="terminal-lease-poller",
        lease_seconds=30,
        now=due_at,
    )

    assert result.terminal == 1
    assert result.errors == ()
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "done"
        assert kb.get_run(conn, run_id).outcome == "completed"


def test_stale_adapter_failure_cannot_mutate_reclaimed_run(
    kanban_home, monkeypatch
):
    task_id, run_id, due_at = _queued_backend_run()
    clock = {"now": due_at}
    monkeypatch.setattr(kb.time, "time", lambda: clock["now"])

    def lose_lease_then_fail(_run):
        clock["now"] = due_at + 121
        with kb.connect() as conn:
            conn.execute(
                """
                UPDATE task_runs
                   SET backend_poll_owner = ?,
                       backend_poll_lease_until = ?
                 WHERE id = ?
                """,
                ("competing-poller", clock["now"] + 30, run_id),
            )
        raise RuntimeError("stale adapter failure")

    result = poll_due_backend_runs(
        adapters={"openclaw": lose_lease_then_fail},
        owner="stale-poller",
        lease_seconds=30,
        now=due_at,
    )

    assert result.claimed == 1
    assert result.errors == ("RuntimeError: stale adapter failure",)
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "running"
        assert run is not None
        assert run.backend_poll_owner == "competing-poller"
        assert (run.metadata or {}).get("last_poll_error") is None


def test_poll_worker_requires_terminal_evidence_handler(kanban_home):
    task_id, _run_id, due_at = _queued_backend_run()

    result = poll_due_backend_runs(
        adapters={
            "openclaw": lambda run: {
                "status": "succeeded",
                "backend_run_id": run.backend_run_id,
                "backend_agent_id": run.backend_agent_id,
                "protocol_version": run.protocol_version,
                "result_digest": "digest",
            }
        },
        owner="poll-worker",
        now=due_at,
    )

    assert result.terminal == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"


def test_poll_worker_retries_terminal_handler_locally_before_blocking(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()
    adapter_calls = 0

    def terminal_observation(run):
        nonlocal adapter_calls
        adapter_calls += 1
        return {
            "status": "succeeded",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
            "result_digest": "digest",
        }

    def fail_terminal(_run, _observation):
        raise RuntimeError("review database unavailable")

    results = [
        poll_due_backend_runs(
            adapters={"openclaw": terminal_observation},
            terminal_handlers={"openclaw": fail_terminal},
            owner=f"poll-worker-{attempt}",
            lease_seconds=1,
            now=due_at + attempt + 1,
        )
        for attempt in range(3)
    ]

    assert [result.retried for result in results] == [1, 1, 0]
    assert all(
        result.errors == ("RuntimeError: review database unavailable",)
        for result in results
    )
    assert adapter_calls == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "blocked"
        assert run is not None and run.backend_status == "succeeded"
        assert run.outcome == "blocked"
        assert run.metadata["terminal_handler_error_count"] == 3


def test_terminal_handler_retry_limit_counts_changing_errors(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()
    handler_calls = 0

    def terminal_observation(run):
        return {
            "status": "succeeded",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
            "result_digest": "digest",
        }

    def fail_terminal(_run, _observation):
        nonlocal handler_calls
        handler_calls += 1
        raise RuntimeError(f"changing failure {handler_calls}")

    results = [
        poll_due_backend_runs(
            adapters={"openclaw": terminal_observation},
            terminal_handlers={"openclaw": fail_terminal},
            owner=f"poll-worker-changing-{attempt}",
            lease_seconds=1,
            now=due_at + attempt + 1,
        )
        for attempt in range(3)
    ]

    assert [result.retried for result in results] == [1, 1, 0]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "blocked"
        assert run is not None
        assert run.metadata["terminal_handler_error_count"] == 3
        assert run.metadata["terminal_handler_error"].endswith("changing failure 3")


def test_terminal_observation_remains_claimable_after_process_exit(kanban_home):
    task_id, run_id, due_at = _queued_backend_run()

    def observation(run):
        return {
            "status": "succeeded",
            "backend_run_id": run.backend_run_id,
            "backend_agent_id": run.backend_agent_id,
            "protocol_version": run.protocol_version,
            "result_digest": "terminal-digest",
        }

    def process_exit(_run, _observation):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        poll_due_backend_runs(
            adapters={"openclaw": observation},
            terminal_handlers={"openclaw": process_exit},
            owner="poll-worker-before-exit",
            lease_seconds=30,
            now=due_at,
        )

    with kb.connect() as conn:
        pending = kb.get_run(conn, run_id)
        task = kb.get_task(conn, task_id)
        assert pending is not None
        assert task is not None and task.status == "running"
        assert pending.backend_status == "succeeded"
        assert pending.backend_poll_owner == "poll-worker-before-exit"
        assert pending.backend_poll_lease_until is not None
        assert pending.backend_next_poll_at <= pending.backend_poll_lease_until
        assert pending.metadata is not None
        persisted_observation = pending.metadata[
            "backend_terminal_observation"
        ]
        assert persisted_observation["status"] == "succeeded"
        assert persisted_observation["result_digest"] == "terminal-digest"
        assert isinstance(
            persisted_observation["circuit_generation"],
            int,
        )
        recovery_at = int(pending.backend_poll_lease_until)
        assert not kb.record_backend_lifecycle(
            conn,
            task_id,
            expected_run_id=run_id,
            status="succeeded",
            backend_run_id=pending.backend_run_id,
            backend_agent_id=pending.backend_agent_id,
            protocol_version=pending.protocol_version,
            result_digest=pending.result_digest,
        )
        still_owned = kb.get_run(conn, run_id)
        assert still_owned is not None
        assert still_owned.backend_poll_owner == "poll-worker-before-exit"
        assert still_owned.backend_poll_lease_until == recovery_at

    def finish(run, _observation):
        with kb.connect() as conn:
            assert kb.complete_task(
                conn,
                run.task_id,
                result="accepted after terminal replay",
                expected_run_id=run.id,
            )
        return {"accepted": True}

    recovered = poll_due_backend_runs(
        adapters={
            "openclaw": lambda _run: pytest.fail(
                "recovery must use durable terminal evidence"
            )
        },
        terminal_handlers={"openclaw": finish},
        owner="poll-worker-after-restart",
        lease_seconds=30,
        now=recovery_at,
    )

    assert recovered.terminal == 1
    assert recovered.errors == ()
    with kb.connect() as conn:
        final = kb.get_run(conn, run_id)
        task = kb.get_task(conn, task_id)
        assert final is not None and final.outcome == "completed"
        assert task is not None and task.status == "done"
