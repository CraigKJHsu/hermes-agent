"""Historical samples test shared boundaries, never hardcoded Topic exceptions."""
from copy import deepcopy
import json

import pytest

from hermes_cli import kanban_db as kb
from proactive import behavior_observation as observation
from proactive import domain_memory
from proactive.grace_task_compiler import render_execution_body
from proactive.loop_contract import LoopContractError, contract_fingerprint, validate_loop_contract
from proactive.policy_registry import create_policy_version
from scripts.replay_behavior_observation import load_cases, make_contract, replay_case


FIXTURE = load_cases()


@pytest.fixture(autouse=True)
def canonical_replay_objectives(tmp_path, monkeypatch, request):
    if request.node.name not in {
        "test_observation_cannot_change_contract_fingerprint_rendering_or_exception",
        "test_receipts_have_policy_provenance_without_leaking_content_or_tokens",
        "test_nested_observers_do_not_mix_topic_receipts",
    }:
        return
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "observation.db"))
    with kb.connect_closing() as conn:
        for case in FIXTURE["cases"]:
            contract = make_contract(FIXTURE, case)
            identity = contract["identity"]
            kb.create_grace_objective(
                conn, objective_id="go_" + case["id"], platform=identity["platform"],
                chat_id=identity["chat_id"], thread_id=identity["thread_id"],
                session_key="fixture", title="replay", objective="replay",
                original_request_sha256="a" * 64, required_stage_keys=["prepare", "publish"],
                terminal_stage_key="publish", acceptance_criteria=["review and approval"],
            )


def create(conn, objective_id="go_metadata"):
    return kb.create_grace_objective(
        conn, objective_id=objective_id, platform="telegram", chat_id="fixture-chat",
        thread_id="fixture", session_key="fixture", title="fixture", objective="fixture",
        original_request_sha256="a" * 64, required_stage_keys=["prepare", "publish"],
        terminal_stage_key="publish", acceptance_criteria=["review and approval"],
    )


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_historical_replay_preserves_expected_outcome(tmp_path, case):
    events = []
    with observation.capture(events.append):
        result = replay_case(FIXTURE, case, tmp_path / "replay.db")
    assert any(event["rule_id"] == "objective.create" for event in events)
    assert all(event["mode"] == "observe_only" for event in events)
    if "expected_error" in case:
        assert case["expected_error"] in result["error"]
        assert result["task_count"] == 0
        assert any(event["rule_id"] == "loop_contract.facebook_group_publish_scope" for event in events)
    else:
        assert result["objective"]["status"] == case["expected_status"]
        assert result["objective"]["current_stage_key"] == case["expected_stage"]
        assert result["package"] == case["package"]
        assert result["external_effect_count"] == result["successor_task_count"] == 0
        assert any(event["rule_id"] in {"objective.callback_outcome", "objective.review_block"}
                   and event["objective_id"] == "go_" + case["id"] for event in events)
        if case["review_outcome"] == "rejected":
            assert result["task_states"] == {"execution": "ready", "review": "todo"}
            assert result["callback"]["outcome_kind"] is None
            assert result["approval_states"] == []
        else:
            assert result["callback"]["state"] == "delivered"
            assert result["approval_states"] == [["pending", 1]]


@pytest.mark.parametrize("changed_schema,affected,unaffected", [
    ("secondhand.item.v1", 2, 0), ("solobizai.case.v1", 0, 2),
])
def test_business_definition_change_does_not_interfere_with_other_topic(
    tmp_path, monkeypatch, changed_schema, affected, unaffected,
):
    before = replay_case(FIXTURE, FIXTURE["cases"][unaffected], tmp_path / "before.db")
    affected_before = replay_case(FIXTURE, FIXTURE["cases"][affected], tmp_path / "affected-before.db")
    changed = deepcopy(domain_memory.BUILTIN_DOMAIN_SCHEMAS[changed_schema])
    changed["artifact_types"].append("fixture_probe")
    monkeypatch.setitem(domain_memory.BUILTIN_DOMAIN_SCHEMAS, changed_schema, changed)
    assert replay_case(FIXTURE, FIXTURE["cases"][unaffected], tmp_path / "after.db") == before
    affected_after = replay_case(FIXTURE, FIXTURE["cases"][affected], tmp_path / "affected-after.db")
    assert affected_after["normalized_contract"] != affected_before["normalized_contract"]


def test_observation_cannot_change_contract_fingerprint_rendering_or_exception(monkeypatch):
    contract = make_contract(FIXTURE, FIXTURE["cases"][0])
    original = deepcopy(contract)
    expected = validate_loop_contract(contract)
    fingerprint = contract_fingerprint(expected)
    body = render_execution_body(expected)

    def broken_sink(event):
        event.clear()
        raise RuntimeError("sink unavailable")

    def broken_logger(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(observation.logger, "info", broken_logger)
    with observation.capture(broken_sink):
        actual = validate_loop_contract(contract)
        assert actual == expected
        assert contract_fingerprint(actual) == fingerprint
        assert render_execution_body(actual) == body
        invalid = deepcopy(contract)
        invalid["stop_rules"]["max_iterations"] = 0
        with pytest.raises(LoopContractError, match="max_iterations must be an integer from 1 to 20"):
            validate_loop_contract(invalid)
    assert contract == original


def test_receipts_have_policy_provenance_without_leaking_content_or_tokens():
    create_policy_version("fixture-policy", "v1", "PRIVATE_POLICY_TEXT", owner_scope="global",
                          owner_id="fixture", activate=True, expected_active_version=None)
    contract = make_contract(FIXTURE, FIXTURE["cases"][0])
    contract["policy_requirements"] = [{"policy_id": "fixture-policy", "resolution": "fixed", "version": "v1"}]
    contract["approval_provenance"] = {"approval_token": "PRIVATE_APPROVAL_TOKEN"}
    contract["original_request"] = "PRIVATE_REQUEST_TEXT"
    events = []
    with observation.capture(events.append):
        normalized = validate_loop_contract(contract)
    receipt = next(event for event in events if event["decision"] == "accepted"
                   and event["rule_id"] == "loop_contract.schema")
    assert receipt["policy_snapshot_hash"] == observation.digest(normalized["policy_snapshots"])
    assert receipt["contract_schema_version"] == normalized["contract_version"]
    assert receipt["behavior_profile_version"] is None
    assert receipt["runtime_version_verified"] is False
    encoded = json.dumps(events)
    for private in ("PRIVATE_POLICY_TEXT", "PRIVATE_APPROVAL_TOKEN", "PRIVATE_REQUEST_TEXT"):
        assert private not in encoded


def test_new_objective_sidecar_is_durable_and_never_relabels_existing_objective(tmp_path, monkeypatch):
    path = tmp_path / "metadata.db"
    with kb.connect_closing(path) as conn:
        original = create(conn)
        saved = dict(conn.execute("SELECT * FROM grace_objective_behavior_observations").fetchone())
        assert "behavior_profile_id" not in original  # Existing prompt/return shape stays identical.
        assert saved["behavior_profile_id"] == "legacy_shared"
        assert saved["behavior_profile_version"] is None
        assert saved["policy_snapshot_hash"] is None  # Creation has no resolved contract yet.
    with kb.connect_closing(path) as conn:
        monkeypatch.setattr(observation, "metadata", lambda *args: {"unexpected": "new version"})
        assert create(conn) == original
        assert dict(conn.execute("SELECT * FROM grace_objective_behavior_observations").fetchone()) == saved


def test_additive_schema_does_not_backfill_legacy_objectives(tmp_path):
    path = tmp_path / "legacy.db"
    with kb.connect_closing(path) as conn:
        conn.execute("DROP TABLE grace_objective_behavior_observations")
        original = create(conn)  # Optional storage failure must preserve creation.
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    with kb.connect_closing(path) as conn:
        assert create(conn) == original
        assert conn.execute("SELECT count(*) FROM grace_objective_behavior_observations").fetchone()[0] == 0


def test_caller_transaction_is_never_used_for_optional_sidecar_writes(tmp_path):
    with kb.connect_closing(tmp_path / "rollback.db") as conn:
        conn.execute("BEGIN")
        create(conn)
        assert conn.execute("SELECT count(*) FROM grace_objective_behavior_observations").fetchone()[0] == 0
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT count(*) FROM grace_objectives").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM grace_objective_behavior_observations").fetchone()[0] == 0


def test_sidecar_storage_rollback_cannot_undo_business_creation(tmp_path):
    with kb.connect_closing(tmp_path / "storage-failure.db") as conn:
        conn.execute("CREATE TRIGGER fail_observation BEFORE INSERT ON grace_objective_behavior_observations "
                     "BEGIN SELECT RAISE(ROLLBACK,'injected observation storage fault'); END")
        result = create(conn)
        assert result["status"] == "active"
        assert kb.get_grace_objective(conn, "go_metadata") == result
        assert conn.execute("SELECT count(*) FROM grace_objective_stages").fetchone()[0] == 2


def test_nested_observers_do_not_mix_topic_receipts():
    first, second = [], []
    with observation.capture(first.append):
        validate_loop_contract(make_contract(FIXTURE, FIXTURE["cases"][0]))
        with observation.capture(second.append):
            validate_loop_contract(make_contract(FIXTURE, FIXTURE["cases"][2]))
        validate_loop_contract(make_contract(FIXTURE, FIXTURE["cases"][1]))
    assert {event["project"] for event in first} == {"ai_bizweek"}
    assert {event["project"] for event in second} == {"secondhand_commerce"}
