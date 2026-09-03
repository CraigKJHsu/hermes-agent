from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from proactive.grace_task_compiler import (
    compile_and_delegate,
    contract_internal_hermes_runtime,
    contract_requires_image_generation,
    render_execution_body,
)
from proactive.hubops_routing import resolved_route_binding, route_clawops_objective
from proactive.loop_contract import contract_fingerprint, validate_loop_contract
from proactive.policy_registry import create_policy_version


def _activate_model_routing_policy() -> None:
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


def _image_contract() -> dict:
    return {
        "identity": {
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "4641",
            "topic_name": "Topic 4641",
            "project": "telegram_1003938559457_4641_bff429b6e587",
            "board": "default",
            "request_instance_id": "image-contract-1",
            "requested_by": "authenticated_user",
            "compiled_by": "Grace",
        },
        "original_request": "Generate the Tiendas CUADRA Hero image.",
        "grace_interpretation": "Create an internal 16:9 Hero image artifact.",
        "trigger": "user_request",
        "goal": {
            "objective": "Generate and verify a 16:9 Tiendas CUADRA Hero image.",
            "deliverables": [
                "Generated image file path",
                "Visual inspection evidence",
                "SHA-256",
            ],
            "non_goals": ["Publish, upload, or schedule any external content"],
        },
        "scope": {
            "allowed": ["Internal image artifact generation"],
            "forbidden": ["External publishing", "Facebook changes"],
        },
        "verification": {
            "checks": ["Inspect generated image", "Compute SHA-256"],
            "evidence_required": ["absolute path", "sha256"],
            "acceptance_criteria": ["16:9 image exists and is visually checked"],
        },
        "stop_rules": {
            "success": ["All deliverables are produced"],
            "blocked": ["Image provider or file verification is unavailable"],
            "no_progress": ["No new artifact after retry"],
            "max_iterations": 3,
            "max_runtime_seconds": 600,
        },
        "memory": {
            "namespace": "telegram:-1003938559457:4641/topic",
            "working": ["Use Topic 4641 instructions"],
            "promote_on_acceptance": ["Accepted Hero image artifact metadata"],
        },
        "routing": {
            "task_type": "content_draft",
            "risk_level": "low",
        },
        "completion_mode": "terminal",
    }


def _route_image_contract(contract: dict) -> tuple[dict, str]:
    preliminary = validate_loop_contract(contract)
    preview = route_clawops_objective(
        contract["goal"]["objective"],
        project=contract["identity"]["project"],
        task_type="content_draft",
        risk_level="low",
        approved=True,
        contract_fingerprint=contract_fingerprint(preliminary),
    )
    contract["routing"]["resolved"] = resolved_route_binding(preview)
    normalized = validate_loop_contract(contract)
    return normalized, contract_fingerprint(normalized)


def test_approved_execution_body_hides_grace_callback_envelope() -> None:
    contract = _image_contract()
    contract["original_request"] = (
        "[SYSTEM: Grace Loop callback]\n"
        "source of truth: callback delivery envelope, not worker source material"
    )
    contract["approval_provenance"] = {
        "approval_grant_id": "gd_test",
        "approval_token": "token-test",
        "approved_message_id": "message-2",
    }
    contract["scope"]["allowed"] = [
        "Use the already-approved exact external action only"
    ]
    contract["goal"]["objective"] = (
        "Use source facts from the approved contract, but do not reinterpret callback text."
    )

    body = render_execution_body(contract)

    assert "already backed by a consumed, one-time owner approval" in body
    assert "Do not create another approval checkpoint" in body
    assert "clawops_delegate, grace_callback_outcome" in body
    assert "[SYSTEM: Grace Loop callback]" not in body
    assert '"original_request_sha256"' in body
    assert "Grace session history only; not disclosed to ClawOps" in body


def _openclaw_loop_result(task: dict, backend_agent_id: str = "missioncrew-content") -> dict:
    return {
        "task_id": task["task_id"],
        "status": "queued",
        "summary": "OpenClaw loop contract queued.",
        "artifacts": [],
        "tool_calls": [{"name": "openclaw_bridge_http"}],
        "audit_log": ["accepted"],
        "errors": [],
        "requires_human_review": False,
        "recommended_next_action": "Poll.",
        "protocol_version": "2.0",
        "protocol_correlated": True,
        "delegation_id": task["delegation_id"],
        "attempt_id": task["attempt_id"],
        "contract_fingerprint": task["contract_fingerprint"],
        "identity_correlated": True,
        "backend_run_id": "openclaw-loop-run-image-1",
        "backend_agent_id": backend_agent_id,
        "backend_session_key": f"agent:{backend_agent_id}:subagent:test-loop",
    }


def test_image_generation_loop_contract_routes_to_openclaw_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()
    _activate_model_routing_policy()
    from proactive import openclaw_async_executor

    monkeypatch.setattr(
        openclaw_async_executor,
        "delegate_loop_contract_to_openclaw",
        lambda args, **_kw: _openclaw_loop_result(args),
    )

    contract = _image_contract()
    _normalized, fingerprint = _route_image_contract(contract)
    owner_hash = hashlib.sha256(b"kj").hexdigest()
    session_key = "agent:main:telegram:group:chat-1:4641"
    session_id = "session-1"

    with kb.connect() as conn:
        challenge = kb.create_grace_approval_challenge(
            conn,
            contract_fingerprint=fingerprint,
            request_instance_id=contract["identity"]["request_instance_id"],
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key=session_key,
            session_id=session_id,
            user_id_sha256=owner_hash,
            requested_message_id="message-1",
            action_summary=contract["goal"]["objective"],
            approval_platform="Internal image artifact",
            approval_scope=json.dumps(contract["scope"]["allowed"]),
        )
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint=fingerprint,
            request_instance_id=contract["identity"]["request_instance_id"],
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key=session_key,
            session_id=session_id,
            resolved_route=contract["routing"]["resolved"],
            approval_required=True,
            challenge_token=challenge["token"],
            user_id_sha256=owner_hash,
            approved_message_id="message-2",
        )
        assert kb.claim_grace_delegation_build(
            conn,
            delegation_id=delegation["delegation_id"],
            build_owner="builder-1",
        )

    result = compile_and_delegate(
        contract,
        context={
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "4641",
            "topic_name": "Topic 4641",
            "project": "telegram_1003938559457_4641_bff429b6e587",
            "memory_namespace": "telegram:-1003938559457:4641/topic",
        },
        task_type="content_draft",
        risk_level="low",
        approved=True,
        delegation_id=delegation["delegation_id"],
        delegation_build_owner="builder-1",
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        user_id="kj",
        session_key=session_key,
        session_id=session_id,
        message_id="message-2",
        notifier_profile="default",
    )

    with kb.connect() as conn:
        execution = kb.get_task(conn, result.execution_task_id)
        review = kb.get_task(conn, result.review_task_id)
        callbacks = conn.execute(
            """
            SELECT *
              FROM grace_loop_callbacks
             WHERE execution_task_id = ? AND review_task_id = ?
            """,
            (result.execution_task_id, result.review_task_id),
        ).fetchall()

    run = None
    if execution is not None and execution.current_run_id is not None:
        with kb.connect() as conn:
            run = kb.get_run(conn, int(execution.current_run_id))

    assert result.assignee == "openclaw"
    assert execution is not None
    assert execution.executor_backend == "openclaw"
    assert execution.executor_profile == "loop-contract"
    assert "image_generate" in execution.body
    assert "Do not call this a content blocker solely because the status is running" in execution.body
    assert run is not None
    assert run.metadata["backend_agent_id"] == "missioncrew-content"
    assert run.metadata["task_type"] == "content_draft"
    assert "image_generate" in run.metadata["allowed_tools"]
    assert review is not None
    assert review.executor_profile == "grace-policy-review"
    assert len(callbacks) == 1


def test_internal_only_image_generation_contract_uses_content_runtime_without_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()
    _activate_model_routing_policy()
    from proactive import openclaw_async_executor

    monkeypatch.setattr(
        openclaw_async_executor,
        "delegate_loop_contract_to_openclaw",
        lambda args, **_kw: _openclaw_loop_result(args),
    )

    contract = _image_contract()
    contract["identity"]["request_instance_id"] = "image-contract-internal-1"
    contract["external_targets"] = [
        "Internal Topic 4641 image artifact only - no external platform action"
    ]
    _normalized, fingerprint = _route_image_contract(contract)
    session_key = "agent:main:telegram:group:chat-1:4641"
    session_id = "session-1"

    with kb.connect() as conn:
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint=fingerprint,
            request_instance_id=contract["identity"]["request_instance_id"],
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key=session_key,
            session_id=session_id,
            resolved_route=contract["routing"]["resolved"],
            approval_required=False,
        )
        assert kb.claim_grace_delegation_build(
            conn,
            delegation_id=delegation["delegation_id"],
            build_owner="builder-1",
        )

    result = compile_and_delegate(
        contract,
        context={
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "4641",
            "topic_name": "Topic 4641",
            "project": "telegram_1003938559457_4641_bff429b6e587",
            "memory_namespace": "telegram:-1003938559457:4641/topic",
        },
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id=delegation["delegation_id"],
        delegation_build_owner="builder-1",
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        session_key=session_key,
        session_id=session_id,
        message_id="message-2",
        notifier_profile="default",
    )

    with kb.connect() as conn:
        execution = kb.get_task(conn, result.execution_task_id)
        stored = kb.get_grace_delegation(
            conn,
            delegation_id=delegation["delegation_id"],
        )

    run = None
    if execution is not None and execution.current_run_id is not None:
        with kb.connect() as conn:
            run = kb.get_run(conn, int(execution.current_run_id))

    assert result.assignee == "openclaw"
    assert execution is not None
    assert execution.executor_backend == "openclaw"
    assert execution.executor_profile == "loop-contract"
    assert run is not None
    assert run.metadata["backend_agent_id"] == "missioncrew-content"
    assert run.metadata["external_effect_budget"] == 0
    assert stored["approval_required"] == 0


def test_source_bound_content_contract_exposes_original_request_to_openclaw(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()
    _activate_model_routing_policy()
    from proactive import openclaw_async_executor

    seen: dict[str, dict] = {}

    def fake_delegate(args, **_kw):
        seen["args"] = args
        return _openclaw_loop_result(args)

    monkeypatch.setattr(
        openclaw_async_executor,
        "delegate_loop_contract_to_openclaw",
        fake_delegate,
    )

    contract = _image_contract()
    contract["identity"]["request_instance_id"] = "source-bound-content-1"
    contract["original_request"] = (
        "SOURCE MATERIAL:\n"
        "Carter’s Junk Away is a Colorado junk removal service. "
        "Peak season reaches about US$15K/month."
    )
    contract["grace_interpretation"] = (
        "Use the original_request embedded SOURCE MATERIAL as the only source."
    )
    contract["scope"]["allowed"] = [
        "只使用original_request內嵌SOURCE MATERIAL",
        "OpenClaw loop-contract missioncrew-content",
    ]
    contract["verification"]["checks"].append(
        "Confirm original_request is embedded for the backend worker"
    )
    _normalized, fingerprint = _route_image_contract(contract)
    session_key = "agent:main:telegram:group:chat-1:4641"
    session_id = "session-source-1"

    with kb.connect() as conn:
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint=fingerprint,
            request_instance_id=contract["identity"]["request_instance_id"],
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key=session_key,
            session_id=session_id,
            resolved_route=contract["routing"]["resolved"],
            approval_required=False,
        )
        assert kb.claim_grace_delegation_build(
            conn,
            delegation_id=delegation["delegation_id"],
            build_owner="builder-source-1",
        )

    result = compile_and_delegate(
        contract,
        context={
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "4641",
            "topic_name": "Topic 4641",
            "project": "telegram_1003938559457_4641_bff429b6e587",
            "memory_namespace": "telegram:-1003938559457:4641/topic",
        },
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id=delegation["delegation_id"],
        delegation_build_owner="builder-source-1",
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        session_key=session_key,
        session_id=session_id,
        message_id="message-source-1",
        notifier_profile="default",
    )

    with kb.connect() as conn:
        execution = kb.get_task(conn, result.execution_task_id)
        run = kb.get_run(conn, int(execution.current_run_id))

    assert execution is not None
    assert "Carter’s Junk Away is a Colorado junk removal service" in execution.body
    assert run is not None
    worker_contract = run.metadata["loop_contract"]
    assert worker_contract["original_request"] == contract["original_request"]
    assert (
        worker_contract["audit"]["original_request_location"]
        == "Embedded in worker contract as original_request"
    )
    assert (
        seen["args"]["loop_contract"]["audit"]["original_request_location"]
        == "Embedded in worker contract as original_request"
    )


def test_source_truth_content_contract_exposes_original_request_to_openclaw(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()
    _activate_model_routing_policy()
    from proactive import openclaw_async_executor

    seen: dict[str, dict] = {}

    def fake_delegate(args, **_kw):
        seen["args"] = args
        return _openclaw_loop_result(args)

    monkeypatch.setattr(
        openclaw_async_executor,
        "delegate_loop_contract_to_openclaw",
        fake_delegate,
    )

    contract = _image_contract()
    contract["identity"]["request_instance_id"] = "source-truth-content-1"
    contract["original_request"] = (
        "Carter’s Junk Away is the source of truth. "
        "The article is about quote-system scaling in Colorado, not decluttering."
    )
    contract["grace_interpretation"] = (
        "Preserve Carter source facts and keep the source of truth visible to "
        "the backend worker."
    )
    contract["goal"]["deliverables"].append(
        "Facebook Page copy preserving source facts"
    )
    _normalized, fingerprint = _route_image_contract(contract)
    session_key = "agent:main:telegram:group:chat-1:4641"
    session_id = "session-source-truth-1"

    with kb.connect() as conn:
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint=fingerprint,
            request_instance_id=contract["identity"]["request_instance_id"],
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key=session_key,
            session_id=session_id,
            resolved_route=contract["routing"]["resolved"],
            approval_required=False,
        )
        assert kb.claim_grace_delegation_build(
            conn,
            delegation_id=delegation["delegation_id"],
            build_owner="builder-source-truth-1",
        )

    result = compile_and_delegate(
        contract,
        context={
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "4641",
            "topic_name": "Topic 4641",
            "project": "telegram_1003938559457_4641_bff429b6e587",
            "memory_namespace": "telegram:-1003938559457:4641/topic",
        },
        task_type="content_draft",
        risk_level="low",
        approved=False,
        delegation_id=delegation["delegation_id"],
        delegation_build_owner="builder-source-truth-1",
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        session_key=session_key,
        session_id=session_id,
        message_id="message-source-truth-1",
        notifier_profile="default",
    )

    with kb.connect() as conn:
        execution = kb.get_task(conn, result.execution_task_id)
        run = kb.get_run(conn, int(execution.current_run_id))

    assert execution is not None
    assert run is not None
    worker_contract = run.metadata["loop_contract"]
    assert worker_contract["original_request"] == contract["original_request"]
    assert (
        seen["args"]["loop_contract"]["audit"]["original_request_location"]
        == "Embedded in worker contract as original_request"
    )


def test_facebook_page_api_contract_uses_openclaw_operator_runtime():
    contract = _image_contract()
    contract["routing"] = {
        "task_type": "facebook_page_api_publish",
        "risk_level": "medium",
    }
    preliminary = validate_loop_contract(contract)
    preview = route_clawops_objective(
        contract["goal"]["objective"],
        project=contract["identity"]["project"],
        task_type="facebook_page_api_publish",
        risk_level="medium",
        approved=True,
        contract_fingerprint=contract_fingerprint(preliminary),
        runtime_callable_tools={
            "missioncrew-facebook-page-operator": {
                "facebook_page_graph_status",
                "facebook_page_graph_publish",
            }
        },
    )
    assert preview["status"] == "routed"
    contract["routing"]["resolved"] = resolved_route_binding(preview)
    normalized = validate_loop_contract(contract)

    assert contract_internal_hermes_runtime(
        normalized,
        task_type="facebook_page_api_publish",
    ) == ""
    assert (
        normalized["routing"]["resolved"]["assignment"]["runtime_profile"]
        == "missioncrew-facebook-page-operator"
    )
    unsafe = json.loads(json.dumps(normalized))
    unsafe["routing"]["resolved"]["assignment"]["allowed_tools"].append(
        "browser_click"
    )
    assert contract_internal_hermes_runtime(
        unsafe,
        task_type="facebook_page_api_publish",
    ) == ""


def test_image_generation_contract_does_not_use_internal_hermes_runtime():
    contract = _image_contract()
    normalized, _fingerprint = _route_image_contract(contract)

    assert contract_requires_image_generation(normalized) is True
    assert contract_internal_hermes_runtime(
        normalized,
        task_type="content_draft",
    ) == ""


def test_internal_ops_contract_uses_hermes_ops_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()
    _activate_model_routing_policy()

    contract = _image_contract()
    contract["identity"]["request_instance_id"] = "internal-ops-contract-1"
    contract["goal"] = {
        "objective": "Inspect internal capability registry and routing.",
        "deliverables": ["Capability report"],
        "non_goals": ["No external action"],
    }
    contract["scope"] = {
        "allowed": ["Internal registry, routing, status, and logs"],
        "forbidden": ["Facebook, browser, credential, or network action"],
    }
    contract["verification"] = {
        "checks": ["Read registry and routing"],
        "evidence_required": ["Tool availability report"],
        "acceptance_criteria": ["external_effect_budget=0"],
    }
    contract["routing"] = {"task_type": "ops", "risk_level": "low"}
    preliminary = validate_loop_contract(contract)
    preview = route_clawops_objective(
        contract["goal"]["objective"],
        project=contract["identity"]["project"],
        task_type="ops",
        risk_level="low",
        approved=False,
        contract_fingerprint=contract_fingerprint(preliminary),
    )
    contract["routing"]["resolved"] = resolved_route_binding(preview)
    normalized = validate_loop_contract(contract)
    assert contract_requires_image_generation(normalized) is False
    assert contract_internal_hermes_runtime(
        normalized,
        task_type="ops",
    ) == "clawops-ops"
    unsafe_route = json.loads(json.dumps(normalized, ensure_ascii=False))
    unsafe_route["routing"]["resolved"]["assignment"]["allowed_tools"].append(
        "browser_snapshot"
    )
    assert contract_internal_hermes_runtime(
        unsafe_route,
        task_type="ops",
    ) == ""
    missing_tools_route = json.loads(json.dumps(normalized, ensure_ascii=False))
    missing_tools_route["routing"]["resolved"]["assignment"].pop(
        "allowed_tools"
    )
    assert contract_internal_hermes_runtime(
        missing_tools_route,
        task_type="ops",
    ) == ""
    fingerprint = contract_fingerprint(normalized)
    session_key = "agent:main:telegram:group:chat-1:4641"
    session_id = "session-ops-1"

    with kb.connect() as conn:
        delegation = kb.reserve_grace_delegation(
            conn,
            contract_fingerprint=fingerprint,
            request_instance_id=contract["identity"]["request_instance_id"],
            platform="telegram",
            chat_id="chat-1",
            thread_id="4641",
            session_key=session_key,
            session_id=session_id,
            resolved_route=contract["routing"]["resolved"],
            approval_required=False,
        )
        assert kb.claim_grace_delegation_build(
            conn,
            delegation_id=delegation["delegation_id"],
            build_owner="builder-ops-1",
        )

    result = compile_and_delegate(
        contract,
        context={
            "platform": "telegram",
            "chat_id": "chat-1",
            "thread_id": "4641",
            "topic_name": "Topic 4641",
            "project": "telegram_1003938559457_4641_bff429b6e587",
            "memory_namespace": "telegram:-1003938559457:4641/topic",
        },
        task_type="ops",
        risk_level="low",
        approved=False,
        delegation_id=delegation["delegation_id"],
        delegation_build_owner="builder-ops-1",
        platform="telegram",
        chat_id="chat-1",
        thread_id="4641",
        session_key=session_key,
        session_id=session_id,
        message_id="message-ops-1",
        notifier_profile="default",
    )

    with kb.connect() as conn:
        execution = kb.get_task(conn, result.execution_task_id)
        review = kb.get_task(conn, result.review_task_id)

    assert result.assignee == "clawops-ops"
    assert execution is not None
    assert execution.executor_backend == "hermes"
    assert execution.executor_profile == "clawops-ops"
    assert review is not None
    assert review.executor_profile == "grace-policy-review"


@pytest.mark.parametrize("source_kind", ["package", "inventory", "callback"])
def test_registry_readonly_package_preserves_exact_source_in_card(source_kind):
    from proactive.grace_task_compiler import _worker_safe_contract
    from proactive.openclaw_async_executor import _worker_safe_loop_contract

    contract = _image_contract()
    original = "  🇺🇸 NewCase 原文\r\n\r\nExact punctuation！\n"
    if source_kind == "callback":
        original = "[SYSTEM: Grace Loop callback] source of truth: internal envelope"
    contract["original_request"] = original
    contract["grace_interpretation"] = "Preserve the original_request SOURCE MATERIAL verbatim."
    contract["domain_memory"] = {"mode": "query"}
    if source_kind != "inventory":
        contract["user_facing_delivery"] = {
            "kind": "content_package", "asset_filenames": ["newcase_page.png"],
        }
    safe = _worker_safe_contract(contract)
    if source_kind == "package":
        assert safe["original_request"].encode("utf-8") == original.encode("utf-8")
        assert _worker_safe_loop_contract(safe)["original_request"] == original
    else:
        assert "original_request" not in safe
