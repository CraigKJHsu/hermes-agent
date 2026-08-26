from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from proactive import openclaw_executor
from proactive.backend_poll_worker import poll_due_openclaw_runs
from proactive.openclaw_executor import execute_readonly_browser_snapshot


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _contract():
    return {
        "identity": {
            "project": "hub_ops",
            "topic_name": "openclaw-pilot",
            "thread_id": "readonly-browser",
            "request_instance_id": "openclaw-pilot-1",
        },
        "original_request": "執行第一個 OpenClaw 唯讀真實任務。",
        "grace_interpretation": "只讀取 Example Domain 並驗證零外部副作用。",
        "trigger": "KJ 要求開始第一個實作包。",
        "completion_mode": "terminal",
        "goal": {
            "objective": "Read Example Domain and return browser snapshot evidence.",
            "deliverables": ["Structured browser snapshot evidence"],
            "non_goals": ["No click, type, submit, upload, send, or write"],
        },
        "scope": {
            "allowed": ["https://example.com/"],
            "forbidden": ["Any external state change"],
        },
        "verification": {
            "checks": ["Backend identity", "URL equality", "Zero-effect evidence"],
            "evidence_required": ["Backend run id", "Browser snapshot result"],
            "acceptance_criteria": ["sideEffectsPerformed=false"],
        },
        "stop_rules": {
            "success": ["All verification checks pass"],
            "blocked": ["Browser capability unavailable"],
            "no_progress": ["Same execution error twice"],
            "max_iterations": 1,
            "max_runtime_seconds": 120,
        },
        "memory": {
            "namespace": "hub_ops/openclaw-pilot",
            "working": ["Current backend attempt"],
            "promote_on_acceptance": ["Verified executor capability"],
        },
    }


def _successful_result(task, *, requires_human_review=False, result_text=None):
    snapshot_excerpt = "Example Domain"
    return {
        "task_id": task["task_id"],
        "status": "succeeded",
        "summary": "OpenClaw completed a real read-only browser snapshot.",
        "artifacts": [
            {
                "type": "openclaw_result",
                "value": {
                    "evidence": {
                        "requestedUrl": "https://example.com/",
                        "browserSnapshotChars": len(snapshot_excerpt),
                        "externalEffectBudget": 0,
                        "sideEffectsPerformed": False,
                    },
                    "resultText": (
                        result_text
                        if result_text is not None
                        else json.dumps(
                            {
                                "url": "https://example.com/",
                                "title": "Example Domain",
                                "snapshotExcerpt": snapshot_excerpt,
                                "sideEffectsPerformed": False,
                            }
                        )
                    ),
                },
            }
        ],
        "tool_calls": [{"name": "openclaw_bridge_http"}],
        "audit_log": ["accepted", "executed"],
        "errors": [],
        "requires_human_review": requires_human_review,
        "recommended_next_action": "Review result.",
        "protocol_version": "2.0",
        "protocol_correlated": True,
        "delegation_id": task["delegation_id"],
        "attempt_id": task["attempt_id"],
        "contract_fingerprint": task["contract_fingerprint"],
        "identity_correlated": True,
        "backend_run_id": "openclaw-run-1",
        "backend_agent_id": "missioncrew-browser-readonly",
        "backend_session_key": "agent:missioncrew-browser-readonly:subagent:test",
    }


def _pending_browser_admission_result(task):
    return {
        "task_id": task["task_id"],
        "status": "running",
        "summary": "OpenClaw browser admission remains pending.",
        "artifacts": [
            {
                "type": "openclaw_result",
                "value": {
                    "evidence": {
                        "externalEffectBudget": 0,
                        "sideEffectsPerformed": False,
                        "terminal": False,
                        "admissionPending": True,
                    }
                },
            }
        ],
        "tool_calls": [{"name": "openclaw_bridge_http"}],
        "audit_log": ["accepted"],
        "errors": [],
        "requires_human_review": False,
        "recommended_next_action": "Replay the same start key.",
        "protocol_version": "2.0",
        "protocol_correlated": True,
        "delegation_id": task["delegation_id"],
        "attempt_id": task["attempt_id"],
        "contract_fingerprint": task["contract_fingerprint"],
        "identity_correlated": True,
    }


def test_readonly_openclaw_executor_closes_execution_and_review_tasks(kanban_home):
    def transport(task):
        assert task["protocol_version"] == "2.0"
        assert task["allowed_tools"] == ["browser.read"]
        assert task["external_effect_budget"] == 0
        assert task["dry_run"] is False
        return _successful_result(task)

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=transport,
    )

    assert result["status"] == "succeeded"
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
        assert execution is not None and execution.status == "done"
        assert review is not None and review.status == "done"
        assert run is not None
        assert run.executor_backend == "openclaw"
        assert run.backend_run_id == "openclaw-run-1"
        assert run.backend_agent_id == "missioncrew-browser-readonly"
        assert run.protocol_version == "2.0"
        assert run.result_digest == result["result_digest"]
        assert run.metadata["snapshot_validated"] is True
        assert run.metadata["snapshot_chars"] == len("Example Domain")


def test_readonly_openclaw_executor_persists_backend_token_usage(kanban_home):
    def transport(task):
        result = _successful_result(task)
        result["token_usage"] = {
            "input_tokens": 11,
            "output_tokens": 5,
            "cache_read_tokens": 13,
            "reasoning_tokens": 2,
        }
        return result

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=transport,
    )

    assert result["status"] == "succeeded"
    with kb.connect() as conn:
        run = kb.latest_run(conn, result["execution_task_id"])
        assert run is not None
        assert run.metadata["backend_token_usage"] == {
            "input_tokens": 11,
            "output_tokens": 5,
            "cache_read_tokens": 13,
            "reasoning_tokens": 2,
            "total_tokens": 31,
        }


def test_readonly_execution_and_review_finalize_in_one_transaction(
    kanban_home, monkeypatch
):
    original_complete = kb.complete_task
    transaction_states = []

    def observe_complete(conn, *args, **kwargs):
        transaction_states.append(conn.in_transaction)
        return original_complete(conn, *args, **kwargs)

    monkeypatch.setattr(kb, "complete_task", observe_complete)

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )

    assert result["status"] == "succeeded"
    assert transaction_states == [True, True]


def test_concurrent_idempotent_completion_returns_durable_success(
    kanban_home, monkeypatch
):
    original_record_lifecycle = kb.record_backend_lifecycle

    def concurrent_winner(conn, task_id, **kwargs):
        assert original_record_lifecycle(conn, task_id, **kwargs)
        run = kb.get_run(conn, int(kwargs["expected_run_id"]))
        assert run is not None
        metadata = run.metadata or {}
        terminal = kwargs["terminal_observation"]
        accepted, errors, _digest = (
            openclaw_executor._finalize_readonly_terminal(
                conn,
                execution_task_id=task_id,
                review_task_id=str(metadata["review_task_id"]),
                execution_run=run,
                delegated_result=terminal["delegated_result"],
                expected_url="https://example.com/",
                expected_delegation_id=str(metadata["delegation_id"]),
                expected_attempt_id=str(metadata["attempt_id"]),
                expected_contract_fingerprint=str(
                    metadata["contract_fingerprint"]
                ),
                expected_circuit_generation=int(
                    metadata["circuit_generation"]
                ),
            )
        )
        assert accepted and errors == []
        return False

    monkeypatch.setattr(
        kb,
        "record_backend_lifecycle",
        concurrent_winner,
    )

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )

    assert result["status"] == "succeeded"
    assert result["deduplicated"] is True
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        assert execution is not None and execution.status == "done"
        assert review is not None and review.status == "done"


def test_concurrent_mutable_wrapper_drift_reuses_terminal_evidence(
    kanban_home, monkeypatch
):
    original_record_lifecycle = kb.record_backend_lifecycle

    def persist_drifting_wrapper(conn, task_id, **kwargs):
        terminal = dict(kwargs["terminal_observation"])
        delegated_result = dict(terminal["delegated_result"])
        delegated_result.update(
            {
                "summary": "A concurrent caller used different wording.",
                "recommended_next_action": "Different mutable recommendation.",
                "audit_log": ["different", "mutable", "audit"],
            }
        )
        terminal["delegated_result"] = delegated_result
        persisted_kwargs = {
            **kwargs,
            "result_digest": (
                openclaw_executor._terminal_evidence_digest(
                    delegated_result
                )
            ),
            "terminal_observation": terminal,
        }
        assert original_record_lifecycle(
            conn,
            task_id,
            **persisted_kwargs,
        )
        return False

    monkeypatch.setattr(
        kb,
        "record_backend_lifecycle",
        persist_drifting_wrapper,
    )

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )

    assert result["status"] == "succeeded"
    assert result["deduplicated"] is True
    assert result["delegated_result"]["summary"] == (
        "A concurrent caller used different wording."
    )
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        assert execution is not None and execution.status == "done"
        assert review is not None and review.status == "done"


def test_readonly_reservation_rolls_back_when_review_creation_fails(
    kanban_home, monkeypatch
):
    original_create_task = kb.create_task
    calls = 0

    def fail_review_creation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("review creation failed")
        return original_create_task(*args, **kwargs)

    monkeypatch.setattr(kb, "create_task", fail_review_creation)

    with pytest.raises(RuntimeError, match="review creation failed"):
        execute_readonly_browser_snapshot(
            "https://example.com/",
            contract=_contract(),
        )

    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_readonly_delegation_replays_same_run_after_process_exit(kanban_home):
    with pytest.raises(KeyboardInterrupt):
        execute_readonly_browser_snapshot(
            "https://example.com/",
            contract=_contract(),
            transport=lambda _task: (_ for _ in ()).throw(KeyboardInterrupt),
        )

    with kb.connect() as conn:
        task = conn.execute(
            "SELECT id FROM tasks WHERE title LIKE 'ClawOps OpenClaw read-only%'"
        ).fetchone()
        assert task is not None
        interrupted = kb.latest_run(conn, str(task["id"]))
        assert interrupted is not None
        interrupted_run_id = interrupted.id
        now = int(kb.time.time())
        for offset in range(3):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error="new concurrent outage",
                now=now + offset,
            )

    replayed = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )

    assert replayed["status"] == "succeeded"
    assert replayed["deduplicated"] is True
    with kb.connect() as conn:
        final = kb.latest_run(conn, replayed["execution_task_id"])
        assert final is not None and final.id == interrupted_run_id
        assert kb.backend_circuit_states(
            conn,
            now=now + 2,
        )["openclaw"] == "open"


def test_readonly_replays_ambiguous_timeout_with_same_key(kanban_home):
    first = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda _task: (_ for _ in ()).throw(
            TimeoutError("response lost")
        ),
    )

    assert first["status"] == "retrying"
    with kb.connect() as conn:
        first_run = kb.latest_run(conn, first["execution_task_id"])
        assert first_run is not None
        assert first_run.backend_status is None
        replay_key = first_run.metadata["idempotency_key"]

    def replay(task):
        assert task["idempotency_key"] == replay_key
        return _successful_result(task)

    second = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=replay,
    )

    assert second["status"] == "succeeded"
    assert second["deduplicated"] is True
    assert second["execution_task_id"] == first["execution_task_id"]


def test_readonly_terminal_result_resumes_after_finalize_process_exit(
    kanban_home, monkeypatch
):
    original_finalize = openclaw_executor._finalize_readonly_terminal
    calls = 0

    def exit_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(
        openclaw_executor,
        "_finalize_readonly_terminal",
        exit_once,
    )
    with pytest.raises(KeyboardInterrupt):
        execute_readonly_browser_snapshot(
            "https://example.com/",
            contract=_contract(),
            transport=_successful_result,
        )

    with kb.connect() as conn:
        task = conn.execute(
            "SELECT id FROM tasks WHERE title LIKE 'ClawOps OpenClaw read-only%'"
        ).fetchone()
        assert task is not None
        pending = kb.latest_run(conn, str(task["id"]))
        assert pending is not None
        assert pending.backend_status == "succeeded"
        assert pending.metadata is not None
        assert (
            pending.metadata["backend_terminal_observation"][
                "delegated_result"
            ]["backend_run_id"]
            == "openclaw-run-1"
        )

    replayed = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda _task: pytest.fail(
            "terminal retry must use durable evidence"
        ),
    )

    assert replayed["status"] == "succeeded"
    assert replayed["deduplicated"] is True
    assert calls == 2


def test_readonly_blocked_retry_returns_durable_state_without_redelegating(
    kanban_home
):
    first = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda _task: (_ for _ in ()).throw(
            RuntimeError("bridge unavailable")
        ),
    )
    assert first["status"] == "blocked"

    second = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda _task: pytest.fail("blocked retry must not redelegate"),
    )

    assert second["status"] == "blocked"
    assert second["deduplicated"] is True
    assert second["execution_task_id"] == first["execution_task_id"]


def test_readonly_completed_replay_does_not_consume_half_open_probe(kanban_home):
    first = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )
    assert first["status"] == "succeeded"
    with kb.connect() as conn:
        for timestamp in (100, 101, 102):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error="bridge outage",
                cooldown_seconds=10,
                now=timestamp,
            )
        assert kb.backend_circuit_states(conn, now=112)["openclaw"] == "half_open"

    replayed = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda _task: pytest.fail(
            "completed replay must not call OpenClaw"
        ),
    )

    assert replayed["status"] == "succeeded"
    assert replayed["deduplicated"] is True
    with kb.connect() as conn:
        assert kb.claim_backend_circuit_probe(conn, "openclaw", now=112)


def test_readonly_half_open_probe_covers_contract_runtime(
    kanban_home, monkeypatch
):
    observed = {}
    monkeypatch.setattr(
        kb,
        "backend_circuit_states",
        lambda _conn: {"openclaw": "half_open"},
    )

    def claim_probe(_conn, backend_id, **kwargs):
        assert backend_id == "openclaw"
        observed["lease_seconds"] = kwargs["lease_seconds"]
        return True

    monkeypatch.setattr(kb, "claim_backend_circuit_probe", claim_probe)

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )

    assert result["status"] == "succeeded"
    assert observed["lease_seconds"] == 150


def test_readonly_nonterminal_response_resumes_through_restart_safe_poller(
    kanban_home,
):
    def admit(task):
        return {
            "task_id": task["task_id"],
            "status": "running",
            "summary": "OpenClaw backend is running.",
            "artifacts": [],
            "tool_calls": [{"name": "openclaw_bridge_http"}],
            "audit_log": ["accepted"],
            "errors": [],
            "requires_human_review": False,
            "recommended_next_action": "Poll the same backend run.",
            "protocol_version": "2.0",
            "protocol_correlated": True,
            "delegation_id": task["delegation_id"],
            "attempt_id": task["attempt_id"],
            "contract_fingerprint": task["contract_fingerprint"],
            "identity_correlated": True,
            "backend_run_id": "openclaw-async-run-1",
            "backend_agent_id": "missioncrew-browser-readonly",
            "backend_session_key": "agent:missioncrew-browser-readonly:async",
        }

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=admit,
    )

    assert result["status"] == "running"
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
        assert execution is not None and execution.status == "running"
        assert review is not None and review.status == "todo"
        assert run is not None
        assert run.status == "running"
        assert run.backend_status == "running"
        assert run.backend_run_id == "openclaw-async-run-1"
        assert run.backend_next_poll_at is not None
        due_at = int(run.backend_next_poll_at)
        start_key = run.metadata["start_idempotency_key"]

    def complete_after_restart(task):
        assert (
            task["openclaw_task_id"]
            == "openclaw.browser.read_snapshot_poll"
        )
        assert task["idempotency_key"].startswith(f"{start_key}:poll:")
        assert task["start_idempotency_key"] == start_key
        assert task["backend_run_id"] == "openclaw-async-run-1"
        assert task["objective"].startswith("Resume")
        terminal = _successful_result(task)
        terminal["backend_run_id"] = "openclaw-async-run-1"
        terminal["backend_session_key"] = (
            "agent:missioncrew-browser-readonly:async"
        )
        return terminal

    polled = poll_due_openclaw_runs(
        owner="browser-restart-poller",
        now=due_at,
        transport=complete_after_restart,
    )

    assert polled.terminal == 1
    assert polled.errors == ()
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
        assert execution is not None and execution.status == "done"
        assert review is not None and review.status == "done"
        assert review.result == "accepted"
        assert run is not None and run.outcome == "completed"
        assert run.backend_status == "succeeded"


def test_readonly_pending_admission_replays_same_start_key(
    kanban_home,
):
    started = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_pending_browser_admission_result,
    )

    assert started["status"] == "retrying"
    with kb.connect() as conn:
        run = kb.latest_run(conn, started["execution_task_id"])
        assert run is not None
        assert run.backend_status == "queued"
        assert run.backend_run_id is None
        assert run.metadata["admission_ambiguous"] is True
        assert run.backend_next_poll_at is not None
        due_at = int(run.backend_next_poll_at)
        start_key = run.metadata["start_idempotency_key"]

    def reconcile(task):
        assert task["openclaw_task_id"] == (
            "openclaw.browser.read_snapshot"
        )
        assert task["idempotency_key"] == start_key
        return {
            **_pending_browser_admission_result(task),
            "status": "running",
            "artifacts": [],
            "backend_run_id": "openclaw-browser-reconciled",
            "backend_agent_id": "missioncrew-browser-readonly",
            "backend_session_key": (
                "agent:missioncrew-browser-readonly:reconciled"
            ),
        }

    polled = poll_due_openclaw_runs(
        owner="browser-admission-reconciler",
        now=due_at,
        transport=reconcile,
    )

    assert polled.observed == 1
    assert polled.errors == ()
    with kb.connect() as conn:
        recovered = kb.latest_run(conn, started["execution_task_id"])
        assert recovered is not None
        assert recovered.backend_status == "running"
        assert recovered.backend_run_id == "openclaw-browser-reconciled"
        assert recovered.metadata["backend_session_key"] == (
            "agent:missioncrew-browser-readonly:reconciled"
        )


def test_readonly_pending_admission_accepts_terminal_rejection_without_run_id(
    kanban_home,
):
    started = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_pending_browser_admission_result,
    )
    with kb.connect() as conn:
        run = kb.latest_run(conn, started["execution_task_id"])
        assert run is not None and run.backend_next_poll_at is not None
        due_at = int(run.backend_next_poll_at)
        start_key = run.metadata["start_idempotency_key"]

    def reject(task):
        assert task["idempotency_key"] == start_key
        result = _pending_browser_admission_result(task)
        result.update(
            {
                "status": "failed",
                "summary": (
                    "OpenClaw rejected browser admission before allocating a run."
                ),
                "artifacts": [],
                "errors": ["admission_rejected"],
            }
        )
        return result

    polled = poll_due_openclaw_runs(
        owner="browser-rejected-admission-poller",
        now=due_at,
        transport=reject,
    )

    assert polled.terminal == 1
    assert polled.errors == ()
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        run = kb.latest_run(conn, started["execution_task_id"])
        assert task is not None and task.status == "blocked"
        assert run is not None and run.backend_status == "failed"
        assert run.backend_run_id is None


def test_readonly_stop_rule_cancels_exact_backend_before_blocking(
    kanban_home,
):
    started = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda task: {
            **_successful_result(task),
            "status": "running",
            "summary": "OpenClaw backend is running.",
            "artifacts": [],
            "backend_run_id": "openclaw-cancel-run",
            "backend_session_key": (
                "agent:missioncrew-browser-readonly:cancel"
            ),
        },
    )
    with kb.connect() as conn:
        run = kb.latest_run(conn, started["execution_task_id"])
        assert run is not None and run.backend_next_poll_at is not None
        assert kb.merge_active_run_metadata(
            conn,
            started["execution_task_id"],
            expected_run_id=run.id,
            metadata={
                "stop_rule_cleanup_pending": True,
                "stop_rule_reason": "max_runtime_seconds reached",
            },
        )
        due_at = int(run.backend_next_poll_at)
        start_key = run.metadata["start_idempotency_key"]

    def cancel(task):
        assert (
            task["openclaw_task_id"]
            == "openclaw.browser.read_snapshot_cancel"
        )
        assert task["start_idempotency_key"] == start_key
        assert task["backend_run_id"] == "openclaw-cancel-run"
        assert task["objective"].startswith("Cancel")
        result = _successful_result(task)
        result.update(
            {
                "status": "blocked",
                "summary": "OpenClaw browser run was cancelled and cleaned.",
                "backend_run_id": "openclaw-cancel-run",
                "backend_session_key": (
                    "agent:missioncrew-browser-readonly:cancel"
                ),
            }
        )
        result["artifacts"] = [
            {
                "type": "openclaw_result",
                "value": {
                    "evidence": {
                        "requestedUrl": "https://example.com/",
                        "externalEffectBudget": 0,
                        "sideEffectsPerformed": False,
                        "terminal": True,
                        "cancellationRequested": True,
                        "terminationProven": True,
                        "sessionCleaned": True,
                        "browserTabsCleaned": True,
                    }
                },
            }
        ]
        return result

    polled = poll_due_openclaw_runs(
        owner="browser-cancel-poller",
        now=due_at,
        transport=cancel,
    )

    assert polled.terminal == 1
    assert polled.errors == ()
    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        run = kb.latest_run(conn, started["execution_task_id"])
        assert execution is not None and execution.status == "blocked"
        blocked_row = conn.execute(
            "SELECT block_kind FROM tasks WHERE id = ?",
            (started["execution_task_id"],),
        ).fetchone()
        assert blocked_row is not None
        assert blocked_row["block_kind"] == "transient"
        assert run is not None and run.backend_status == "blocked"
        evidence = run.metadata["backend_terminal_observation"][
            "delegated_result"
        ]["artifacts"][0]["value"]["evidence"]
        assert evidence["terminationProven"] is True
        assert evidence["browserTabsCleaned"] is True


def test_readonly_invalid_cancel_evidence_retries_without_abandoning_run(
    kanban_home,
):
    started = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda task: {
            **_successful_result(task),
            "status": "running",
            "artifacts": [],
            "backend_run_id": "openclaw-invalid-cancel",
            "backend_session_key": (
                "agent:missioncrew-browser-readonly:invalid-cancel"
            ),
        },
    )
    with kb.connect() as conn:
        run = kb.latest_run(conn, started["execution_task_id"])
        assert run is not None and run.backend_next_poll_at is not None
        assert kb.merge_active_run_metadata(
            conn,
            started["execution_task_id"],
            expected_run_id=run.id,
            metadata={
                "stop_rule_cleanup_pending": True,
                "stop_rule_reason": "max_runtime_seconds reached",
            },
        )
        due_at = int(run.backend_next_poll_at)

    def invalid_cancel(task):
        result = _successful_result(task)
        result.update(
            {
                "status": "blocked",
                "backend_run_id": "openclaw-invalid-cancel",
                "backend_session_key": (
                    "agent:missioncrew-browser-readonly:invalid-cancel"
                ),
            }
        )
        result["artifacts"] = [
            {
                "type": "openclaw_result",
                "value": {
                    "evidence": {
                        "externalEffectBudget": 0,
                        "sideEffectsPerformed": False,
                        "terminal": True,
                        "cancellationRequested": True,
                        "terminationProven": False,
                        "sessionCleaned": False,
                        "browserTabsCleaned": False,
                    }
                },
            }
        ]
        return result

    polled = poll_due_openclaw_runs(
        owner="browser-invalid-cancel-poller",
        now=due_at,
        transport=invalid_cancel,
    )

    assert polled.terminal == 0
    assert polled.retried == 1
    assert "did not prove exact terminal resource cleanup" in polled.errors[0]
    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        run = kb.latest_run(conn, started["execution_task_id"])
        assert execution is not None and execution.status == "running"
        assert run is not None and run.backend_status == "running"
        assert run.backend_next_poll_at is not None
        assert run.metadata["stop_rule_cleanup_pending"] is True
        assert run.metadata["cleanup_attempt_count"] == 1


def test_readonly_cancel_progress_does_not_consume_cleanup_failure_budget(
    kanban_home,
):
    started = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda task: {
            **_successful_result(task),
            "status": "running",
            "artifacts": [],
            "backend_run_id": "openclaw-cancel-progress",
            "backend_session_key": (
                "agent:missioncrew-browser-readonly:cancel-progress"
            ),
        },
    )
    with kb.connect() as conn:
        run = kb.latest_run(conn, started["execution_task_id"])
        assert run is not None and run.backend_next_poll_at is not None
        assert kb.merge_active_run_metadata(
            conn,
            started["execution_task_id"],
            expected_run_id=run.id,
            metadata={
                "stop_rule_cleanup_pending": True,
                "stop_rule_reason": "max_runtime_seconds reached",
            },
        )
        due_at = int(run.backend_next_poll_at)

    def cancellation_progress(task):
        assert ":cancel:" in task["idempotency_key"]
        return {
            **_successful_result(task),
            "status": "running",
            "artifacts": [],
            "backend_run_id": "openclaw-cancel-progress",
            "backend_session_key": (
                "agent:missioncrew-browser-readonly:cancel-progress"
            ),
        }

    polled = poll_due_openclaw_runs(
        owner="browser-cancel-progress-poller",
        now=due_at,
        transport=cancellation_progress,
    )

    assert polled.observed == 1
    assert polled.retried == 0
    assert polled.errors == ()
    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        run = kb.latest_run(conn, started["execution_task_id"])
        assert execution is not None and execution.status == "running"
        assert run is not None and run.backend_status == "running"
        assert run.backend_next_poll_at is not None
        assert run.metadata["stop_rule_cleanup_pending"] is True
        assert run.metadata["cleanup_attempt_count"] == 0


def test_readonly_binding_failure_preserves_pollable_cleanup(
    kanban_home,
    monkeypatch,
):
    monkeypatch.setattr(
        kb,
        "renew_external_backend_claim",
        lambda *_args, **_kwargs: False,
    )

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda task: {
            **_successful_result(task),
            "status": "running",
            "summary": "OpenClaw backend is running.",
            "artifacts": [],
            "backend_run_id": "openclaw-binding-recovery",
            "backend_session_key": (
                "agent:missioncrew-browser-readonly:binding-recovery"
            ),
        },
    )

    assert result["status"] == "running"
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
        assert execution is not None and execution.status == "running"
        assert run is not None
        assert run.backend_status == "running"
        assert run.backend_run_id == "openclaw-binding-recovery"
        assert run.backend_next_poll_at is not None
        assert run.metadata["stop_rule_cleanup_pending"] is True
        assert run.metadata["backend_session_key"].endswith(
            ":binding-recovery"
        )


@pytest.mark.parametrize(
    ("requires_human_review", "result_text"),
    [
        (True, None),
        (False, "{}"),
        (False, "not-json"),
    ],
)
def test_readonly_openclaw_executor_blocks_unreviewable_results(
    kanban_home,
    requires_human_review,
    result_text,
):
    def transport(task):
        return _successful_result(
            task,
            requires_human_review=requires_human_review,
            result_text=result_text,
        )

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=transport,
    )

    assert result["status"] == "blocked"
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
        assert execution is not None and execution.status == "blocked"
        assert review is not None and review.status != "done"
        assert run is not None
        assert run.backend_run_id == "openclaw-run-1"
        assert run.backend_agent_id == "missioncrew-browser-readonly"
        assert run.result_digest


def test_readonly_openclaw_executor_rejects_uncorrelated_protocol_result(
    kanban_home,
):
    def transport(task):
        result = _successful_result(task)
        result["protocol_correlated"] = False
        return result

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=transport,
    )

    assert result["status"] == "blocked"
    assert (
        "OpenClaw result is not correlated to Protocol v2."
        in result["review_errors"]
    )
    with kb.connect() as conn:
        run = kb.latest_run(conn, result["execution_task_id"])
        assert run is not None
        assert run.backend_status == "failed"
        assert run.backend_run_id is None
        assert run.backend_agent_id is None


def test_deduplicated_retry_resumes_incomplete_grace_review(
    kanban_home, monkeypatch
):
    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )
    assert result["status"] == "succeeded"

    with kb.connect() as conn:
        conn.execute(
            """
            UPDATE tasks
               SET status = 'ready', result = NULL, completed_at = NULL
             WHERE id = ?
            """,
            (result["review_task_id"],),
        )
        conn.commit()

    transaction_states = []
    original_claim = kb.claim_task
    original_complete = kb.complete_task

    def observe_claim(conn, task_id, *args, **kwargs):
        if task_id == result["review_task_id"]:
            transaction_states.append(("claim", conn.in_transaction))
        return original_claim(conn, task_id, *args, **kwargs)

    def observe_complete(conn, task_id, *args, **kwargs):
        if task_id == result["review_task_id"]:
            transaction_states.append(("complete", conn.in_transaction))
        return original_complete(conn, task_id, *args, **kwargs)

    monkeypatch.setattr(kb, "claim_task", observe_claim)
    monkeypatch.setattr(kb, "complete_task", observe_complete)

    replay = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=lambda _task: pytest.fail("deduplicated retry must not execute again"),
    )

    assert replay["status"] == "succeeded"
    assert replay["deduplicated"] is True
    assert transaction_states == [("claim", True), ("complete", True)]
    with kb.connect() as conn:
        review = kb.get_task(conn, result["review_task_id"])
        assert review is not None
        assert review.status == "done"
        assert review.result == "accepted"


def test_readonly_openclaw_executor_rejects_url_outside_contract_scope(
    kanban_home,
):
    contract = _contract()
    contract["scope"]["allowed"] = ["https://www.iana.org/"]

    with pytest.raises(ValueError, match="explicitly allowed"):
        execute_readonly_browser_snapshot(
            "https://example.com/",
            contract=contract,
            transport=lambda task: _successful_result(task),
        )

    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_readonly_idempotency_is_scoped_by_project_and_contract(kanban_home):
    def unique_backend_result(task):
        result = _successful_result(task)
        result["backend_run_id"] = f"backend-{task['task_id']}"
        return result

    first = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=unique_backend_result,
    )
    second_contract = _contract()
    second_contract["identity"]["project"] = "another_project"
    second_contract["memory"]["namespace"] = "another_project/openclaw-pilot"
    second = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=second_contract,
        transport=unique_backend_result,
    )

    assert first["execution_task_id"] != second["execution_task_id"]
    assert first["review_task_id"] != second["review_task_id"]


def test_duplicate_backend_run_id_blocks_new_attempt_without_leaving_it_running(
    kanban_home,
):
    first = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=_successful_result,
    )
    assert first["status"] == "succeeded"
    second_contract = _contract()
    second_contract["identity"]["request_instance_id"] = "openclaw-pilot-2"

    second = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=second_contract,
        transport=_successful_result,
    )

    assert second["status"] == "blocked"
    assert "uniquely bound" in second["review_errors"][0]
    with kb.connect() as conn:
        execution = kb.get_task(conn, second["execution_task_id"])
        run = kb.latest_run(conn, second["execution_task_id"])
        assert execution is not None and execution.status == "blocked"
        assert run is not None and run.status == "blocked"
        assert run.ended_at is not None


def test_transport_exception_closes_exact_attempt_as_blocked(kanban_home):
    def transport(_task):
        raise RuntimeError("bridge unavailable")

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=transport,
    )

    assert result["status"] == "blocked"
    assert "bridge unavailable" in result["review_errors"][0]
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        run = kb.latest_run(conn, result["execution_task_id"])
        assert execution is not None and execution.status == "blocked"
        assert run is not None
        assert run.status == "blocked"
        assert run.ended_at is not None


@pytest.mark.parametrize("returned_protocol", [None, "1.0"])
def test_executor_rejects_protocol_downgrade_without_binding(
    kanban_home,
    returned_protocol,
):
    def transport(task):
        result = _successful_result(task)
        if returned_protocol is None:
            result.pop("protocol_version")
        else:
            result["protocol_version"] = returned_protocol
        return result

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=transport,
    )

    assert result["status"] == "blocked"
    with kb.connect() as conn:
        run = kb.latest_run(conn, result["execution_task_id"])
        assert run is not None
        assert run.status == "blocked"
        assert run.backend_run_id is None
        assert run.protocol_version is None


@pytest.mark.parametrize(
    "mutation",
    ["identity", "correlation_flag", "bridge_errors"],
)
def test_executor_rejects_uncorrelated_or_contradictory_success(
    kanban_home,
    mutation,
):
    def transport(task):
        result = _successful_result(task)
        if mutation == "identity":
            result["attempt_id"] = "stale-attempt"
        elif mutation == "correlation_flag":
            result["identity_correlated"] = False
            result["errors"] = ["OpenClaw execution identity did not correlate."]
        else:
            result["errors"] = ["backend reported a hidden failure"]
        return result

    result = execute_readonly_browser_snapshot(
        "https://example.com/",
        contract=_contract(),
        transport=transport,
    )

    assert result["status"] == "blocked"
    with kb.connect() as conn:
        run = kb.latest_run(conn, result["execution_task_id"])
        assert run is not None and run.status == "blocked"
        if mutation in {"identity", "correlation_flag"}:
            assert run.backend_run_id is None
        else:
            assert run.backend_run_id == "openclaw-run-1"
