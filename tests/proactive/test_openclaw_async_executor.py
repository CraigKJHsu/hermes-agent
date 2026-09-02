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
    retry_ready_approved_loop_contract_after_capability_repair,
    retry_ready_loop_contract_execution,
    start_loop_contract_execution,
    start_zero_effect_async_acceptance,
)
from proactive.policy_registry import bind_topic_policies, create_policy_version


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    source = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "managed-policies"
        / "missioncrew-model-routing-v1.json"
    ).read_text(encoding="utf-8")
    create_policy_version(
        "missioncrew-model-routing-v1",
        "v1",
        source,
        owner_scope="global",
        owner_id="missioncrew",
        activate=True,
        expected_active_version=None,
    )
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


def test_facebook_page_api_uses_dedicated_openclaw_capability(monkeypatch):
    contract = _contract()
    contract["routing"] = {
        "task_type": "facebook_page_api_publish",
        "resolved": {
            "assignment": {
                "assigned_worker": "clawops.facebook_page_api",
                "allowed_tools": [
                    "facebook_page_graph_status",
                    "facebook_page_graph_publish",
                ],
            }
        },
    }
    monkeypatch.setattr(
        openclaw_async_executor,
        "_existing_loop_agent_or_executor",
        lambda agent_id: agent_id,
    )

    assert openclaw_async_executor._loop_allowed_tools(
        "facebook_page_api_publish",
        external_effects=True,
        contract_tools=openclaw_async_executor._contract_runtime_tools(contract),
    ) == ["facebook_page_graph_status", "facebook_page_graph_publish"]
    assert openclaw_async_executor._loop_backend_agent_id(
        contract,
        task_type="facebook_page_api_publish",
        external_effects=True,
    ) == "missioncrew-facebook-page-operator"


def test_facebook_page_preflight_uses_one_zero_effect_capability(monkeypatch):
    contract = _contract()
    contract["routing"] = {
        "task_type": "facebook_page_publish_preflight",
        "resolved": {
            "assignment": {
                "assigned_worker": "clawops.facebook_page_preflight",
                "allowed_tools": ["facebook_page_publish_preflight"],
            }
        },
    }
    monkeypatch.setattr(
        openclaw_async_executor,
        "_existing_loop_agent_or_executor",
        lambda agent_id: agent_id,
    )

    assert openclaw_async_executor._loop_allowed_tools(
        "facebook_page_publish_preflight",
        external_effects=False,
        contract_tools=openclaw_async_executor._contract_runtime_tools(contract),
    ) == ["facebook_page_publish_preflight"]
    assert openclaw_async_executor._loop_backend_agent_id(
        contract,
        task_type="facebook_page_publish_preflight",
        external_effects=False,
    ) == "missioncrew-facebook-page-operator"


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
    snapshots = (task.get("loop_contract") or {}).get("policy_snapshots") or []
    policy_receipts = [
        {
            "role": "execution",
            "policy_id": item["policy_id"],
            "version": item["version"],
            "sha256": item["sha256"],
            "loaded": True,
        }
        for item in snapshots
    ]
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
                    "policyReceipts": policy_receipts,
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
        task_type="content_draft",
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
    assert run.metadata["backend_agent_id"] == "missioncrew-executor"
    role_card = run.metadata["loop_contract"]["routing"]["resolved"][
        "backend_role_card"
    ]
    assert role_card["agent_id"] == "content_creator"
    assert role_card["agent_display_name"] == "Content Creator Agent"
    assert role_card["agent_role"] == "content_drafting"
    assert role_card["primary_model"] == "openai/gpt-image-2"
    assert role_card["fallback_model"] == "codex"
    assert role_card["worker_id"] == "missioncrew.content"
    assert role_card["worker_role"] == "content"
    assert role_card["runtime_profile"] == "missioncrew-content"
    assert role_card["approval_required"] is True
    assert role_card["output_format"] == "markdown"
    assert "draft" in role_card["required_sections"]
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


def test_approved_external_capability_recovery_supports_browser_write_topics(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-external-recovery-1"
    contract["external_targets"] = ["Facebook group: 1333742673375089"]
    contract["routing"] = {
        "resolved": {
            "assignment": {
                "allowed_tools": ["browser"],
                "assigned_worker": "missioncrew.browser",
                "required_agent_id": "missioncrew-browser-operator",
            }
        }
    }

    started = start_loop_contract_execution(
        contract=contract,
        task_type="facebook_group_relist",
        risk_level="high",
        approved=True,
        delegation_id="delegation-loop-external-recovery-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
        polluted_metadata = dict(run.metadata)
        polluted_contract = dict(polluted_metadata["loop_contract"])
        polluted_contract["original_request"] = "[SYSTEM: Grace Loop callback]\nreplay envelope"
        polluted_metadata["loop_contract"] = polluted_contract
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps(polluted_metadata), run.id),
        )
        assert kb.block_task(
            conn,
            started["execution_task_id"],
            reason="OpenClaw Loop Contract admission raised: missing browser capability",
            kind="capability",
            expected_run_id=run.id,
        )
        assert kb.unblock_task(conn, started["execution_task_id"])

    seen = {}

    def transport(task):
        seen.update(task)
        return _loop_result(task, "queued")

    retried = retry_ready_approved_loop_contract_after_capability_repair(
        started["execution_task_id"],
        transport=transport,
    )

    assert retried["execution_task_id"] == started["execution_task_id"]
    assert retried["status"] == "queued"
    assert seen["external_effect_budget"] == 1
    assert seen["task_type"] == "facebook_group_relist"
    assert "[SYSTEM: Grace Loop callback]" not in json.dumps(
        seen["loop_contract"],
        ensure_ascii=False,
    )
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        run = kb.get_run(conn, int(retried["run_id"]))
    assert task is not None and task.status == "running"
    assert run is not None
    assert run.metadata["capability_recovery"] is True
    assert run.metadata["approval_grant_id"] == "delegation-loop-external-recovery-1"


def test_image_generation_loop_contract_routes_with_backend_capability(
    kanban_home,
):
    (
        kanban_home.parent
        / "my_agent_team"
        / "openclaw-workspace"
        / "agents"
        / "missioncrew-content"
    ).mkdir(parents=True)
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

    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=True,
        delegation_id="delegation-loop-image-generation-1",
        transport=lambda task: _loop_result(task, "queued", "missioncrew-content"),
    )

    with kb.connect() as conn:
        execution = kb.get_task(conn, started["execution_task_id"])
        run = kb.get_run(conn, int(started["run_id"]))

    assert execution is not None
    assert execution.executor_backend == "openclaw"
    assert execution.executor_profile == "loop-contract"
    assert run is not None
    assert run.metadata["backend_agent_id"] == "missioncrew-content"
    assert "image_generate" in run.metadata["allowed_tools"]


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
    contract["objective_ref"] = {
        "objective_id": "go-ext-marketplace-readonly-1",
        "stage_key": "prepare-readonly",
    }
    with kb.connect() as conn:
        kb.create_grace_objective(
            conn,
            objective_id="go-ext-marketplace-readonly-1",
            platform="telegram",
            chat_id="chat-1",
            thread_id="topic-1",
            session_key="agent:main:telegram:chat-1:topic-1",
            title="Marketplace read-only recovery",
            objective="Recover exact destination evidence before approval.",
            original_request_sha256="a" * 64,
            required_stage_keys=("prepare-readonly", "execute_external_action"),
            terminal_stage_key="execute_external_action",
            acceptance_criteria=("Recover evidence or return a blocker.",),
            current_stage_key="prepare-readonly",
        )

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
        task = kb.get_task(conn, started["execution_task_id"])
        run = kb.get_run(conn, int(started["run_id"]))
    assert task is not None
    assert task.model_override == "gpt-5.5"
    assert run is not None
    assert run.metadata["external_effect_budget"] == 0
    assert run.metadata["credential_refs"] == []
    snapshot = run.metadata["loop_contract"]["durable_evidence_snapshot"]
    assert snapshot["objective_id"] == "go-ext-marketplace-readonly-1"
    assert snapshot["stage_key"] == "prepare-readonly"
    assert snapshot["objective"]["current_stage_key"] == "prepare-readonly"


def test_browser_readonly_task_gets_browser_readback_tools(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "browser-readonly-tools-1"

    def transport(task):
        assert task["backend_agent_id"] == "missioncrew-browser-readonly"
        assert task["external_effect_budget"] == 0
        assert not task.get("approval_grant_id")
        assert task["allowed_tools"] == ["read", "web_search", "browser"]
        assert task["credential_refs"] == []
        return _loop_result(task, "queued")

    started = start_loop_contract_execution(
        contract=contract,
        task_type="browser_readonly",
        risk_level="low",
        approved=False,
        delegation_id="delegation-browser-readonly-tools-1",
        transport=transport,
    )

    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
    assert run is not None
    assert run.metadata["task_type"] == "browser_readonly"
    assert run.metadata["external_effect_budget"] == 0
    assert run.metadata["allowed_tools"] == ["read", "web_search", "browser"]
    assert run.metadata["credential_refs"] == []


def test_browser_readonly_correction_does_not_inherit_stale_write_tool(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "browser-readonly-correction-tools-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="browser_readonly",
        risk_level="low",
        approved=False,
        delegation_id="delegation-browser-readonly-correction-tools-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
        stale_metadata = dict(run.metadata)
        stale_metadata["allowed_tools"] = ["read", "write", "web_search"]
        assert kb.merge_active_run_metadata(
            conn,
            started["execution_task_id"],
            metadata=stale_metadata,
            expected_run_id=run.id,
        )
        assert kb.block_task(
            conn,
            started["execution_task_id"],
            reason="OpenClaw Loop Contract was blocked before verified completion: browser unavailable",
            kind="capability",
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
                '{"reason":"read-only browser tools were missing"}',
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

    assert retried["status"] == "queued"
    assert seen["allowed_tools"] == ["read", "web_search", "browser"]
    with kb.connect() as conn:
        retried_run = kb.get_run(conn, int(retried["run_id"]))
    assert retried_run is not None
    assert retried_run.metadata["allowed_tools"] == ["read", "web_search", "browser"]


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


def test_domain_mutation_loop_contract_sends_terminal_result_contract(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-domain-mutation-schema-1"
    contract["domain_memory"] = {
        "schema_id": "secondhand.item.v1",
        "mode": "mutate",
        "require_delta_on_acceptance": True,
    }
    contract["facebook_group_publish"] = {
        "mode": "canonical_url_per_group",
        "source_listing_id": "37276725125275496",
        "management_listing_id": "915975414881937",
        "destinations": [
            {
                "group_id": "207110076321670",
                "canonical_name": "二手家電冷氣買賣",
                "canonical_url": "https://www.facebook.com/groups/207110076321670",
            }
        ],
    }
    contract["external_targets"] = [
        "facebook marketplace listing 37276725125275496",
        "https://www.facebook.com/groups/207110076321670",
    ]
    seen: dict[str, object] = {}

    def transport(task):
        seen.update(task)
        return _loop_result(task, "queued")

    started = start_loop_contract_execution(
        contract=contract,
        task_type="facebook_marketplace_group_publish",
        risk_level="high",
        approved=True,
        delegation_id="delegation-loop-domain-mutation-schema-1",
        transport=transport,
    )

    assert started["status"] == "queued"
    result_contract = seen["loop_contract"]["terminal_result_contract"]
    assert result_contract["format"] == "single_valid_json_object"
    assert "python_expression" in result_contract["forbidden"]
    assert "domainMemoryDeltas" in result_contract["required_top_level_keys"]
    assert (
        result_contract["domainMemoryDeltas"]["shape"]
        == "array of entity deltas; each delta must use artifacts[] for artifact state"
    )
    assert (
        "artifact_type"
        in result_contract["domainMemoryDeltas"]["forbidden_top_level_artifact_fields"]
    )
    assert result_contract["domainMemoryDeltas"]["artifact_types"] == [
        "shopee_listing",
        "facebook_marketplace_listing",
        "facebook_group_post",
    ]
    assert (
        result_contract["externalEffects"]["zero_effects_allowed_only_when_status"]
        == "blocked"
    )
    assert result_contract["facebook_group_publish"]["destination_count"] == 1
    assert (
        result_contract["facebook_group_publish"]["per_destination_external_effect"][
            "effect_key"
        ]
        == "group:<group_id>"
    )


def test_domain_mutation_terminal_blocks_invalid_memory_delta_without_crash(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-domain-mutation-invalid-delta-1"
    contract["domain_memory"] = {
        "schema_id": "secondhand.item.v1",
        "mode": "mutate",
        "require_delta_on_acceptance": True,
    }
    contract["facebook_group_publish"] = {
        "mode": "canonical_url_per_group",
        "source_listing_id": "37276725125275496",
        "management_listing_id": "915975414881937",
        "destinations": [
            {
                "group_id": "207110076321670",
                "canonical_name": "二手家電冷氣買賣",
                "canonical_url": "https://www.facebook.com/groups/207110076321670",
            }
        ],
    }
    contract["external_targets"] = [
        "facebook marketplace listing 37276725125275496",
        "https://www.facebook.com/groups/207110076321670",
    ]
    started = start_loop_contract_execution(
        contract=contract,
        task_type="facebook_marketplace_group_publish",
        risk_level="high",
        approved=True,
        delegation_id="delegation-loop-domain-mutation-invalid-delta-1",
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
    output["evidence"]["externalEffectBudget"] = run.metadata["external_effect_budget"]
    output["result"] = {
        "status": "succeeded",
        "summary": "Returned flat artifact-style domain deltas.",
        "acceptanceEvidence": ["worker claims completion"],
        "externalEffects": [],
        "domainMemoryDeltas": [
            {
                "operation": "upsert_artifact",
                "entity_id": "kolin-kd-291m06",
                "label": "Kolin KD-291M06",
                "status": "published",
                "artifact_type": "facebook_group_post",
                "platform": "facebook",
                "public_url": (
                    "https://www.facebook.com/groups/207110076321670/posts/1"
                ),
            }
        ],
    }
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-invalid-domain-delta-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is False
    assert "Domain memory:" in handled["reason"]
    assert "artifact state inside artifacts[]" in handled["reason"]
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"
    assert ended_run is not None and ended_run.status == "blocked"
    assert ended_run.metadata["loop_contract_blocked_result"]["status"] == "succeeded"
    assert ended_run.metadata["domain_memory_delta_error"].startswith(
        "metadata.domain_memory_deltas[0] must be an entity delta"
    )
    assert ended_run.metadata["external_effects"] == []


@pytest.mark.parametrize("wire_format", [
    "existing_package", "canonical_report", "missing_report", "wrong_hash",
    "wrong_filename", "missing_image", "object_body", "reference_body", "see_reference_body",
    "prose_reference_body", "metadata_reference_body", "symlink_asset",
    "cjk_reference_body", "cjk_metadata_reference_body",
])
def test_loop_contract_terminal_promotes_content_package_for_gateway_delivery(
    kanban_home,
    wire_format,
):
    page = kanban_home / "page.png"
    cover = kanban_home / "cover.png"
    page.write_bytes(b"page-image")
    cover.write_bytes(b"cover-image")
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-content-package-1"
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_with_attachment",
        "subject_keys": ["page-body", "page-hero", "audio-brief"],
        "asset_filenames": [page.name, cover.name],
    }
    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-content-package-1",
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
    terminal["artifacts"][0]["value"]["result"]["acceptanceEvidence"] = {
        "telegram_user_facing_content_package": {
            "facebook_page_body": "完整 Page 內文\n\n#FinalHashtag",
            "group_discussion_copy": "Group 討論附文",
            "gemini_notebook_audio_prompt": "Gemini prompt",
            "podcast_title": "Podcast title",
            "podcast_description": "Podcast description",
            "image_attachments": [
                {
                    "filename": page.name,
                    "path": str(page),
                    "asset_family": "page_hero",
                    "sha256": openclaw_async_executor.hashlib.sha256(
                        page.read_bytes()
                    ).hexdigest(),
                },
                {
                    "filename": cover.name,
                    "path": str(cover),
                    "asset_family": "audio_brief",
                    "sha256": openclaw_async_executor.hashlib.sha256(
                        cover.read_bytes()
                    ).hexdigest(),
                },
            ],
        }
    }
    if wire_format != "existing_package":
        payload = terminal["artifacts"][0]["value"]["result"]
        package = payload["acceptanceEvidence"].pop("telegram_user_facing_content_package")
        payload["acceptanceEvidence"]["source_fidelity"] = "verified"
        payload["metadata"] = {"user_facing_report": {
            "kind": "content_package", "delivery": "inline_with_attachment",
            "complete": True, "title": "Verified package",
            "observed_at": int(openclaw_async_executor.time.time()),
            "body": "\n\n".join(package[key] for key in [
                "facebook_page_body", "group_discussion_copy", "gemini_notebook_audio_prompt",
                "podcast_title", "podcast_description",
            ]),
            "assets": [{**a, "label": a["asset_family"]} for a in package["image_attachments"]],
        }}
        report = payload["metadata"]["user_facing_report"]
        if wire_format == "missing_report":
            payload["metadata"] = {"user_facing_report": {"kind": "content_package",
                "inline_content_package_field": "acceptanceEvidence.inline_content_package"}}
        elif wire_format == "wrong_hash":
            report["assets"][0]["sha256"] = "0" * 64
        elif wire_format == "wrong_filename":
            report["assets"][0]["filename"] = "another-case.png"
        elif wire_format == "missing_image":
            report["assets"].pop()
        elif wire_format == "object_body":
            report["body"] = {"reference": "acceptanceEvidence.inline_content_package"}
        elif wire_format == "reference_body":
            report["body"] = "acceptanceEvidence.inline_content_package"
        elif wire_format == "see_reference_body":
            report["body"] = "請參閱 `acceptanceEvidence.inline_content_package`。"
        elif wire_format == "prose_reference_body":
            report["body"] = "See acceptanceEvidence.inline_content_package for the complete package."
        elif wire_format == "metadata_reference_body":
            report["body"] = "The package is at metadata.user_facing_report.body"
        elif wire_format == "cjk_reference_body":
            report["body"] = "請參閱acceptanceEvidence.inline_content_package"
        elif wire_format == "cjk_metadata_reference_body":
            report["body"] = "請參閱metadata.user_facing_report內容"
        elif wire_format == "symlink_asset":
            target = kanban_home / "unrelated.txt"
            page.rename(target)
            page.symlink_to(target)
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "content-package-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    if wire_format not in {"existing_package", "canonical_report"}:
        assert handled["accepted"] is False
        assert "Required content package" in handled["reason"]
        with kb.connect() as conn:
            assert kb.get_task(conn, run.task_id).status == "blocked"
            assert kb.list_attachments(conn, run.task_id) == []
        return
    assert handled["accepted"] is True
    with kb.connect() as conn:
        completed_run = kb.latest_run(conn, started["execution_task_id"])
        attachments = kb.list_attachments(conn, started["execution_task_id"])
    assert completed_run is not None
    report = completed_run.metadata["user_facing_report"]
    assert report["kind"] == "content_package"
    assert "完整 Page 內文" in report["body"]
    assert {item.filename for item in attachments} == {
        f"{started['execution_task_id']}-content-package.md",
        page.name,
        cover.name,
    }
    with kb.connect() as conn:
        rebuilt = kb.grace_inline_content_package_report(conn, started["execution_task_id"])
    assert rebuilt["body"] == report["body"]
    assert {a["sha256"] for a in rebuilt["assets"]} == {a["sha256"] for a in report["assets"]}


def test_loop_contract_terminal_promotes_inline_text_content_package(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-inline-content-package-1"
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_only",
        "body_field": "final_user_facing_text",
    }
    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-inline-content-package-1",
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
    output["result"]["summary"] = "完整最終提案"
    output["result"]["acceptanceEvidence"] = {
        "final_user_facing_text": "完整、未截斷的繁體中文提案正文。",
    }

    handled = make_loop_contract_terminal_handler()(
        run,
        {
            "status": "succeeded",
            "delegated_result": terminal,
            "result_digest": "inline-content-package-digest",
        },
    )

    assert handled["accepted"] is True
    with kb.connect() as conn:
        completed_run = kb.latest_run(conn, started["execution_task_id"])
        attachments = kb.list_attachments(conn, started["execution_task_id"])
    assert completed_run is not None
    assert completed_run.metadata["user_facing_report"] == {
        "kind": "content_package",
        "delivery": "inline_only",
        "complete": True,
        "title": "完整最終提案",
        "body_field": "final_user_facing_text",
        "body": "完整、未截斷的繁體中文提案正文。",
        "observed_at": completed_run.metadata["user_facing_report"]["observed_at"],
        "assets": [],
    }
    assert attachments == []


@pytest.mark.parametrize(
    "malformed_body", [{"text": "proposal"}, ["proposal"], True, 1]
)
def test_inline_text_content_package_rejects_non_string_body(malformed_body):
    result = openclaw_async_executor._content_package_completion_metadata(
        {
            "status": "succeeded",
            "summary": "完整最終提案",
            "acceptanceEvidence": {
                "final_user_facing_text": malformed_body,
            },
        },
        metadata={
            "loop_contract": {
                "user_facing_delivery": {
                    "required": True,
                    "kind": "content_package",
                    "delivery": "inline_only",
                    "body_field": "final_user_facing_text",
                },
            },
        },
        task_id="t_inline_package",
        board=None,
    )

    assert result == {}


def test_loop_contract_terminal_defaults_missing_policy_receipts(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-terminal-no-policy-receipts"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-terminal-no-policy-receipts",
        transport=lambda task: _loop_result(task, "queued"),
    )

    def succeeded_without_policy_receipts(task):
        result = _loop_result(task, "succeeded")
        payload = result["artifacts"][0]["value"]["result"]
        payload.pop("policyReceipts")
        return result

    adapter = make_loop_contract_poll_adapter(
        transport=succeeded_without_policy_receipts
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
    handled = make_loop_contract_terminal_handler()(run, adapter(run))

    assert handled["accepted"] is True
    with kb.connect() as conn:
        completed_run = kb.latest_run(conn, started["execution_task_id"])
    assert completed_run is not None
    assert completed_run.metadata["policy_receipts"] == []


def test_loop_contract_terminal_persists_topic_policy_receipts(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-terminal-policy-1"
    namespace = contract["memory"]["namespace"]
    create_policy_version(
        "async-policy",
        "v1",
        "# Async policy\n",
        owner_scope="topic",
        owner_id=namespace,
        activate=True,
    )
    bind_topic_policies(
        namespace,
        [{"policy_id": "async-policy", "resolution": "latest_active"}],
    )
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-terminal-policy-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    adapter = make_loop_contract_poll_adapter(
        transport=lambda task: _loop_result(task, "succeeded")
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
    handled = make_loop_contract_terminal_handler()(run, adapter(run))

    assert handled["accepted"] is True
    with kb.connect() as conn:
        completed_run = kb.latest_run(conn, started["execution_task_id"])
    assert completed_run is not None
    assert completed_run.metadata["policy_receipts"] == [
        {
            "role": "execution",
            "policy_id": "async-policy",
            "version": "v1",
            "sha256": completed_run.metadata["loop_contract"]["policy_snapshots"][0][
                "sha256"
            ],
            "loaded": True,
        }
    ]


def test_loop_contract_terminal_defaults_policy_receipts_with_internal_image_receipts(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-terminal-policy-image-1"
    namespace = contract["memory"]["namespace"]
    create_policy_version(
        "async-image-policy",
        "v1",
        "# Async image policy\n",
        owner_scope="topic",
        owner_id=namespace,
        activate=True,
    )
    bind_topic_policies(
        namespace,
        [{"policy_id": "async-image-policy", "resolution": "latest_active"}],
    )
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-terminal-policy-image-1",
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
    payload = terminal["artifacts"][0]["value"]["result"]
    payload.pop("policyReceipts")
    payload["externalEffects"] = [
        {
            "target": "openclaw.image_generate:hero",
            "state": "verified",
            "externalId": "session=image_generate:hero",
            "readback": {
                "path": "/Users/kj/.openclaw/media/tool-image-generation/hero.png",
                "model": "openai/gpt-image-2",
            },
        },
    ]

    handled = make_loop_contract_terminal_handler()(
        run,
        {
            "status": "succeeded",
            "delegated_result": terminal,
            "result_digest": "terminal-policy-image-digest",
        },
    )

    assert handled["accepted"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        completed_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "done"
    assert completed_run is not None
    assert completed_run.metadata["external_effects"] == []
    assert len(completed_run.metadata["internal_tool_receipts"]) == 1
    assert completed_run.metadata["policy_receipts"] == [
        {
            "role": "execution",
            "policy_id": "async-image-policy",
            "version": "v1",
            "sha256": completed_run.metadata["loop_contract"]["policy_snapshots"][0][
                "sha256"
            ],
            "loaded": True,
        }
    ]


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
    output["resultText"] = (
        "Complete.\n```json\n"
        + json.dumps(result, ensure_ascii=False)
        + "\n```\nNo further actions were performed."
    )
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
            metadata={"policy_receipts": []},
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
            metadata={"policy_receipts": []},
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


def test_loop_contract_from_execution_body_keeps_embedded_policy_fences():
    from proactive.grace_task_compiler import render_execution_body
    from proactive.openclaw_async_executor import _loop_contract_from_execution_body

    contract = _contract()
    contract["policy_snapshots"] = [
        {
            "policy_id": "ai-bizweek",
            "version": "1",
            "sha256": "abc123",
            "content": "Policy text with an embedded fenced block:\n```json\n{\"ok\": true}\n```",
        }
    ]

    parsed = _loop_contract_from_execution_body(render_execution_body(contract))

    assert parsed["policy_snapshots"][0]["content"] == contract["policy_snapshots"][0]["content"]


def test_quarantined_loop_contract_correction_is_recoverable(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-correction-quarantine-1"
    contract["routing"] = {
        "resolved": {
            "assignment": {
                "allowed_tools": ["image_generate"],
                "assigned_worker": "missioncrew.content",
                "required_agent_id": "missioncrew-content",
            }
        }
    }
    started = start_loop_contract_execution(
        contract=contract,
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-correction-quarantine-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
        assert kb.complete_task(
            conn,
            started["execution_task_id"],
            result="needs correction",
            metadata={"policy_receipts": []},
            expected_run_id=run.id,
        )
        conn.execute(
            """
            INSERT INTO task_events (task_id, kind, payload, created_at)
            VALUES (?, 'grace_correction_requested', ?, 1)
            """,
            (
                started["execution_task_id"],
                '{"reason":"missing visual hierarchy"}',
            ),
        )
        conn.execute(
            "UPDATE tasks SET status = 'blocked', completed_at = NULL WHERE id = ?",
            (started["execution_task_id"],),
        )
        conn.execute(
            """
            INSERT INTO task_events (task_id, kind, payload, created_at)
            VALUES (?, 'blocked', ?, 2)
            """,
            (
                started["execution_task_id"],
                '{"reason":"OpenClaw correction admission quarantined: Unterminated string","kind":"capability"}',
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
    assert retried["status"] == "queued"
    assert seen["loop_contract"]["verification"]["review_feedback"] == [
        "missing visual hierarchy"
    ]
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        run = kb.get_run(conn, int(retried["run_id"]))
    assert task is not None and task.status == "running"
    assert run is not None
    assert run.metadata["correction_admission"] is True
    assert run.metadata["allowed_tools"] == [
        "read",
        "write",
        "web_search",
        "image_generate",
    ]


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
        policy_receipts = [
            {
                "role": "execution",
                "policy_id": item["policy_id"],
                "version": item["version"],
                "sha256": item["sha256"],
                "loaded": True,
            }
            for item in (run.metadata.get("loop_contract") or {}).get(
                "policy_snapshots", []
            )
        ]
        assert kb.complete_task(
            conn,
            started["execution_task_id"],
            result="evidence incomplete",
            metadata={"policy_receipts": policy_receipts},
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
    assert len(handled["reason"]) <= 2000
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"


def test_loop_contract_terminal_materializes_openclaw_external_effects(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-openclaw-effect-format-1"
    contract["external_targets"] = [
        "facebook marketplace listing 37276725125275496 live page",
        "group:897927458651235",
    ]
    started = start_loop_contract_execution(
        contract=contract,
        task_type="browser_publish",
        risk_level="high",
        approved=True,
        delegation_id="delegation-loop-openclaw-effect-format-1",
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
    terminal["status"] = "failed"
    terminal["errors"] = ["openclaw_bridge_failed"]
    terminal["requires_human_review"] = True
    output = terminal["artifacts"][0]["value"]
    output["evidence"]["externalEffectBudget"] = 2
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Loop Contract external effect evidence is incomplete or outside "
        "the approved targets."
    )
    output["result"]["externalEffects"] = [
        {
            "target": "facebook marketplace listing 37276725125275496",
            "deterministicEffectKey": (
                "facebook-marketplace-renew-existing-listing-"
                "37276725125275496-2026-08-30"
            ),
            "state": "verified",
            "externalId": "37276725125275496",
            "readback": "listing-bound Renew control was clicked",
        },
        {
            "target": "group:897927458651235",
            "deterministicEffectKey": (
                "facebook-marketplace-list-more-places-37276725125275496-"
                "group-897927458651235-2026-08-30"
            ),
            "state": "verified",
            "externalId": "897927458651235",
            "readback": "Post submitted and chooser no longer listed the group",
        },
    ]

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "failed",
        "delegated_result": terminal,
        "result_digest": "terminal-openclaw-effect-format-digest",
    })

    assert handled["accepted"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
        effects = kb.list_external_effects(conn, started["execution_task_id"])
    assert task is not None and task.status == "done"
    assert ended_run is not None
    assert ended_run.metadata["external_effects"] == [
        {
            "platform": "facebook",
            "effect_key": "marketplace:37276725125275496",
            "state": "verified",
            "external_id": "37276725125275496",
            "details": {
                "target": "facebook marketplace listing 37276725125275496",
                "readback": "listing-bound Renew control was clicked",
                "deterministicEffectKey": (
                    "facebook-marketplace-renew-existing-listing-"
                    "37276725125275496-2026-08-30"
                ),
            },
        },
        {
            "platform": "facebook",
            "effect_key": "group:897927458651235",
            "state": "verified",
            "external_id": "897927458651235",
            "details": {
                "target": "group:897927458651235",
                "readback": "Post submitted and chooser no longer listed the group",
                "deterministicEffectKey": (
                    "facebook-marketplace-list-more-places-37276725125275496-"
                    "group-897927458651235-2026-08-30"
                ),
            },
        },
    ]
    assert [
        (effect["platform"], effect["effect_key"], effect["state"], effect["external_id"])
        for effect in effects
    ] == [
        ("facebook", "group:897927458651235", "verified", "897927458651235"),
        (
            "facebook",
            "marketplace:37276725125275496",
            "verified",
            "37276725125275496",
        ),
    ]


def test_loop_contract_terminal_accepts_canonical_group_url_effect_target(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-openclaw-effect-url-target-1"
    contract["external_targets"] = [
        "facebook marketplace listing 37276725125275496",
        "https://www.facebook.com/groups/897927458651235",
    ]
    contract["facebook_group_publish"] = {
        "mode": "canonical_url_per_group",
        "source_listing_id": "37276725125275496",
        "destinations": [
            {
                "group_id": "897927458651235",
                "canonical_name": "二手家具 家電 買賣",
                "canonical_url": "https://www.facebook.com/groups/897927458651235",
            }
        ],
    }
    started = start_loop_contract_execution(
        contract=contract,
        task_type="browser_publish",
        risk_level="high",
        approved=True,
        delegation_id="delegation-loop-openclaw-effect-url-target-1",
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
    terminal["status"] = "failed"
    terminal["errors"] = ["openclaw_bridge_failed"]
    terminal["requires_human_review"] = True
    output = terminal["artifacts"][0]["value"]
    output["evidence"]["externalEffectBudget"] = 2
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Loop Contract external effect evidence is incomplete or outside "
        "the approved targets."
    )
    output["result"]["externalEffects"] = [
        {
            "target": "https://www.facebook.com/groups/897927458651235",
            "state": "verified",
            "externalId": "897927458651235",
            "readback": "Canonical group URL matched numeric id and group name before Post.",
        },
    ]

    handled = make_loop_contract_terminal_handler()(
        run,
        {
            "status": "failed",
            "delegated_result": terminal,
            "result_digest": "terminal-openclaw-effect-url-target-digest",
        },
    )

    assert handled["accepted"] is True
    with kb.connect() as conn:
        effects = kb.list_external_effects(conn, started["execution_task_id"])
    assert [
        (effect["platform"], effect["effect_key"], effect["state"], effect["external_id"])
        for effect in effects
    ] == [("facebook", "group:897927458651235", "verified", "897927458651235")]


def test_loop_contract_terminal_downgrades_uncertain_effect_readback(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-openclaw-effect-unknown-1"
    contract["external_targets"] = [
        "facebook marketplace listing 37276725125275496 live page",
        "group:1333742673375089",
    ]
    started = start_loop_contract_execution(
        contract=contract,
        task_type="browser_publish",
        risk_level="high",
        approved=True,
        delegation_id="delegation-loop-openclaw-effect-unknown-1",
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
    terminal["status"] = "failed"
    terminal["errors"] = ["openclaw_bridge_failed"]
    terminal["requires_human_review"] = True
    output = terminal["artifacts"][0]["value"]
    output["evidence"]["externalEffectBudget"] = 2
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Loop Contract external effect evidence is incomplete or outside "
        "the approved targets."
    )
    output["result"]["externalEffects"] = [
        {
            "target": "group:1333742673375089",
            "deterministicEffectKey": (
                "facebook-marketplace-list-more-places-37276725125275496-"
                "group-1333742673375089-2026-08-31"
            ),
            "state": "verified",
            "externalId": "1333742673375089",
            "readback": (
                "Selected exact chooser checkbox and clicked enabled Post once. "
                "Subsequent destination readback did not expose a matching group "
                "post, so outcome is recorded as unknown."
            ),
        },
    ]

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "failed",
        "delegated_result": terminal,
        "result_digest": "terminal-openclaw-effect-unknown-digest",
    })

    assert handled["accepted"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
        effects = kb.list_external_effects(conn, started["execution_task_id"])
    assert task is not None and task.status == "done"
    assert ended_run is not None
    assert ended_run.metadata["external_effects"][0]["state"] == "unknown"
    assert [
        (effect["platform"], effect["effect_key"], effect["state"], effect["external_id"])
        for effect in effects
    ] == [("facebook", "group:1333742673375089", "unknown", "1333742673375089")]


def test_loop_contract_terminal_rejects_openclaw_external_effect_outside_allowlist(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-openclaw-effect-format-2"
    contract["external_targets"] = ["group:897927458651235"]
    started = start_loop_contract_execution(
        contract=contract,
        task_type="browser_publish",
        risk_level="high",
        approved=True,
        delegation_id="delegation-loop-openclaw-effect-format-2",
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
    terminal["status"] = "failed"
    terminal["errors"] = ["openclaw_bridge_failed"]
    terminal["requires_human_review"] = True
    output = terminal["artifacts"][0]["value"]
    output["evidence"]["externalEffectBudget"] = 1
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Loop Contract external effect evidence is incomplete or outside "
        "the approved targets."
    )
    output["result"]["externalEffects"] = [
        {
            "target": "group:1333742673375089",
            "state": "verified",
            "externalId": "1333742673375089",
            "readback": "out of scope",
        },
    ]

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "failed",
        "delegated_result": terminal,
        "result_digest": "terminal-openclaw-effect-format-rejected",
    })

    assert handled["accepted"] is False
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        effects = kb.list_external_effects(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"
    assert effects == []


def test_loop_contract_terminal_synthesizes_commerce_status_report(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-commerce-status-report-1"
    contract["routing"] = {"task_type": "secondhand_commerce_group_status"}
    contract["user_facing_delivery"] = {
        "required": True,
        "kind": "commerce_group_status",
        "delivery": "inline_only",
        "subject_keys": ["kolin-kd-291m06:37276725125275496:1333742673375089"],
    }
    started = start_loop_contract_execution(
        contract=contract,
        task_type="secondhand_commerce_group_status",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-commerce-status-report-1",
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
    output["result"]["acceptanceEvidence"] = {
        "inlineReport": {
            "listing_id": "37276725125275496",
            "group_numeric_id": "1333742673375089",
            "group_name": "(北市新北) 冷氣 家電 家具 五金 雜貨全新中古買賣",
            "status": "not_posted",
            "observed_at": 1_788_094_528,
            "evidence_url": "https://www.facebook.com/marketplace/you/selling",
            "evidence": (
                "The listing-bound chooser shows the target group as an "
                "available destination; no checkbox was selected."
            ),
        }
    }

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-commerce-status-report-digest",
    })

    assert handled["accepted"] is True
    with kb.connect() as conn:
        ended_run = kb.latest_run(conn, started["execution_task_id"])
        coverage = kb.list_commerce_group_coverage(
            conn,
            subject_key="kolin-kd-291m06:37276725125275496:1333742673375089",
        )
    assert ended_run is not None
    report = ended_run.metadata["user_facing_report"]
    assert report["kind"] == "commerce_group_status"
    assert report["complete"] is True
    assert report["coverage"][0]["expected_total"] == 1
    assert report["coverage"][0]["named_count"] == 1
    assert report["coverage"][0]["gap_count"] == 0
    assert report["rows"][0]["status"] == "not_posted"
    assert len(coverage) == 1
    assert coverage[0]["complete"] == 1


def test_zero_budget_terminal_accepts_internal_image_generation_receipts(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-internal-image-effect-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-internal-image-effect-1",
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
    terminal["artifacts"][0]["value"]["result"]["externalEffects"] = [
        {
            "target": "openclaw.image_generate",
            "state": "verified",
            "externalId": "session=image_generate:hero",
            "readback": {
                "path": "/Users/kj/.openclaw/media/tool-image-generation/hero.png",
                "model": "openai/gpt-image-2",
            },
        },
        {
            "target": "openclaw.image_generate",
            "state": "verified",
            "externalId": "session=image_generate:cover",
            "readback": {
                "path": "/Users/kj/.openclaw/media/tool-image-generation/cover.png",
                "model": "openai/gpt-image-2",
            },
        },
    ]
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-internal-image-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "done"
    assert ended_run is not None
    assert ended_run.metadata["external_effects"] == []
    assert len(ended_run.metadata["internal_tool_receipts"]) == 2


def test_zero_budget_terminal_reclassifies_media_generation_budget_error(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-media-generation-effect-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-media-generation-effect-1",
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
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Loop Contract result exceeded its external effect budget."
    )
    output["result"]["externalEffects"] = [
        {
            "target": "media_generation_page_hero",
            "state": "verified",
            "externalId": (
                "media:/Users/kj/.openclaw/media/tool-image-generation/"
                "ep04_page_hero.png"
            ),
            "readback": {
                "path": "/Users/kj/.openclaw/media/tool-image-generation/ep04_page_hero.png",
                "asset_family": "page_hero",
            },
        },
        {
            "target": "media_generation_audio_brief",
            "state": "verified",
            "externalId": (
                "media:/Users/kj/.openclaw/media/tool-image-generation/"
                "ep04_audio_brief.png"
            ),
            "readback": {
                "path": "/Users/kj/.openclaw/media/tool-image-generation/ep04_audio_brief.png",
                "asset_family": "audio_brief",
            },
        },
    ]

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-media-generation-digest",
    })

    assert handled["accepted"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "done"
    assert ended_run is not None
    assert ended_run.metadata["external_effects"] == []
    assert len(ended_run.metadata["internal_tool_receipts"]) == 2


@pytest.mark.parametrize(
    "effect_target",
    [
        "local openclaw.image_generate",
        "openclaw.image_generate.local_media",
        "openclaw.image_generate local managed media",
    ],
)
def test_zero_budget_terminal_reclassifies_local_openclaw_image_generation(
    kanban_home,
    effect_target,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-local-openclaw-image-effect-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-local-openclaw-image-effect-1",
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
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Loop Contract result exceeded its external effect budget."
    )
    output["result"]["externalEffects"] = [
        {
            "target": effect_target,
            "state": "verified",
            "readback": {
                "path": (
                    "/Users/kj/.openclaw/media/tool-image-generation/"
                    "topic4641_carters_page_hero_16x9.png"
                ),
                "asset_family": "page_hero",
            },
        },
    ]

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-local-openclaw-image-digest",
    })

    assert handled["accepted"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "done"
    assert ended_run is not None
    assert ended_run.metadata["external_effects"] == []
    assert len(ended_run.metadata["internal_tool_receipts"]) == 1


def test_zero_budget_terminal_keeps_telegram_delivery_as_external_effect(
    kanban_home,
):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-telegram-delivery-effect-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-telegram-delivery-effect-1",
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
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Loop Contract result exceeded its external effect budget."
    )
    output["result"]["externalEffects"] = [
        {
            "target": "telegram_inline_delivery",
            "state": "verified",
            "externalId": "tgtrace_b131ff86a5b0c85adb56cfa9c42691c8",
            "readback": {"external_platform_action": False},
        },
    ]

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-telegram-delivery-digest",
    })

    assert handled["accepted"] is False
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"


def test_internal_image_effect_requires_verified_state():
    assert openclaw_async_executor._is_internal_image_generation_effect({
        "target": "openclaw.image_generate local managed media",
        "state": "running",
        "readback": {
            "path": "/Users/kj/.openclaw/media/tool-image-generation/pending.png",
        },
    }) is False


def test_loop_terminal_rejects_failed_acceptance_evidence(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-failed-acceptance-evidence-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-failed-acceptance-evidence-1",
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
    terminal["artifacts"][0]["value"]["result"]["acceptanceEvidence"] = [
        {"check": "content source", "result": "failed"},
    ]

    handled = make_loop_contract_terminal_handler()(run, {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-failed-evidence-digest",
    })

    assert handled["accepted"] is False
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"


@pytest.mark.parametrize("ledger_state", [None, "created", "verified"])
def test_loop_terminal_preserves_specific_backend_blocker(kanban_home, ledger_state):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-specific-blocker-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="research",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-specific-blocker-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
    run.metadata["loop_contract"]["domain_memory"] = {
        "schema_id": "solobizai.case.v1", "domain_key": "solobizai",
        "entity_type": "SoloBizAiCase", "mode": "mutate",
        "require_delta_on_acceptance": True,
    }
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
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = (
        "Required Facebook Graph API tool is unavailable."
    )
    output["result"] = {
        "status": "blocked",
        "summary": "Blocked before any Facebook Graph POST.",
        "acceptanceEvidence": {},
        "externalEffects": [],
        "unvalidatedWorkerResult": {"externalEffects": [{"target": {"page_url": "bad-shape"}}],
                                    "domainMemoryDeltas": [{"op": "upsert_entity"}]},
    }
    if ledger_state:
        with kb.connect() as conn:
            kb.record_external_effect(conn, run.task_id, platform="facebook", state=ledger_state,
                                      external_id="existing-post", expected_run_id=run.id)
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-specific-blocker-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is False
    assert "Domain memory:" not in handled["reason"]
    assert "Required Facebook Graph API tool is unavailable" in handled["reason"]
    assert ("Read-only contract reported zero external effects as expected" in handled["reason"]) is (ledger_state is None)
    assert "No external effect was verified or recorded" not in handled["reason"]
    if ledger_state:
        assert "do not repeat the external action" in handled["reason"]
        assert f"state={ledger_state}" in handled["reason"]
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"
    assert ended_run is not None
    assert ended_run.metadata["unvalidated_worker_result"] == output["result"]["unvalidatedWorkerResult"]
    assert len(ended_run.metadata["durable_external_effects"]) == (1 if ledger_state else 0)
    assert "Blocked before any Facebook Graph POST" in ended_run.summary
    assert ended_run.metadata["loop_contract_blocked_result"]["status"] == "blocked"
    assert ended_run.metadata["read_only_zero_external_effects"] is (ledger_state is None)


def test_loop_terminal_synthesizes_readonly_empty_result_json_blocker(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-empty-result-json-blocker-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="secondhand_commerce_group_status",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-empty-result-json-blocker-1",
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
    output.pop("result")
    output["resultText"] = ""
    output["evidence"]["resultContractValid"] = False
    output["evidence"]["resultContractError"] = "Loop Contract result is not valid JSON."
    terminal["status"] = "failed"
    terminal["errors"] = ["openclaw_bridge_failed"]
    observation = {
        "status": "failed",
        "delegated_result": terminal,
        "result_digest": "terminal-empty-result-json-blocker-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is False
    assert "Loop Contract result is not valid JSON" in handled["reason"]
    assert "OpenClaw returned no structured result JSON" in handled["reason"]
    assert "Read-only contract reported zero external effects as expected" in handled["reason"]
    assert "No external effect was verified or recorded" not in handled["reason"]
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"
    assert ended_run is not None
    assert ended_run.metadata["loop_contract_blocked_result"]["status"] == "blocked"
    assert (
        ended_run.metadata["required_evidence"]["structuredResultJson"] is None
    )
    assert ended_run.metadata["external_effects"] == []
    assert ended_run.metadata["read_only_zero_external_effects"] is True


def test_loop_terminal_synthesizes_readonly_timeout_cleanup_blocker(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-timeout-cleanup-blocker-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="secondhand_commerce_group_status",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-timeout-cleanup-blocker-1",
        transport=lambda task: _loop_result(task, "queued"),
    )
    with kb.connect() as conn:
        run = kb.get_run(conn, int(started["run_id"]))
        assert run is not None
        kb.merge_active_run_metadata(
            conn,
            run.task_id,
            expected_run_id=run.id,
            metadata={
                "stop_rule_cleanup_pending": True,
                "stop_rule_reason": (
                    "Loop Contract max_runtime_seconds reached before backend "
                    "terminal state: 1200s."
                ),
            },
        )
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
    output.pop("result")
    output.pop("resultText", None)
    output["evidence"].pop("resultContractValid", None)
    output["evidence"]["sessionCleaned"] = True
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-timeout-cleanup-blocker-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is False
    assert "max_runtime_seconds" in handled["reason"]
    assert "OpenClaw reached its runtime limit" in handled["reason"]
    assert "Read-only contract reported zero external effects as expected" in handled["reason"]
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"
    assert ended_run is not None
    assert ended_run.metadata["loop_contract_blocked_result"]["blocker"] == {
        "kind": "runtime_output_contract",
        "reason": "runtime_timeout_cleanup",
    }
    assert ended_run.metadata["required_evidence"]["structuredResultJson"] is None
    assert ended_run.metadata["external_effects"] == []


def test_loop_terminal_preserves_readonly_share_link_blocker_details(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-share-link-blocker-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="secondhand_commerce_group_status",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-share-link-blocker-1",
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
    output["result"] = {
        "status": "blocked",
        "summary": "blocked",
        "acceptanceEvidence": {
            "checks": [
                {
                    "name": "redirect inspection",
                    "result": "blocked",
                    "notes": "未取得最終導向 URL",
                },
                {
                    "name": "group ID/name binding",
                    "result": "not_available",
                    "notes": "無頁面可讀取，無法比對 1333742673375089",
                },
            ],
        },
        "requiredEvidence": {
            "resolvedUrl": None,
            "canonicalUrl": None,
            "groupId": None,
            "listingIdentity": None,
        },
        "externalEffects": [],
    }
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-share-link-blocker-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is False
    assert "redirect inspection: blocked" in handled["reason"]
    assert "Missing required evidence: resolvedUrl, canonicalUrl, groupId" in handled["reason"]
    assert "Read-only contract reported zero external effects as expected" in handled["reason"]
    assert "No external effect was verified or recorded" not in handled["reason"]
    with kb.connect() as conn:
        task = kb.get_task(conn, started["execution_task_id"])
        ended_run = kb.latest_run(conn, started["execution_task_id"])
    assert task is not None and task.status == "blocked"
    assert ended_run is not None
    assert ended_run.metadata["required_evidence"]["resolvedUrl"] is None
    assert (
        ended_run.metadata["acceptance_evidence"]["checks"][0]["notes"]
        == "未取得最終導向 URL"
    )
    assert ended_run.metadata["read_only_zero_external_effects"] is True


def test_loop_terminal_rejects_succeeded_payload_with_not_verified_evidence(kanban_home):
    contract = _contract()
    contract["identity"]["request_instance_id"] = "loop-not-verified-evidence-1"
    started = start_loop_contract_execution(
        contract=contract,
        task_type="browser_readonly",
        risk_level="low",
        approved=False,
        delegation_id="delegation-loop-not-verified-evidence-1",
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
    output["result"] = {
        "status": "succeeded",
        "summary": "未完成驗證。",
        "acceptanceEvidence": [
            {
                "criterion": "可否解析/重導 URL 並讀回最終頁面身份",
                "outcome": "blocked",
                "result": "not_verified",
                "notes": "未取得最終導向 URL",
            }
        ],
        "externalEffects": [],
    }
    observation = {
        "status": "succeeded",
        "delegated_result": terminal,
        "result_digest": "terminal-not-verified-evidence-digest",
    }

    handled = make_loop_contract_terminal_handler()(run, observation)

    assert handled["accepted"] is False
    assert "可否解析/重導 URL 並讀回最終頁面身份: not_verified" in handled["reason"]
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
        assert review is not None and review.status == "ready"
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

    assert result["status"] == "review_pending"
    with kb.connect() as conn:
        execution = kb.get_task(conn, result["execution_task_id"])
        review = kb.get_task(conn, result["review_task_id"])
        assert execution is not None and execution.status == "done"
        assert review is not None and review.status == "ready"


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

    assert replayed["status"] == "review_pending"
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
