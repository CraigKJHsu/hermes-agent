from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from proactive.grace_task_compiler import render_execution_body, render_review_body
from proactive.loop_contract import LoopContractError, validate_loop_contract
from proactive.policy_registry import (
    PolicyRegistryError,
    activate_policy,
    bind_topic_policies,
    create_policy_version,
    policy_status,
    resolve_topic_policies_for_scope,
    resolve_task_policy_snapshots,
    topic_policy_binding_for_scope,
    topic_policy_binding,
    validate_policy_completion,
)


def _contract(namespace: str) -> dict:
    return {
        "identity": {
            "project": "topic_project",
            "topic_name": "Topic 5000",
            "thread_id": "5000",
            "request_instance_id": "policy-test-1",
        },
        "original_request": "Create the governed artifact",
        "grace_interpretation": "Create one internal governed artifact",
        "trigger": "authenticated user request",
        "completion_mode": "terminal",
        "goal": {
            "objective": "Create the governed artifact",
            "deliverables": ["artifact"],
            "non_goals": ["external publishing"],
        },
        "scope": {
            "allowed": ["internal artifact"],
            "forbidden": ["external state changes"],
        },
        "verification": {
            "checks": ["policy compliance"],
            "evidence_required": ["policy receipt"],
            "acceptance_criteria": ["all policy rules satisfied"],
        },
        "stop_rules": {
            "success": ["artifact verified"],
            "blocked": ["policy unavailable"],
            "no_progress": ["same failure twice"],
            "max_iterations": 3,
            "max_runtime_seconds": 600,
        },
        "memory": {
            "namespace": namespace,
            "working": ["current task state"],
            "promote_on_acceptance": ["accepted artifact pointer"],
        },
    }


def _install_policy(namespace: str, content: str = "# Brand policy\n\nMust comply.\n") -> dict:
    create_policy_version(
        "brand-policy",
        "2026-08-27.1",
        content,
        owner_scope="brand",
        owner_id="Example Brand",
        activate=True,
    )
    return bind_topic_policies(
        namespace,
        [{"policy_id": "brand-policy", "resolution": "latest_active"}],
    )


def test_topic_binding_resolves_complete_policy_into_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "telegram:chat:5000/project"
    content = "# Brand policy\n\nFull channel and review rules.\n"
    binding = _install_policy(namespace, content)

    normalized = validate_loop_contract(_contract(namespace))

    assert binding["namespace"] == namespace
    assert normalized["policy_requirements"] == [
        {
            "policy_id": "brand-policy",
            "resolution": "latest_active",
            "sections": [],
        }
    ]
    snapshot = normalized["policy_snapshots"][0]
    assert snapshot["content"] == content
    assert snapshot["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert snapshot["version"] == "2026-08-27.1"
    assert "GRACE_POLICY_SNAPSHOT:" in render_execution_body(normalized)
    assert "Topic memory is only a binding hint" in render_execution_body(normalized)
    assert "policy_stale" in render_review_body(normalized, "t_parent")


def test_same_policy_can_bind_multiple_topics_without_copying(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    create_policy_version(
        "shared-brand-policy",
        "v1",
        "# Shared policy\n",
        owner_scope="brand",
        owner_id="Shared Brand",
        activate=True,
    )
    for namespace in ("topic:a", "topic:b"):
        bind_topic_policies(
            namespace,
            [{"policy_id": "shared-brand-policy", "resolution": "latest_active"}],
        )
        normalized = validate_loop_contract(_contract(namespace))
        assert normalized["policy_snapshots"][0]["version"] == "v1"

    assert topic_policy_binding("topic:a")["requirements"] == topic_policy_binding(
        "topic:b"
    )["requirements"]


def test_topic_scope_resolves_unique_complete_active_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "telegram:-1003938559457:4641/topic-project"
    content = "# Audio Brief\n\nFormal complete instructions.\n"
    binding = _install_policy(namespace, content)

    resolved = resolve_topic_policies_for_scope(
        "telegram", "-1003938559457", "4641"
    )

    assert resolved["namespace"] == namespace
    assert resolved["binding_sha256"] == binding["binding_sha256"]
    assert resolved["policies"][0]["content"] == content
    assert resolved["policies"][0]["version"] == "2026-08-27.1"
    assert resolved["policies"][0]["sha256"] == hashlib.sha256(
        content.encode()
    ).hexdigest()


def test_topic_scope_fails_closed_for_ambiguous_bindings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _install_policy("telegram:chat:5000/project-a")
    bind_topic_policies(
        "telegram:chat:5000/project-b",
        [{"policy_id": "brand-policy", "resolution": "latest_active"}],
    )

    with pytest.raises(PolicyRegistryError, match="ambiguous"):
        topic_policy_binding_for_scope("telegram", "chat", "5000")


def test_task_snapshot_resolves_without_messaging_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "telegram:chat:5000/project"
    _install_policy(namespace, "complete policy")
    normalized = validate_loop_contract(_contract(namespace))
    body = render_review_body(normalized, "t_parent")

    resolved = resolve_task_policy_snapshots(body)

    assert resolved["binding"]["namespace"] == namespace
    assert resolved["policies"][0]["content"] == "complete policy"


def test_task_snapshot_requires_every_topic_bound_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "telegram:chat:5000/project"
    _install_policy(namespace, "complete policy")
    normalized = validate_loop_contract(_contract(namespace))
    marker = json.loads(
        render_review_body(normalized, "t_parent")
        .split("GRACE_POLICY_SNAPSHOT: ", 1)[1]
        .splitlines()[0]
    )
    marker["policies"] = []
    body = "GRACE_POLICY_SNAPSHOT: " + json.dumps(marker, sort_keys=True)

    with pytest.raises(PolicyRegistryError, match="all Topic requirements"):
        resolve_task_policy_snapshots(body)


def test_task_snapshot_preserves_topic_policy_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "telegram:chat:5000/project"
    create_policy_version(
        "brand-policy",
        "v1",
        "# Visual\n\nRules.\n",
        owner_scope="brand",
        owner_id="Example Brand",
        activate=True,
    )
    bind_topic_policies(
        namespace,
        [
            {
                "policy_id": "brand-policy",
                "resolution": "latest_active",
                "sections": ["Visual"],
            }
        ],
    )
    normalized = validate_loop_contract(_contract(namespace))

    resolved = resolve_task_policy_snapshots(
        render_review_body(normalized, "t_parent")
    )

    assert resolved["policies"][0]["sections"] == ["Visual"]


def test_policy_versions_are_immutable_and_activation_is_cas_guarded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    create_policy_version(
        "brand-policy",
        "v1",
        "version one",
        owner_scope="brand",
        owner_id="Brand",
        activate=True,
    )
    with pytest.raises(PolicyRegistryError, match="immutable"):
        create_policy_version(
            "brand-policy",
            "v1",
            "changed version one",
            owner_scope="brand",
            owner_id="Brand",
        )
    with pytest.raises(PolicyRegistryError, match="active version changed"):
        create_policy_version(
            "brand-policy",
            "v2",
            "version two",
            owner_scope="brand",
            owner_id="Brand",
            supersedes="v1",
            activate=True,
            expected_active_version="wrong",
        )


def test_concurrent_policy_activation_allows_only_one_cas_winner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    create_policy_version(
        "concurrent-policy",
        "v1",
        "version one",
        owner_scope="global",
        owner_id="Hermes",
        activate=True,
    )
    barrier = threading.Barrier(2)

    def update(version: str) -> str:
        barrier.wait()
        try:
            create_policy_version(
                "concurrent-policy",
                version,
                version,
                owner_scope="global",
                owner_id="Hermes",
                supersedes="v1",
                activate=True,
                expected_active_version="v1",
            )
        except PolicyRegistryError:
            return "rejected"
        return "activated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, ("v2", "v3")))

    assert sorted(outcomes) == ["activated", "rejected"]
    assert policy_status("concurrent-policy")["active_version"] in {"v2", "v3"}


def test_initial_activation_can_cas_against_no_active_version(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    create_policy_version(
        "initial-cas-policy",
        "v1",
        "version one",
        owner_scope="global",
        owner_id="Hermes",
    )
    activate_policy("initial-cas-policy", "v1", expected_active_version=None)
    create_policy_version(
        "initial-cas-policy",
        "v2",
        "version two",
        owner_scope="global",
        owner_id="Hermes",
    )

    with pytest.raises(PolicyRegistryError, match="active version changed"):
        activate_policy("initial-cas-policy", "v2", expected_active_version=None)


def test_latest_active_contract_and_review_fail_closed_after_policy_update(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:stale"
    _install_policy(namespace, "version one")
    normalized = validate_loop_contract(_contract(namespace))
    execution_body = render_execution_body(normalized)
    review_body = render_review_body(normalized, "t_parent")
    old = normalized["policy_snapshots"][0]

    create_policy_version(
        "brand-policy",
        "2026-08-27.2",
        "version two",
        owner_scope="brand",
        owner_id="Example Brand",
        supersedes="2026-08-27.1",
        activate=True,
        expected_active_version="2026-08-27.1",
    )

    with pytest.raises(LoopContractError, match="stale or altered"):
        validate_loop_contract(normalized)

    kb.init_db()
    with kb.connect_closing() as conn:
        execution_id = kb.create_task(conn, title="execution", body=execution_body)
        assert kb.complete_task(
            conn,
            execution_id,
            summary="done",
            metadata={
                "policy_receipts": [
                    {
                        "role": "execution",
                        "policy_id": old["policy_id"],
                        "version": old["version"],
                        "sha256": old["sha256"],
                        "loaded": True,
                    }
                ]
            },
        )
        review_id = kb.create_task(conn, title="review", body=review_body)
        with pytest.raises(PolicyRegistryError, match="policy_stale"):
            kb.complete_task(
                conn,
                review_id,
                summary="accepted",
                metadata={
                    "review_outcome": "accepted",
                    "policy_receipts": [
                        {
                            "role": "review",
                            "policy_id": old["policy_id"],
                            "version": old["version"],
                            "sha256": old["sha256"],
                            "loaded": True,
                            "latest_active_verified": True,
                        }
                    ],
                },
            )


def test_review_fails_when_topic_binding_changes_after_compilation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:binding-stale"
    _install_policy(namespace)
    normalized = validate_loop_contract(_contract(namespace))
    snapshot = normalized["policy_snapshots"][0]
    current = topic_policy_binding(namespace)
    create_policy_version(
        "second-policy",
        "v1",
        "# Second policy\n",
        owner_scope="topic",
        owner_id=namespace,
        activate=True,
    )
    bind_topic_policies(
        namespace,
        [
            {"policy_id": "brand-policy", "resolution": "latest_active"},
            {"policy_id": "second-policy", "resolution": "latest_active"},
        ],
        expected_binding_sha256=current["binding_sha256"],
    )

    with pytest.raises(PolicyRegistryError, match="Topic policy binding changed"):
        validate_policy_completion(
            render_review_body(normalized, "t_parent"),
            {
                "policy_receipts": [
                    {
                        "role": "review",
                        "policy_id": snapshot["policy_id"],
                        "version": snapshot["version"],
                        "sha256": snapshot["sha256"],
                        "loaded": True,
                        "latest_active_verified": True,
                    }
                ]
            },
            role="review",
        )


def test_topic_binding_cas_rejects_stale_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:binding-cas"
    _install_policy(namespace)
    stale_sha256 = topic_policy_binding(namespace)["binding_sha256"]
    bind_topic_policies(
        namespace,
        [],
        expected_binding_sha256=stale_sha256,
    )

    with pytest.raises(PolicyRegistryError, match="binding changed"):
        bind_topic_policies(
            namespace,
            [{"policy_id": "brand-policy", "resolution": "latest_active"}],
            expected_binding_sha256=stale_sha256,
        )


def test_policy_completion_requires_matching_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:receipt"
    _install_policy(namespace)
    normalized = validate_loop_contract(_contract(namespace))
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="execution",
            body=render_execution_body(normalized),
        )
        with pytest.raises(PolicyRegistryError, match="policy_receipts"):
            kb.complete_task(conn, task_id, summary="done", metadata={})


def test_policy_text_cannot_change_execution_receipt_role(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:stage-token"
    _install_policy(
        namespace,
        "# Policy\n\nLiteral example: GRACE_LOOP_CONTRACT_STAGE: grace_review\n",
    )
    normalized = validate_loop_contract(_contract(namespace))
    snapshot = normalized["policy_snapshots"][0]
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="execution",
            body=render_execution_body(normalized),
        )
        assert kb.complete_task(
            conn,
            task_id,
            summary="done",
            metadata={
                "policy_receipts": [
                    {
                        "role": "execution",
                        "policy_id": snapshot["policy_id"],
                        "version": snapshot["version"],
                        "sha256": snapshot["sha256"],
                        "loaded": True,
                    }
                ]
            },
        )


def test_current_policy_review_with_exact_receipt_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:current-review"
    _install_policy(namespace)
    normalized = validate_loop_contract(_contract(namespace))
    snapshot = normalized["policy_snapshots"][0]
    kb.init_db()
    with kb.connect_closing() as conn:
        review_id = kb.create_task(
            conn,
            title="review",
            body=render_review_body(normalized, "t_parent"),
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="accepted",
            metadata={
                "review_outcome": "accepted",
                "policy_receipts": [
                    {
                        "role": "review",
                        "policy_id": snapshot["policy_id"],
                        "version": snapshot["version"],
                        "sha256": snapshot["sha256"],
                        "loaded": True,
                        "latest_active_verified": True,
                    }
                ],
            },
        )


def test_policy_status_reads_back_active_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    content = "# Policy\n"
    create_policy_version(
        "status-policy",
        "v1",
        content,
        owner_scope="topic",
        owner_id="topic:status",
        activate=True,
    )
    status = policy_status("status-policy")
    assert status["active_version"] == "v1"
    assert status["active_sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_policy_registry_is_shared_across_worker_profiles(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    monkeypatch.setenv("HERMES_HOME", str(root))
    create_policy_version(
        "shared-policy",
        "v1",
        "# Shared\n",
        owner_scope="global",
        owner_id="Hermes",
        activate=True,
    )

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "clawops-content"))
    status = policy_status("shared-policy")
    assert status["active_version"] == "v1"
    assert status["manifest_path"].startswith(str(root / "policies"))


def test_policy_marker_and_duplicate_receipts_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:fail-closed"
    _install_policy(namespace)
    normalized = validate_loop_contract(_contract(namespace))
    body = render_execution_body(normalized)
    snapshot = normalized["policy_snapshots"][0]
    receipt = {
        "role": "execution",
        "policy_id": snapshot["policy_id"],
        "version": snapshot["version"],
        "sha256": snapshot["sha256"],
        "loaded": True,
    }

    with pytest.raises(PolicyRegistryError, match="duplicate execution"):
        validate_policy_completion(
            body,
            {"policy_receipts": [receipt, dict(receipt)]},
            role="execution",
        )

    malformed_lines = body.splitlines()
    marker_index = next(
        index
        for index, line in enumerate(malformed_lines)
        if line.startswith("GRACE_POLICY_SNAPSHOT:")
    )
    prefix, payload_text = malformed_lines[marker_index].split(":", 1)
    payload = json.loads(payload_text)
    payload["policies"].append("invalid")
    malformed_lines[marker_index] = f"{prefix}: {json.dumps(payload)}"
    malformed = "\n".join(malformed_lines)
    with pytest.raises(PolicyRegistryError, match="refs must be objects"):
        validate_policy_completion(
            malformed,
            {"policy_receipts": [receipt]},
            role="execution",
        )


def test_empty_topic_binding_is_pinned_and_rejected_if_policy_is_added(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:initially-empty"
    normalized = validate_loop_contract(_contract(namespace))
    assert normalized["policy_binding_snapshot"]["sha256"] is None
    assert normalized.get("policy_snapshots") is None
    review_body = render_review_body(normalized, "t_parent")
    assert "GRACE_POLICY_SNAPSHOT:" in review_body

    create_policy_version(
        "new-policy",
        "v1",
        "# New policy\n",
        owner_scope="topic",
        owner_id=namespace,
        activate=True,
    )
    bind_topic_policies(
        namespace,
        [{"policy_id": "new-policy", "resolution": "latest_active"}],
        expected_binding_sha256=None,
    )

    with pytest.raises(PolicyRegistryError, match="Topic policy binding changed"):
        validate_policy_completion(
            review_body,
            {"policy_receipts": []},
            role="review",
        )


def test_review_rechecks_active_manifest_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    namespace = "topic:manifest-digest"
    _install_policy(namespace)
    normalized = validate_loop_contract(_contract(namespace))
    snapshot = normalized["policy_snapshots"][0]
    manifest_path = Path(snapshot["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["versions"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PolicyRegistryError, match="digest mismatch"):
        validate_policy_completion(
            render_review_body(normalized, "t_parent"),
            {
                "policy_receipts": [
                    {
                        "role": "review",
                        "policy_id": snapshot["policy_id"],
                        "version": snapshot["version"],
                        "sha256": snapshot["sha256"],
                        "loaded": True,
                        "latest_active_verified": True,
                    }
                ]
            },
            role="review",
        )
