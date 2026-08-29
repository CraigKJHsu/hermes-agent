from __future__ import annotations

import pytest

from proactive.execution_backends import (
    ExecutionRequirements,
    assert_backend_transition,
    build_shadow_comparison_report,
    load_backend_registry,
    next_poll_delay_seconds,
    route_execution_backend,
    select_semantic_fallback,
)


def test_routes_isolated_readonly_browser_to_openclaw_with_audit_evidence():
    requirements = ExecutionRequirements.build(
        capabilities=[
            "browser_read",
            "isolated_session",
            "isolated_workspace",
        ],
        risk_level="low",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
        max_runtime_seconds=300,
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=123,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["decided_at"] == 123
    assert decision["mode"] == "enforced"
    assert decision["selection_reason"].endswith("openclaw")
    assert decision["candidates"][0] == {
        "backend": "openclaw",
        "eligible": True,
        "reasons": ["requirements_matched"],
        "cost_tier": "medium",
        "supports_async": True,
    }


def test_routes_image_generation_loop_contract_to_openclaw():
    requirements = ExecutionRequirements.build(
        capabilities=[
            "isolated_session",
            "long_running",
            "image_generate",
        ],
        risk_level="low",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
        preferred_backend="openclaw",
        max_runtime_seconds=600,
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=124,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["mode"] == "enforced"
    assert decision["candidates"][0]["eligible"] is True


def test_routes_facebook_page_publish_preflight_to_openclaw():
    requirements = ExecutionRequirements.build(
        capabilities=[
            "isolated_session",
            "long_running",
            "facebook_page_publish_preflight",
        ],
        semantic_class="isolated_long_running",
        risk_level="medium",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
        preferred_backend="openclaw",
        max_runtime_seconds=900,
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=125,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["selection_reason"].endswith("openclaw")
    assert decision["candidates"][0]["reasons"] == ["requirements_matched"]


def test_routes_facebook_page_graph_publish_to_openclaw():
    requirements = ExecutionRequirements.build(
        capabilities=[
            "browser_write",
            "facebook_page_graph_publish",
            "facebook_page_graph_status",
            "isolated_session",
            "long_running",
        ],
        semantic_class="browser_write",
        risk_level="high",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
        preferred_backend="openclaw",
        max_runtime_seconds=900,
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=126,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["candidates"][0]["eligible"] is True
    assert decision["candidates"][0]["reasons"] == ["requirements_matched"]


def test_open_circuit_produces_deterministic_no_backend_decision():
    requirements = ExecutionRequirements.build(
        capabilities=["browser_read", "isolated_session"],
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        circuit_states={"openclaw": "open"},
        now=456,
    )

    assert decision["selected_backend"] is None
    assert decision["candidates"][0]["reasons"] == ["circuit_open"]
    assert all(not item["eligible"] for item in decision["candidates"])


def test_half_open_circuit_requires_an_explicit_single_probe():
    requirements = ExecutionRequirements.build(
        capabilities=["browser_read", "isolated_session"],
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        circuit_states={"openclaw": "half_open"},
        now=457,
    )

    assert decision["selected_backend"] is None
    assert decision["candidates"][0]["reasons"] == [
        "circuit_half_open_probe_required"
    ]


def test_preferred_backend_does_not_override_missing_capabilities():
    requirements = ExecutionRequirements.build(
        capabilities=["code", "tests"],
        preferred_backend="hermes",
        workspace_policy="dedicated",
        session_policy="managed",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=789,
    )

    assert decision["candidates"][0]["backend"] == "hermes"
    assert decision["candidates"][0]["eligible"] is False
    assert "missing_capabilities:code,tests" in decision["candidates"][0]["reasons"]
    assert decision["selected_backend"] is None
    assert [item["backend"] for item in decision["candidates"]] == [
        "hermes",
        "openclaw",
    ]


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, "queued"),
        ("queued", "running"),
        ("queued", "succeeded"),
        ("running", "running"),
        ("running", "failed"),
        ("succeeded", "succeeded"),
    ],
)
def test_backend_lifecycle_accepts_monotonic_transitions(previous, current):
    assert_backend_transition(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("running", "queued"),
        ("succeeded", "running"),
        ("failed", "blocked"),
    ],
)
def test_backend_lifecycle_rejects_regression_or_terminal_rewrite(previous, current):
    with pytest.raises(ValueError):
        assert_backend_transition(previous, current)


def test_poll_backoff_is_bounded():
    assert [next_poll_delay_seconds(index) for index in range(6)] == [
        2,
        4,
        8,
        16,
        30,
        30,
    ]


def test_semantic_class_prevents_capability_only_fallback():
    requirements = ExecutionRequirements.build(
        capabilities=["browser_read"],
        semantic_class="browser_readonly",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=900,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["fallback_order"] == []
    assert select_semantic_fallback(
        decision,
        failed_backend="openclaw",
    ) is None
    assert len(decision["candidates"]) == 1


def test_semantic_fallback_selects_next_compatible_backend():
    registry = load_backend_registry()
    registry["policy"]["selection_order"] = ["codex", "hermes", "openclaw"]
    registry["backends"]["codex"]["enabled"] = True
    registry["backends"]["codex"]["semantic_classes"].append("analysis")
    requirements = ExecutionRequirements.build(
        capabilities=["analysis"],
        semantic_class="analysis",
    )

    decision = route_execution_backend(requirements, registry=registry, now=901)

    assert decision["selected_backend"] == "codex"
    assert select_semantic_fallback(
        decision,
        failed_backend="codex",
    ) == "hermes"


def test_target_policy_prefers_openclaw_for_verified_code_capability():
    requirements = ExecutionRequirements.build(
        capabilities=["code", "local_files", "tests", "isolated_workspace"],
        semantic_class="local_code",
        risk_level="medium",
        credential_policy="agent_scoped",
        workspace_policy="dedicated",
        session_policy="ephemeral",
        max_runtime_seconds=900,
    )

    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=904,
    )

    assert decision["selected_backend"] == "openclaw"
    assert decision["candidates"][0]["backend"] == "openclaw"
    assert decision["candidates"][0]["eligible"] is True
    assert decision["fallback_order"] == []


def test_shadow_report_compares_outcome_cost_duration_and_evidence():
    requirements = ExecutionRequirements.build(
        capabilities=["analysis"],
        semantic_class="analysis",
    )
    registry = load_backend_registry()
    registry["shadow_mode"] = True
    registry["policy"]["selection_order"] = ["openclaw", "hermes"]
    decision = route_execution_backend(
        requirements,
        registry=registry,
        now=902,
    )

    report = build_shadow_comparison_report(
        decision,
        {
            "openclaw": {
                "status": "succeeded",
                "duration_ms": 1200,
                "cost_units": 3.5,
                "evidence_digest": "sha256:same",
            },
            "hermes": {
                "status": "succeeded",
                "duration_ms": 800,
                "cost_units": 1.5,
                "evidence_digest": "sha256:same",
            },
        },
    )

    assert report["selected_backend"] == "openclaw"
    assert report["summary"] == {
        "observed_backends": 2,
        "comparable_backends": 1,
        "outcome_matches": 1,
        "evidence_matches": 1,
    }
    assert report["observations"][1]["duration_ms"] == 800


def test_shadow_report_requires_selected_backend_observation():
    requirements = ExecutionRequirements.build(capabilities=["analysis"])
    decision = route_execution_backend(
        requirements,
        registry=load_backend_registry(),
        now=903,
    )

    with pytest.raises(
        ValueError,
        match="selected-backend observation",
    ):
        build_shadow_comparison_report(decision, {})
