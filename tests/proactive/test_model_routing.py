from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from proactive.model_routing import (
    MODEL_LUNA,
    MODEL_MECHANICAL,
    MODEL_SOL,
    MODEL_SPARK,
    MODEL_TERRA,
    ModelRoutingError,
    attest_runtime_execution,
    classify_grace_message,
    clear_routing_env,
    execution_receipt_from_env,
    route_grace,
    route_worker,
    routing_env,
    validate_grace_acceptance_receipt,
)
from proactive.policy_registry import PolicyRegistryError, create_policy_version


@pytest.fixture(autouse=True)
def active_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
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


def test_grace_default_review_and_critical_routes() -> None:
    assert route_grace("planning")["requested_model"] == MODEL_SOL
    assert route_grace("planning")["reasoning_effort"] == "medium"

    review = route_grace("acceptance_review")
    assert review["reasoning_effort"] == "high"
    assert review["fallback_allowed"] is False

    critical = route_grace(
        "planning", {"financial_or_security_impact": True}
    )
    assert critical["reasoning_effort"] == "xhigh"
    assert critical["reasoning_mode"] == "standard"


def test_gateway_preclassification_escalates_sensitive_turns() -> None:
    decision = classify_grace_message("請刪除正式資料並處理 API key")
    assert decision["task_risk"] == "high"
    assert decision["destructive_or_irreversible"] is True
    assert decision["financial_or_security_impact"] is True
    route = route_grace("planning", decision)
    assert route["reasoning_effort"] == "xhigh"
    assert route["fallback_allowed"] is False


def test_routing_environment_is_cleared_before_child_injection() -> None:
    env = {
        "PATH": "/bin",
        "HERMES_KANBAN_TASK": "t_stale",
        "HERMES_REASONING_EFFORT": "xhigh",
        "HERMES_DISABLE_MODEL_FALLBACK": "1",
        "HERMES_MODEL_ROUTING_RECEIPT": "stale",
    }
    clear_routing_env(env)
    assert env == {"PATH": "/bin"}


def test_routing_environment_binds_the_worker_to_the_task() -> None:
    route = route_grace("acceptance_review")
    env = routing_env(route, task_id="t_review")
    assert env["HERMES_KANBAN_TASK"] == "t_review"


def test_worker_routes_spark_luna_and_terra_by_work_class() -> None:
    assert route_worker("focused_code")["requested_model"] == MODEL_SPARK
    assert route_worker("browser_readonly")["requested_model"] == MODEL_MECHANICAL
    assert route_worker("general")["requested_model"] == MODEL_TERRA
    assert route_worker("devops")["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "task_type",
    [
        "facebook_marketplace_readonly",
        "facebook_marketplace_group_readonly",
        "facebook_group_readonly",
        "secondhand_commerce_group_status",
        "commerce_group_status",
        "read_only",
        "status_check",
    ],
)
def test_commerce_readonly_tasks_use_mechanical_worker_route(
    task_type: str,
) -> None:
    route = route_worker(task_type)
    assert route["requested_model"] == MODEL_MECHANICAL
    assert route["reasoning_effort"] == "low"
    assert route["routing_reason"] == "mechanical_high_volume_work"


def _attest(
    route: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_id: str = "t_review",
) -> dict:
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    for key, value in routing_env(route, task_id=task_id).items():
        monkeypatch.setenv(key, value)
    attest_runtime_execution(
        model=route["requested_model"],
        reasoning_effort=route["reasoning_effort"],
        api_mode="codex_responses",
    )
    receipt = execution_receipt_from_env(
        os.environ["HERMES_MODEL_ROUTING_RECEIPT"]
    )
    assert receipt is not None
    return receipt


def test_grace_acceptance_rejects_spark_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spark = route_worker("focused_code")
    spark_receipt = _attest(spark, monkeypatch)
    with pytest.raises(ModelRoutingError, match="gpt-5.6-sol"):
        validate_grace_acceptance_receipt(
            spark_receipt, expected_route=spark, expected_task_id="t_review"
        )

    review = route_grace("acceptance_review")
    review_receipt = _attest(review, monkeypatch)
    review_receipt["fallback_applied"] = True
    with pytest.raises(ModelRoutingError, match="prohibits fallback"):
        validate_grace_acceptance_receipt(
            review_receipt, expected_route=review, expected_task_id="t_review"
        )


def test_grace_acceptance_rejects_reasoning_below_critical_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    critical = route_grace(
        "acceptance_review", {"financial_or_security_impact": True}
    )
    receipt = _attest(critical, monkeypatch)
    receipt["effective_reasoning_effort"] = "high"
    with pytest.raises(ModelRoutingError, match="below the task route"):
        validate_grace_acceptance_receipt(
            receipt, expected_route=critical, expected_task_id="t_review"
        )


def test_grace_acceptance_rejects_receipt_for_a_different_policy_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = route_grace("acceptance_review")
    forged = _attest(review, monkeypatch)
    forged["policy_sha256"] = "0" * 64
    with pytest.raises(ModelRoutingError, match="does not match"):
        validate_grace_acceptance_receipt(
            forged, expected_route=review, expected_task_id="t_review"
        )


def test_grace_acceptance_rejects_a_missing_task_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = route_grace("acceptance_review")
    receipt = _attest(review, monkeypatch)
    with pytest.raises(ModelRoutingError, match="does not match"):
        validate_grace_acceptance_receipt(
            receipt, expected_route=None, expected_task_id="t_review"
        )


def test_grace_acceptance_keeps_verified_task_snapshot_after_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = route_grace("acceptance_review")
    receipt = _attest(review, monkeypatch)
    source = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "managed-policies"
        / "missioncrew-model-routing-v1.json"
    ).read_text(encoding="utf-8")
    rotated = json.loads(source)
    rotated["test_revision"] = 2
    create_policy_version(
        "missioncrew-model-routing-v1",
        "v2",
        json.dumps(rotated, sort_keys=True),
        owner_scope="global",
        owner_id="missioncrew",
        activate=True,
        expected_active_version="v1",
    )
    validate_grace_acceptance_receipt(
        receipt, expected_route=review, expected_task_id="t_review"
    )


def test_grace_rejects_invalid_critical_reasoning_policy() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "managed-policies"
        / "missioncrew-model-routing-v1.json"
    ).read_text(encoding="utf-8")
    malformed = json.loads(source)
    malformed["grace"]["critical_reasoning"] = "xhgh"
    create_policy_version(
        "missioncrew-model-routing-v1",
        "v2",
        json.dumps(malformed, sort_keys=True),
        owner_scope="global",
        owner_id="missioncrew",
        activate=True,
        expected_active_version="v1",
    )
    with pytest.raises(ModelRoutingError, match="Grace configuration"):
        route_grace("critical_action")


def test_corrupted_active_policy_does_not_fall_back_to_bootstrap(
    tmp_path: Path,
) -> None:
    version = (
        tmp_path
        / "hermes"
        / "policies"
        / "registry"
        / "missioncrew-model-routing-v1"
        / "versions"
        / "v1.md"
    )
    version.write_text("tampered", encoding="utf-8")
    with pytest.raises(PolicyRegistryError, match="digest.*match"):
        route_grace("planning")


def test_draft_policy_still_allows_bundled_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_home = tmp_path / "draft-hermes"
    monkeypatch.setenv("HERMES_HOME", str(draft_home))
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
        activate=False,
        expected_active_version=None,
    )
    route = route_worker("focused_code")
    assert route["policy_source"] == "bundled_bootstrap"
    with pytest.raises(ModelRoutingError, match="active managed policy"):
        route_grace("acceptance_review")


def test_policy_receipt_is_immutable_and_traceable() -> None:
    route = route_worker("focused_code")
    assert route["policy_snapshot_id"] == "missioncrew-model-routing-v1@v1"
    assert len(route["policy_sha256"]) == 64
    assert json.dumps(route, sort_keys=True)
