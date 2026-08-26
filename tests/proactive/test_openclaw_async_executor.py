from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from proactive import openclaw_async_executor
from proactive.backend_poll_worker import poll_due_backend_runs
from proactive.openclaw_async_executor import (
    make_loop_contract_poll_adapter,
    make_loop_contract_terminal_handler,
    make_zero_effect_async_poll_adapter,
    make_zero_effect_async_terminal_handler,
    retry_ready_loop_contract_execution,
    start_loop_contract_execution,
    start_zero_effect_async_acceptance,
)


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
            "topic_name": "openclaw-async",
            "thread_id": "zero-effect-async",
            "request_instance_id": "openclaw-async-1",
        },
        "original_request": "驗證 OpenClaw 真實非同步零副作用執行。",
        "grace_interpretation": "啟動、輪詢、驗收並清理一個零工具任務。",
        "trigger": "Package 3 acceptance",
        "completion_mode": "terminal",
        "goal": {
            "objective": "Verify asynchronous OpenClaw execution.",
            "deliverables": ["Correlated terminal evidence"],
            "non_goals": ["No tools or external effects"],
        },
        "scope": {
            "allowed": ["OpenClaw zero-effect agent session"],
            "forbidden": ["Any external state change"],
        },
        "verification": {
            "checks": ["Backend identity", "Terminal evidence", "Cleanup"],
            "evidence_required": ["Backend run id", "Zero-effect transcript"],
            "acceptance_criteria": ["sideEffectsPerformed=false"],
        },
        "stop_rules": {
            "success": ["Grace review accepts all evidence"],
            "blocked": ["Backend identity mismatch"],
            "no_progress": ["Same poll error twice"],
            "max_iterations": 5,
            "max_runtime_seconds": 120,
        },
        "memory": {
            "namespace": "hub_ops/openclaw-async",
            "working": ["Current async run"],
            "promote_on_acceptance": ["Verified async capability"],
        },
    }


def _result(task, status):
    terminal = status == "succeeded"
    return {
        "task_id": task["task_id"],
        "status": status,
        "summary": f"OpenClaw async run is {status}.",
        "artifacts": (
            [
                {
                    "type": "openclaw_result",
                    "value": {
                        "evidence": {
                            "externalEffectBudget": 0,
                            "sideEffectsPerformed": False,
                            "toolsAllowed": [],
                            "terminal": True,
                            "sessionCleaned": True,
                            "transcriptMessageCount": 1,
                        },
                        "resultText": (
                            '{"result":"zero-effect async completed",'
                            '"sideEffectsPerformed":false}'
                        ),
                    },
                }
            ]
            if terminal
            else []
        ),
        "tool_calls": [{"name": "openclaw_bridge_http"}],
        "audit_log": ["accepted"],
        "errors": [],
        "requires_human_review": False,
        "recommended_next_action": "Poll." if not terminal else "Review.",
        "protocol_version": "2.0",
        "protocol_correlated": True,
        "delegation_id": task["delegation_id"],
        "attempt_id": task["attempt_id"],
        "contract_fingerprint": task["contract_fingerprint"],
        "identity_correlated": True,
        "backend_run_id": "openclaw-real-async-1",
        "backend_agent_id": "missioncrew-browser-readonly",
        "backend_session_key": "agent:missioncrew-browser-readonly:async",
    }


def _pending_admission_result(task):
    result = _result(task, "running")
    result.pop("backend_run_id")
    result.pop("backend_agent_id")
    result.pop("backend_session_key")
    result["artifacts"] = [
        {
            "type": "openclaw_result",
            "value": {
                "evidence": {
                    "externalEffectBudget": 0,
                    "sideEffectsPerformed": False,
                    "toolsAllowed": [],
                    "terminal": False,
                    "admissionPending": True,
                }
            },
        }
    ]
    return result


def _loop_result(task, status):
    terminal = status == "succeeded"
    backend_agent_id = task.get("backend_agent_id") or "missioncrew-executor"
    return {
        "task_id": task["task_id"],
        "status": status,
        "summary": f"OpenClaw Loop Contract is {status}.",
        "artifacts": ([{
            "type": "openclaw_result",
            "value": {
                "evidence": {
                    "terminal": True,
                    "resultContractValid": True,
                    "externalEffectBudget": 0,
                },
                "result": {
                    "status": "succeeded",
                    "summary": "Loop Contract completed.",
                    "acceptanceEvidence": ["verified"],
                    "externalEffects": [],
                },
            },
        }] if terminal else []),
        "tool_calls": [{"name": "openclaw_bridge_http"}],
        "audit_log": ["accepted"],
        "errors": [],
        "requires_human_review": False,
        "recommended_next_action": "Review." if terminal else "Poll.",
        "protocol_version": "2.0",
        "protocol_correlated": True,
        "delegation_id": task["delegation_id"],
        "attempt_id": task["attempt_id"],
        "contract_fingerprint": task["contract_fingerprint"],
        "identity_correlated": True,
        "backend_run_id": "openclaw-loop-run-1",
        "backend_agent_id": backend_agent_id,
        "backend_session_key": (
            task.get("backend_session_key")
            or f"agent:{backend_agent_id}:subagent:test-loop"
        ),
    }


def test_loop_contract_routes_execution_to_openclaw_and_keeps_grace_review(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-routing-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-routing-1",
        transport=lambda task: _loop_result(task, "queued"),
    )

    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        review = kb.get_task(conn, started["review_task_id"])
        run = kb.get_run(conn, int(started["run_id"]))
    assert execution is not None
    assert execution.assignee == "openclaw"
    assert execution.executor_backend == "openclaw"
    assert execution.executor_profile == "loop-contract"
    assert run is not None
    assert run.metadata["backend_agent_id"] == "missioncrew-research"
    role_card = run.metadata["loop_contract"]["routing"]["resolved"][
        "backend_role_card"
    ]
    assert role_card["agent_id"] == "research"
    assert role_card["agent_display_name"] == "Research Agent"
    assert role_card["agent_role"] == "market_research_and_evidence"
    assert role_card["primary_model"] == "codex"
    assert role_card["fallback_model"] == "gemini-3.1-pro"
    assert role_card["worker_id"] == "clawops.research"
    assert role_card["worker_role"] == "research"
    assert role_card["runtime_profile"] == "clawops-research"
    assert role_card["approval_required"] is False
    assert role_card["output_format"] == "markdown"
    assert "evidence" in role_card["required_sections"]
    assert "驗證 OpenClaw 真實非同步零副作用執行。" not in execution.body
    assert review is not None
    assert review.executor_backend == "hermes"
    assert review.executor_profile == "grace-policy-review"


def test_external_loop_contract_requires_scoped_approval(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-external-1"
    contract["external_targets"] = ["Facebook Page: MissionCrew.ai"]

    with pytest.raises(ValueError, match="requires scoped approval"):
        start_loop_contract_execution(
            contract=contract,
            task_type="browser_publish",
            risk_level="high",
            approved=False,
            delegation_id="delegation-loop-external-1",
            transport=lambda task: _loop_result(task, "queued"),
        )


def test_image_generation_loop_contract_requires_matching_backend_capability(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-image-generation-1"
    contract["goal"]["objective"] = "Generate and verify a 16:9 Hero image."
    contract["goal"]["deliverables"] = [
        "Generated image file",
        "Visual inspection",
        "SHA-256",
    ]
    contract["routing"] = {
        "resolved": {
            "assignment": {
                "allowed_tools": [
                    "memory_read",
                    "docs_read",
                    "draft_markdown",
                    "image_generate",
                ],
            },
        },
    }

    with pytest.raises(RuntimeError, match="missing_capabilities:image_generate"):
        start_loop_contract_execution(
            contract=contract,
            task_type="content_draft",
            risk_level="low",
            approved=True,
            delegation_id="delegation-loop-image-generation-1",
            transport=lambda task: _loop_result(task, "queued"),
        )

    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_internal_artifact_sentinel_has_no_external_effect_budget(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-internal-artifact-1"
    contract["external_targets"] = [
        "Internal Topic Instructions artifact only — no external platform action"
    ]

    def transport(task):
        assert task["external_effect_budget"] == 0
        assert task["allowed_tools"] == ["read", "write", "web_search"]
        assert task["credential_refs"] == []
        assert "external_targets" not in task["loop_contract"]
        return _loop_result(task, "queued")

    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-internal-artifact-1",
        transport=transport,
    )

    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0
    assert run.metadata["max_poll_iterations"] == 0


def test_explicit_zh_internal_targets_have_no_external_effect_budget(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-zh-internal-artifact-1"
    contract["external_targets"] = [
        "Facebook Page（僅修訂貼文文案結構與規則，不登入或操作）",
        "Gemini Notebook（僅產出貼入用 Prompt，不登入或操作）",
        "Podcast Hosting／Apple Podcasts（僅產出 Title 與 Description，不上架或操作）",
    ]

    def transport(task):
        assert task["external_effect_budget"] == 0
        assert task["allowed_tools"] == ["read", "write", "web_search"]
        assert "external_targets" not in task["loop_contract"]
        return _loop_result(task, "queued")

    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-zh-internal-artifact-1",
        transport=transport,
    )

    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0


def test_content_draft_url_target_has_no_external_effect_budget(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "tasker-content-draft-1"
    contract["external_targets"] = [
        "https://www.tasker.com.tw/users/tasker/profile"
    ]
    contract["scope"] = {
        "allowed": ["僅產製本地文字草稿"],
        "forbidden": ["瀏覽、登入或修改外部網站"],
    }

    def transport(task):
        assert task["external_effect_budget"] == 0
        assert task["allowed_tools"] == ["read", "write", "web_search"]
        assert task["credential_refs"] == []
        assert "external_targets" not in task["loop_contract"]
        return _loop_result(task, "queued")

    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=True,
        delegation_id="delegation-tasker-content-draft-1",
        transport=transport,
    )

    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0


def test_missing_specialized_loop_agent_workspace_falls_back_to_executor(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "missing-content-agent-1"
    executor_workspace = (
        Path.home()
        / "my_agent_team"
        / "openclaw-workspace"
        / "agents"
        / "missioncrew-executor"
    )
    executor_workspace.mkdir(parents=True)

    def transport(task):
        assert task["backend_agent_id"] == "missioncrew-executor"
        return _loop_result(task, "queued")

    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id="delegation-missing-content-agent-1",
        transport=transport,
    )

    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
    assert run is not None
    assert run.metadata["backend_agent_id"] == "missioncrew-executor"


def test_marketplace_readonly_target_keeps_zero_external_effect_budget(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "marketplace-readonly-1"
    contract["external_targets"] = [
        "Facebook Marketplace listing ID 915975414881937"
    ]

    def transport(task):
        assert task["external_effect_budget"] == 0
        assert not task.get("approval_grant_id")
        assert task["allowed_tools"] == ["read", "web_search", "browser"]
        assert task["credential_refs"] == []
        return _loop_result(task, "queued")

    started = start_loop_contract_execution(
        contract=contract,
        task_type="facebook_marketplace_readonly",
        risk_level="low",
        approved=False,
        delegation_id="delegation-marketplace-readonly-1",
        transport=transport,
    )

    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0
    assert run.metadata["credential_refs"] == []


def test_loop_start_replays_ambiguous_timeout_with_same_key(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-timeout-replay-1"
    seen_keys = []

    def timeout_transport(task):
        seen_keys.append(task["idempotency_key"])
        raise TimeoutError("response lost after admission")

    first = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-timeout-replay-1",
        transport=timeout_transport,
    )
    assert first["status"] == "retrying"

    def replay_transport(task):
        seen_keys.append(task["idempotency_key"])
        return _loop_result(task, "queued")

    replayed = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-timeout-replay-1",
        transport=replay_transport,
    )

    assert replayed["status"] == "queued"
    assert replayed["run_id"] == first["run_id"]
    assert replayed["deduplicated"] is True
    assert seen_keys[0] == seen_keys[1]


def test_loop_start_replays_ambiguous_result_with_same_key(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-result-replay-1"
    seen_keys = []

    def pending_transport(task):
        seen_keys.append(task["idempotency_key"])
        return _pending_admission_result(task)

    first = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-result-replay-1",
        transport=pending_transport,
    )
    assert first["status"] == "retrying"

    def replay_transport(task):
        seen_keys.append(task["idempotency_key"])
        return _loop_result(task, "queued")

    replayed = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-result-replay-1",
        transport=replay_transport,
    )

    assert replayed["status"] == "queued"
    assert replayed["run_id"] == first["run_id"]
    assert seen_keys[0] == seen_keys[1]


def test_loop_start_rejects_uncorrelated_protocol(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-protocol-reject-1"

    def transport(task):
        result = _loop_result(task, "queued")
        result["protocol_correlated"] = False
        return result

    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-protocol-reject-1",
        transport=transport,
    )

    assert started["status"] == "blocked"
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"


def test_loop_contract_terminal_result_releases_grace_review(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-terminal-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-terminal-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    adapter = make_loop_contract_poll_adapter(
        transport=lambda task: _loop_result(task, "succeeded")
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
    observation = adapter(run)
    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is True
    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        review = kb.get_task(conn, started["review_task_id"])
    assert execution is not None and execution.status == "done"
    assert review is not None and review.status in {"ready", "todo"}


def test_loop_contract_terminal_accepts_result_text_json(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-terminal-result-text-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-terminal-result-text-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
    terminal = _loop_result(
        {
            "task_id": run.task_id,
            "delegation_id": run.metadata["delegation_id"],
            "attempt_id": run.metadata["attempt_id"],
            "contract_fingerprint": run.metadata["contract_fingerprint"],
            "backend_agent_id": run.metadata["backend_agent_id"],
            "backend_session_key": run.metadata["backend_session_key"],
        },
        "succeeded",
    )
    output = terminal["artifacts"][0]["value"]
    result = output.pop("result")
    output["resultText"] = json.dumps(result, ensure_ascii=False)
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-result-text-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is True
    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
    assert execution is not None and execution.status == "done"


def test_loop_contract_synchronous_success_is_completed_before_return(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-sync-terminal-1"

    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-sync-terminal-1",
        transport=lambda task: _loop_result(task, "succeeded"),
    )

    assert started["status"] == "succeeded"
    assert started["terminal_review"]["accepted"] is True
    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        review = kb.get_task(conn, started["review_task_id"])
    assert execution is not None and execution.status == "done"
    assert review is not None and review.status in {"ready", "todo"}


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("backend_run_id", "wrong-run"),
        ("backend_agent_id", "wrong-agent"),
        ("backend_session_key", "wrong-session"),
        ("protocol_version", "1.0"),
    ],
)
def test_loop_contract_poll_rejects_cross_run_backend_identity(
    kanban_home,
    field,
    wrong_value,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = f"loop-poll-mismatch-{field}"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id=f"delegation-loop-poll-mismatch-{field}",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
    assert run is not None

    def mismatched(task):
        result = _loop_result(task, "running")
        result[field] = wrong_value
        return result

    adapter = make_loop_contract_poll_adapter(transport=mismatched)
    with pytest.raises(ValueError, match="backend correlation mismatch"):
        adapter(run)


def test_grace_rejected_openclaw_card_is_readmitted_on_same_task(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-correction-1"
    from hermes_cli.telegram_message_path import build_telegram_message_path

    message_path = build_telegram_message_path(
        chat_id="chat-1",
        thread_id="2",
        user_id="kj",
        inbound_message_id="message-correction-1",
        session_key="agent:main:telegram:group:chat-1:2",
        session_id="grace-session-1",
    )
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-correction-1",
        telegram_message_path=message_path,
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
        assert kb.complete_task(
            conn,
            started["execution_task_id"],
            result="incomplete evidence",
            expected_run_id=run.id,
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
            (started["execution_task_id"],),
        )
        conn.execute(
            """
            INSERT INTO task_events (task_id, kind, payload, created_at)
            VALUES (?, 'grace_correction_requested', ?, 1)
            """,
            (
                started["execution_task_id"],
                '{"reason":"candidate URLs are missing"}',
            ),
        )
        conn.commit()

    seen = {}

    def transport(task):
        seen.update(task)
        return _loop_result(task, "queued")

    retried = retry_ready_loop_contract_execution(
        started["execution_task_id"],
        transport=transport,
    )

    assert retried["execution_task_id"] == started["execution_task_id"]
    assert retried["run_id"] != started["run_id"]
    assert seen["objective"] == contract["goal"]["objective"]
    assert seen["external_effect_budget"] == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        run = kb.get_run(conn, int(retried["run_id"]))
        assert task is not None and task.status == "running"
        assert run is not None
        assert run.metadata["correction_reason"] == "candidate URLs are missing"
        assert run.metadata["contract_fingerprint"] != (
            kb.get_run(conn, int(started["run_id"])).metadata["contract_fingerprint"]
        )
        assert run.metadata["loop_contract"]["verification"]["review_feedback"] == [
            "candidate URLs are missing"
        ]
        assert run.metadata["telegram_message_path"]["run_id"] == str(run.id)
        assert run.metadata["telegram_message_path"]["openclaw_backend_run_id"]
        assert (
            run.metadata["backend_telegram_message_path"]["trace_id"]
            == message_path["trace_id"]
        )
        assert (
            run.metadata["loop_contract"]["audit"]["original_request_sha256"]
            == seen["loop_contract"]["audit"]["original_request_sha256"]
        )

    with kb.connect() as conn:
        corrected_run = kb.get_run(conn, int(retried["run_id"]))
        assert corrected_run is not None
        assert kb.complete_task(
            conn,
            started["execution_task_id"],
            result="still incomplete",
            expected_run_id=corrected_run.id,
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
            (started["execution_task_id"],),
        )
        conn.execute(
            """
            INSERT INTO task_events (task_id, kind, payload, created_at)
            VALUES (?, 'grace_correction_requested', ?, 2)
            """,
            (
                started["execution_task_id"],
                '{"reason":"source timestamps are missing"}',
            ),
        )
        conn.commit()

    second_retry = retry_ready_loop_contract_execution(
        started["execution_task_id"],
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        second_run = kb.get_run(conn, int(second_retry["run_id"]))
    assert second_run is not None
    assert second_run.metadata["loop_contract"]["verification"][
        "review_feedback"
    ] == ["candidate URLs are missing", "source timestamps are missing"]


def test_grace_correction_does_not_auto_replay_external_effects(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-effect-correction-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-effect-correction-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
        assert kb.merge_active_run_metadata(
            conn,
            started["execution_task_id"],
            expected_run_id=run.id,
            metadata={
                "external_effect_budget": 1,
                "approval_grant_id": "original-scoped-grant",
            },
        )
        assert kb.complete_task(
            conn,
            started["execution_task_id"],
            result="evidence incomplete",
            expected_run_id=run.id,
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
            (started["execution_task_id"],),
        )
        conn.execute(
            """
            INSERT INTO task_events (task_id, kind, payload, created_at)
            VALUES (?, 'grace_correction_requested', '{}', 1)
            """,
            (started["execution_task_id"],),
        )
        conn.commit()

    with pytest.raises(ValueError, match="fresh scoped approval"):
        retry_ready_loop_contract_execution(
            started["execution_task_id"],
            transport=lambda _task: pytest.fail("must not replay an external effect"),
        )


def test_zero_budget_terminal_rejects_reported_external_effect(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-effect-evidence-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-effect-evidence-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
    terminal = _loop_result(
        {
            "task_id": run.task_id,
            "delegation_id": run.metadata["delegation_id"],
            "attempt_id": run.metadata["attempt_id"],
            "contract_fingerprint": run.metadata["contract_fingerprint"],
            "backend_session_key": run.metadata["backend_session_key"],
        },
        "succeeded",
    )
    terminal["artifacts"][0]["value"]["result"]["externalEffects"] = [
        {"platform": "facebook", "state": "published"}
    ]
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is False
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"


def test_async_openclaw_start_poll_terminal_and_grace_review(kanban_home):
    poll_statuses = iter(["running", "succeeded"])

    def transport(task):
        assert task["allowed_tools"] == []
        assert task["external_effect_budget"] == 0
        assert task["dry_run"] is False
        if task["openclaw_task_id"] == "openclaw.agent.zero_effect_async_start":
            return _result(task, "queued")
        assert task["openclaw_task_id"] == "openclaw.agent.zero_effect_async_poll"
        assert task["start_idempotency_key"].endswith(":async-start")
        assert task["backend_run_id"] == "openclaw-real-async-1"
        return _result(task, next(poll_statuses))

    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=transport,
    )

    assert started["status"] == "queued"
    replayed = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda _task: pytest.fail(
            "deduplicated active admission must not call OpenClaw again"
        ),
    )
    assert replayed["deduplicated"] is True
    assert replayed["run_id"] == started["run_id"]
    assert replayed["backend_run_id"] == started["backend_run_id"]
    adapter = make_zero_effect_async_poll_adapter(transport=transport)
    handler = make_zero_effect_async_terminal_handler()
    with kb.connect() as conn:
        run = kb.get_run(conn, started["run_id"])
        assert run is not None and run.backend_next_poll_at is not None
        first_due = int(run.backend_next_poll_at)

    first = poll_due_backend_runs(
        adapters={"openclaw": adapter},
        terminal_handlers={"openclaw": handler},
        owner="async-test-poller",
        now=first_due,
    )
    assert first.as_dict() == {
        "claimed": 1,
        "observed": 1,
        "terminal": 0,
        "retried": 0,
        "errors": [],
    }
    with kb.connect() as conn:
        run = kb.get_run(conn, started["run_id"])
        assert run is not None
        assert run.backend_status == "running"
        assert run.backend_next_poll_at is not None
        second_due = int(run.backend_next_poll_at)

    second = poll_due_backend_runs(
        adapters={"openclaw": adapter},
        terminal_handlers={"openclaw": handler},
        owner="async-test-poller",
        now=second_due,
    )
    assert second.terminal == 1
    assert second.errors == ()
    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        review = kb.get_task(conn, started["review_task_id"])
        run = kb.get_run(conn, started["run_id"])
        assert execution is not None and execution.status == "done"
        assert review is not None and review.status == "done"
        assert review.result == "accepted"
        assert run is not None and run.backend_status == "succeeded"
        assert run.outcome == "completed"
        assert run.metadata["backend_terminal_observation"][
            "delegated_result"
        ]["backend_run_id"] == "openclaw-real-async-1"
        assert run.metadata["side_effects_performed"] is False


def test_async_start_accepts_immediate_terminal_success(kanban_home):
    result = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda task: _result(task, "succeeded"),
    )

    assert result["status"] == "succeeded"
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        assert execution is not None and execution.status == "done"
        assert review is not None and review.status == "done"


def test_async_replay_finalizes_persisted_immediate_terminal_observation(
    kanban_home, monkeypatch
):
    original_factory = (
        openclaw_async_executor.make_zero_effect_async_terminal_handler
    )

    def crash_before_terminal_review(*, board=None):
        def crash(_run, _observation):
            raise KeyboardInterrupt("process exited before terminal review")

        return crash

    monkeypatch.setattr(
        openclaw_async_executor,
        "make_zero_effect_async_terminal_handler",
        crash_before_terminal_review,
    )
    with pytest.raises(KeyboardInterrupt):
        start_zero_effect_async_acceptance(
            contract=_contract(),
            transport=lambda task: _result(task, "succeeded"),
        )
    with kb.connect() as conn:
        interrupted = conn.execute(
            """
            SELECT r.backend_next_poll_at
              FROM task_runs r
              JOIN tasks t ON t.current_run_id = r.id
             WHERE t.idempotency_key LIKE 'openclaw-zero-effect:%'
            """
        ).fetchone()
        assert interrupted is not None
        assert interrupted["backend_next_poll_at"] is not None

    monkeypatch.setattr(
        openclaw_async_executor,
        "make_zero_effect_async_terminal_handler",
        original_factory,
    )
    replayed = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda _task: pytest.fail(
            "persisted terminal replay must not call OpenClaw"
        ),
    )

    assert replayed["status"] == "succeeded"
    assert replayed["deduplicated"] is True


def test_async_start_replays_ambiguous_timeout_with_same_key(kanban_home):
    first = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda _task: (_ for _ in ()).throw(
            TimeoutError("response lost")
        ),
    )

    assert first["status"] == "retrying"
    with kb.connect() as conn:
        first_run = kb.latest_run(conn, first["execution_task_id"])
        assert first_run is not None
        assert first_run.backend_status == "queued"
        assert first_run.backend_next_poll_at is not None
        start_key = first_run.metadata["start_idempotency_key"]
        retry_due = int(first_run.backend_next_poll_at)

    def replay(task):
        assert task["idempotency_key"] == start_key
        return _result(task, "queued")

    second = poll_due_backend_runs(
        adapters={
            "openclaw": make_zero_effect_async_poll_adapter(
                transport=replay
            )
        },
        terminal_handlers={
            "openclaw": make_zero_effect_async_terminal_handler()
        },
        owner="ambiguous-admission-poller",
        now=retry_due,
    )

    assert second.observed == 1
    with kb.connect() as conn:
        recovered = kb.get_run(conn, first_run.id)
        assert recovered is not None
        assert recovered.backend_status == "queued"
        assert recovered.backend_run_id == "openclaw-real-async-1"


def test_async_start_reconciles_pending_admission_without_duplicate_run(
    kanban_home,
    monkeypatch,
):
    original_renew = kb.renew_external_backend_claim
    monkeypatch.setattr(
        kb,
        "renew_external_backend_claim",
        lambda *_args, **_kwargs: False,
    )
    first = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=_pending_admission_result,
    )

    assert first["status"] == "retrying"
    assert first["claim_renewed"] is False
    with kb.connect() as conn:
        task = kb.get_task(conn, first["execution_task_id"])
        pending_run = kb.get_run(conn, first["run_id"])
        assert task is not None and task.status == "running"
        assert pending_run is not None
        assert pending_run.backend_status == "queued"
        assert pending_run.backend_run_id is None
        assert pending_run.metadata["admission_ambiguous"] is True
        assert pending_run.backend_next_poll_at is not None
        retry_due = int(pending_run.backend_next_poll_at)
        start_key = pending_run.metadata["start_idempotency_key"]
    monkeypatch.setattr(
        kb,
        "renew_external_backend_claim",
        original_renew,
    )

    def reconcile(task):
        assert task["openclaw_task_id"] == (
            "openclaw.agent.zero_effect_async_start"
        )
        assert task["idempotency_key"] == start_key
        return _result(task, "queued")

    observed = poll_due_backend_runs(
        adapters={
            "openclaw": make_zero_effect_async_poll_adapter(
                transport=reconcile
            )
        },
        terminal_handlers={
            "openclaw": make_zero_effect_async_terminal_handler()
        },
        owner="pending-admission-poller",
        now=retry_due,
    )

    assert observed.observed == 1
    with kb.connect() as conn:
        reconciled = kb.get_run(conn, first["run_id"])
        assert reconciled is not None
        assert reconciled.backend_status == "queued"
        assert reconciled.backend_run_id == "openclaw-real-async-1"


def test_async_pending_admission_accepts_terminal_rejection_without_run_id(
    kanban_home,
):
    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=_pending_admission_result,
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, started["run_id"])
        assert run is not None and run.backend_next_poll_at is not None
        due_at = int(run.backend_next_poll_at)
        start_key = run.metadata["start_idempotency_key"]

    def reject(task):
        assert task["idempotency_key"] == start_key
        result = _result(task, "blocked")
        result.pop("backend_run_id")
        result.pop("backend_agent_id")
        result.pop("backend_session_key")
        result["summary"] = "OpenClaw rejected admission before allocating a run."
        result["errors"] = ["admission_rejected"]
        return result

    observed = poll_due_backend_runs(
        adapters={
            "openclaw": make_zero_effect_async_poll_adapter(
                transport=reject
            )
        },
        terminal_handlers={
            "openclaw": make_zero_effect_async_terminal_handler()
        },
        owner="rejected-admission-poller",
        now=due_at,
    )

    assert observed.terminal == 1
    assert observed.errors == ()
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        run = kb.get_run(conn, started["run_id"])
        assert task is not None and task.status == "blocked"
        assert run is not None and run.backend_status == "blocked"
        assert run.backend_run_id is None


def test_async_stop_rule_uses_cancel_and_closes_only_after_cleanup_evidence(
    kanban_home,
):
    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda task: _result(task, "queued"),
    )
    with kb.connect() as conn:
        assert kb.merge_active_run_metadata(
            conn,
            started["execution_task_id"],
            expected_run_id=started["run_id"],
            metadata={
                "stop_rule_cleanup_pending": True,
                "stop_rule_reason": "max_runtime_seconds reached",
            },
        )
        run = kb.get_run(conn, started["run_id"])
        assert run is not None and run.backend_next_poll_at is not None
        due_at = int(run.backend_next_poll_at)

    def cancel(task):
        assert (
            task["openclaw_task_id"]
            == "openclaw.agent.zero_effect_async_cancel"
        )
        result = _result(task, "blocked")
        result["artifacts"] = [
            {
                "type": "openclaw_result",
                "value": {
                    "evidence": {
                        "externalEffectBudget": 0,
                        "sideEffectsPerformed": False,
                        "toolsAllowed": [],
                        "terminal": True,
                        "cancellationRequested": True,
                        "terminationProven": True,
                        "sessionCleaned": True,
                    }
                },
            }
        ]
        return result

    polled = poll_due_backend_runs(
        adapters={
            "openclaw": make_zero_effect_async_poll_adapter(
                transport=cancel
            )
        },
        terminal_handlers={
            "openclaw": make_zero_effect_async_terminal_handler()
        },
        owner="stop-rule-cancel-poller",
        now=due_at,
    )

    assert polled.terminal == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        run = kb.get_run(conn, started["run_id"])
        assert task is not None and task.status == "blocked"
        assert run is not None and run.backend_status == "blocked"
        evidence = run.metadata["backend_terminal_observation"][
            "delegated_result"
        ]["artifacts"][0]["value"]["evidence"]
        assert evidence["terminationProven"] is True
        assert evidence["sessionCleaned"] is True


def test_async_half_open_probe_covers_contract_runtime(
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

    result = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda task: _result(task, "queued"),
    )

    assert result["status"] == "queued"
    assert observed["lease_seconds"] == 150


def test_async_deduplicated_active_run_bypasses_open_circuit(kanban_home):
    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda task: _result(task, "queued"),
    )
    with kb.connect() as conn:
        for offset in range(3):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error="bridge outage",
                now=100 + offset,
            )

    replayed = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda _task: pytest.fail(
            "durable active replay must not call OpenClaw"
        ),
    )

    assert replayed["status"] == "queued"
    assert replayed["deduplicated"] is True
    assert replayed["run_id"] == started["run_id"]


def test_async_admission_replays_after_process_exit_before_backend_state(
    kanban_home,
):
    def process_exit(_task):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        start_zero_effect_async_acceptance(
            contract=_contract(),
            transport=process_exit,
        )

    with kb.connect() as conn:
        task = conn.execute(
            "SELECT id FROM tasks WHERE title = ?",
            ("OpenClaw zero-effect asynchronous acceptance",),
        ).fetchone()
        assert task is not None
        interrupted_run = kb.latest_run(conn, str(task["id"]))
        assert interrupted_run is not None
        assert interrupted_run.backend_status is None
        interrupted_run_id = interrupted_run.id
        interrupted_start_key = interrupted_run.metadata["start_idempotency_key"]
        now = int(kb.time.time())
        for offset in range(3):
            kb.record_backend_circuit_outcome(
                conn,
                "openclaw",
                succeeded=False,
                error="new concurrent outage",
                now=now + offset,
            )

    def replay(task):
        assert task["idempotency_key"] == interrupted_start_key
        return _result(task, "queued")

    resumed = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=replay,
    )

    assert resumed["status"] == "queued"
    assert resumed["deduplicated"] is True
    assert resumed["run_id"] == interrupted_run_id
    assert resumed["backend_run_id"] == "openclaw-real-async-1"
    with kb.connect() as conn:
        assert kb.backend_circuit_states(
            conn,
            now=now + 2,
        )["openclaw"] == "open"


def test_async_admission_failure_is_counted_by_circuit_breaker(kanban_home):
    result = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda _task: (_ for _ in ()).throw(
            RuntimeError("bridge unavailable")
        ),
    )

    assert result["status"] == "blocked"
    with kb.connect() as conn:
        row = conn.execute(
            """
            SELECT consecutive_failures, last_error
              FROM execution_backend_circuits
             WHERE backend_id = 'openclaw'
            """
        ).fetchone()
        assert row is not None
        assert row["consecutive_failures"] == 1
        assert "bridge unavailable" in row["last_error"]


def test_async_reservation_rolls_back_when_review_creation_fails(
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
        start_zero_effect_async_acceptance(contract=_contract())

    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("evidence_key", "invalid_value"),
    [
        ("toolsAllowed", ["browser.read"]),
        ("sessionCleaned", False),
    ],
)
def test_async_terminal_review_rejects_unproven_zero_tool_or_cleanup_evidence(
    kanban_home, evidence_key, invalid_value
):
    def transport(task):
        if task["openclaw_task_id"] == "openclaw.agent.zero_effect_async_start":
            return _result(task, "queued")
        result = _result(task, "succeeded")
        result["artifacts"][0]["value"]["evidence"][evidence_key] = invalid_value
        return result

    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=transport,
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, started["run_id"])
        assert run is not None and run.backend_next_poll_at is not None
        due_at = int(run.backend_next_poll_at)

    result = poll_due_backend_runs(
        adapters={
            "openclaw": make_zero_effect_async_poll_adapter(transport=transport)
        },
        terminal_handlers={"openclaw": make_zero_effect_async_terminal_handler()},
        owner="invalid-evidence-poller",
        now=due_at,
    )

    assert result.terminal == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        review = kb.get_task(conn, started["review_task_id"])
        assert task is not None and task.status == "blocked"
        assert review is not None and review.status == "todo"


def test_async_terminal_failure_can_omit_redundant_backend_run_evidence(
    kanban_home,
):
    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda task: _result(task, "queued"),
    )

    def failed_without_backend_evidence(task):
        result = _result(task, "failed")
        result.pop("backend_run_id")
        return result

    adapter = make_zero_effect_async_poll_adapter(
        transport=failed_without_backend_evidence
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None

    observation = adapter(run)

    assert observation["status"] == "failed"
    assert observation["backend_run_id"] == started["backend_run_id"]


def test_async_poll_rejects_a_different_backend_session(kanban_home):
    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda task: _result(task, "queued"),
    )

    def different_session(task):
        result = _result(task, "running")
        result["backend_session_key"] = "agent:missioncrew-browser-readonly:other"
        return result

    adapter = make_zero_effect_async_poll_adapter(transport=different_session)
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None

    with pytest.raises(ValueError, match="different backend session"):
        adapter(run)


def test_terminal_digest_ignores_non_evidence_wrapper_drift(kanban_home):
    started = start_zero_effect_async_acceptance(
        contract=_contract(),
        transport=lambda task: _result(task, "queued"),
    )
    summaries = iter(["first summary", "second summary"])

    def terminal_with_drifting_summary(task):
        result = _result(task, "succeeded")
        result["summary"] = next(summaries)
        result["audit_log"] = [{"observedAt": result["summary"]}]
        return result

    adapter = make_zero_effect_async_poll_adapter(
        transport=terminal_with_drifting_summary
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None

    first = adapter(run)
    second = adapter(run)

    assert first["result_digest"] == second["result_digest"]
