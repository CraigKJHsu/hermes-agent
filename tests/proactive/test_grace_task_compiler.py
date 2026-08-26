from __future__ import annotations

import hashlib
import json

from hermes_cli import kanban_db as kb
from proactive.grace_task_compiler import (
    compile_and_delegate,
    contract_internal_hermes_runtime,
    contract_requires_image_generation,
)
from proactive.hubops_routing import resolved_route_binding, route_clawops_objective
from proactive.loop_contract import contract_fingerprint, validate_loop_contract


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


def test_image_generation_loop_contract_routes_to_clawops_content(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

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

    assert result.assignee == "clawops-content"
    assert execution is not None
    assert execution.executor_backend == "hermes"
    assert execution.executor_profile == "clawops-content"
    assert "Image generation capability contract" in execution.body
    assert review is not None
    assert review.executor_profile == "grace-policy-review"
    assert len(callbacks) == 1


def test_internal_only_image_generation_contract_uses_content_runtime_without_token(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

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

    assert result.assignee == "clawops-content"
    assert execution is not None
    assert execution.executor_backend == "hermes"
    assert execution.executor_profile == "clawops-content"
    assert stored["approval_required"] == 0


def test_internal_ops_contract_uses_hermes_ops_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

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
