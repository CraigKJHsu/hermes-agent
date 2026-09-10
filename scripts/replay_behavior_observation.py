#!/usr/bin/env python3
"""Replay fixed review outcomes through real local Contract/SQLite boundaries.

Always uses a new output directory as HERMES_HOME. No worker, browser, model,
approval consumer or external publication is invoked. Retains evidence files.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch


def load_cases():
    return json.loads((Path(__file__).resolve().parents[1]
                       / "tests/fixtures/behavior_observation/topics.json").read_text())


def make_contract(fixture, case):
    contract = deepcopy(fixture["base_contract"])
    contract["identity"].update(
        project=case["project"], thread_id=case["thread_id"], topic_name=case["topic_name"],
    )
    contract["memory"]["namespace"] = f"telegram:fixture-chat:{case['thread_id']}/{case['project']}"
    contract["objective_ref"] = {"objective_id": "go_" + case["id"], "stage_key": "prepare"}
    contract["routing"] = {"task_type": case["task_type"], "risk_level": "low"}
    contract.update(deepcopy(case.get("contract_override", {})))
    return contract


def complete_fixture_review(conn, review_id, case, run_id):
    """Replay a synthetic transport receipt, as existing callback tests do.

    No model was called: this proves receipt/callback handling, not review quality.
    """
    from hermes_cli import kanban_db as kb
    from proactive.model_routing import attest_runtime_execution, route_grace, routing_env
    from proactive.policy_registry import create_policy_version

    source = (Path(kb.__file__).resolve().parents[1]
              / "config/managed-policies/missioncrew-model-routing-v1.json").read_text()
    create_policy_version("missioncrew-model-routing-v1", "v1", source,
                          owner_scope="global", owner_id="missioncrew", activate=True)
    route = route_grace("acceptance_review")
    conn.execute("UPDATE tasks SET routing_decision=? WHERE id=?",
                 (json.dumps({"model_route": route}), review_id))
    with patch.dict(os.environ, routing_env(route, task_id=review_id)):
        attest_runtime_execution(model=route["requested_model"],
                                 reasoning_effort=route["reasoning_effort"], api_mode="codex_responses")
        return kb.complete_task(conn, review_id, summary=case.get("reason", "accepted"),
                                metadata={"review_outcome": case["review_outcome"], "policy_receipts": []},
                                expected_run_id=run_id)


def replay_case(fixture, case, db_path):
    from hermes_cli import kanban_db as kb
    from proactive.grace_task_compiler import render_execution_body, render_review_body
    from proactive.hubops_routing import _match_worker_route
    from proactive.loop_contract import LoopContractError, contract_fingerprint, validate_loop_contract

    contract = make_contract(fixture, case)
    objective_id = contract["objective_ref"]["objective_id"]
    scope = dict(platform="telegram", chat_id="fixture-chat", thread_id=case["thread_id"])
    session_key = f"agent:main:telegram:fixture-chat:{case['thread_id']}"
    with kb.connect_closing(db_path) as conn:
        kb.create_grace_objective(
            conn, objective_id=objective_id, **scope, session_key=session_key,
            title=case["id"], objective=contract["goal"]["objective"],
            original_request_sha256=hashlib.sha256(contract["original_request"].encode()).hexdigest(),
            required_stage_keys=["prepare", "publish"], terminal_stage_key="publish",
            acceptance_criteria=["Reviewed package and separately authorized publication"],
        )
        try:
            normalized = validate_loop_contract(contract)
        except LoopContractError as exc:
            return {"decision": "rejected", "review_outcome": "not_run", "error": str(exc),
                    "task_count": conn.execute("SELECT count(*) FROM tasks").fetchone()[0]}
        execution_body = render_execution_body(normalized)
        review_body = render_review_body(normalized, "t_fixture_execution")
        # Exercise the actual matcher with fixed local routes, never live routing YAML.
        route = _match_worker_route([
            {"match": {"project": "ai_bizweek", "task_type": "content_draft"}, "worker": "fixture-content"},
            {"match": {"project": "secondhand_commerce", "task_type": "facebook_marketplace_readonly"}, "worker": "fixture-readonly"},
        ], project=case["project"], task_type=case["task_type"], risk_level="low")
        fingerprint = contract_fingerprint(normalized)
        execution_id = kb.create_task(conn, title="prepare", body=execution_body)
        execution = kb.claim_task(conn, execution_id, claimer="fixture-executor")
        assert execution is not None
        assert kb.complete_task(conn, execution_id, summary="fixture evidence",
                                metadata={"package": case["package"], "policy_receipts": []},
                                expected_run_id=execution.current_run_id)
        review_id = kb.create_task(conn, title="review", body=review_body, parents=(execution_id,))
        kb.add_grace_loop_callback(
            conn, review_task_id=review_id, execution_task_id=execution_id, **scope,
            session_key=session_key, session_id="fixture-session", contract_fingerprint=fingerprint,
            completion_mode="intermediate", objective_id=objective_id, stage_key="prepare",
        )
        conn.execute(
            "INSERT INTO grace_delegations(delegation_id,contract_fingerprint,request_instance_id,"
            "platform,chat_id,thread_id,session_key,session_id,resolved_route,approval_required,"
            "state,execution_task_id,review_task_id,created_at,updated_at) "
            "VALUES (?,?,?,'telegram','fixture-chat',?,?,'fixture-session','{}',0,'queued',?,?,1,1)",
            ("gd_fixture", fingerprint, case["id"], case["thread_id"], session_key, execution_id, review_id),
        )
        review = kb.claim_task(conn, review_id, claimer="fixture-reviewer")
        assert review is not None
        # This is a recorded reviewer decision, not a new LLM/content validator.
        if case["review_outcome"] == "rejected":
            # Formal reviews reject via block_task; complete_task accepts only
            # accepted receipts. Preserve this existing lifecycle distinction.
            assert kb.block_task(conn, review_id, reason=case["reason"], kind="dependency")
        else:
            assert complete_fixture_review(conn, review_id, case, review.current_run_id)
        approval_token = None
        if case["review_outcome"] == "rejected":
            # The existing dependency-rejection path reopens the SAME execution
            # for correction; it does not deliver an accepted callback or publish.
            assert kb.list_due_grace_loop_callbacks(conn) == []
        else:
            callback = kb.list_due_grace_loop_callbacks(conn)[0]
            assert kb.claim_grace_loop_callback(conn, review_task_id=review_id,
                                                 event_id=callback["event_id"], lease_owner="fixture-owner")
            approval_scope = {"package_id": case["id"]}
            challenge = kb.create_grace_approval_challenge(
                conn, contract_fingerprint=fingerprint, request_instance_id=case["id"], **scope,
                session_key=session_key, session_id="fixture-session", user_id_sha256="a" * 64,
                requested_message_id="fixture-message", action_summary="Publish reviewed package",
                approval_platform="fixture", approval_scope=json.dumps(approval_scope, sort_keys=True, separators=(",", ":")),
                origin_review_task_id=review_id, origin_event_id=callback["event_id"],
                callback_lease_owner="fixture-owner",
            )
            approval_token = challenge["token"]
            kb.record_grace_loop_callback_outcome(
                conn, review_task_id=review_id, event_id=callback["event_id"], **scope,
                session_id="fixture-session", lease_owner="fixture-owner", outcome_kind="approval_blocked",
                payload={"action": "Publish reviewed package", "platform": "fixture",
                         "scope": approval_scope, "exact_question": "核准 " + approval_token,
                         "next_stage_key": "publish"},
            )
            assert kb.finish_grace_loop_callback(conn, review_task_id=review_id,
                                                 event_id=callback["event_id"], lease_owner="fixture-owner")
        objective = kb.get_grace_objective(conn, objective_id)
        if approval_token:
            objective["waiting_for"] = objective["waiting_for"].replace(approval_token, "$APPROVAL_TOKEN")
        callback = kb.get_grace_loop_callback(conn, review_id)
        return {
            "decision": "accepted", "normalized_contract": normalized,
            "review_outcome": case["review_outcome"],
            "review_evidence_mode": ("synthetic_receipt_no_model_call" if case["review_outcome"] == "accepted"
                                     else "recorded_rejection_no_model_call"),
            "contract_fingerprint": fingerprint,
            "execution_body": execution_body, "review_body": review_body,
            "route": route, "package": kb.latest_run(conn, execution_id).metadata["package"],
            "objective": {key: objective[key] for key in ("status", "current_stage_key", "next_action", "waiting_for")},
            "stages": [list(row) for row in conn.execute(
                "SELECT stage_key,status,outcome_kind FROM grace_objective_stages ORDER BY position")],
            "callback": {key: callback[key] for key in ("state", "outcome_kind", "completion_mode", "objective_id", "stage_key")},
            "task_states": {"execution": kb.get_task(conn, execution_id).status, "review": kb.get_task(conn, review_id).status},
            "successor_task_count": conn.execute("SELECT count(*) FROM tasks WHERE id NOT IN (?,?)", (execution_id, review_id)).fetchone()[0],
            "external_effect_count": conn.execute("SELECT count(*) FROM task_external_effects").fetchone()[0],
            "approval_states": [list(row) for row in conn.execute("SELECT state,count(*) FROM grace_approval_challenges GROUP BY state")],
        }


def comparable_results(results, home):
    """Remove ONLY sandbox-root differences and their derived fingerprint.

    Raw fingerprints remain in raw-semantics.json. Verify those first, then
    recompute a separately named digest of the path-normalized contract.
    """
    from proactive.loop_contract import contract_fingerprint
    for result in results.values():
        if "normalized_contract" in result:
            assert result["contract_fingerprint"] == contract_fingerprint(result["normalized_contract"])
    normalized = json.loads(json.dumps(results, ensure_ascii=False).replace(str(home), "$HERMES_HOME"))
    for result in normalized.values():
        if "normalized_contract" in result:
            result.pop("contract_fingerprint")
            result["canonical_contract_fingerprint"] = contract_fingerprint(result["normalized_contract"])
    return normalized


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New local evidence directory")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    # Isolate BEFORE importing any Hermes code; leave production config untouched.
    replay_path = os.environ.get("PATH", "")
    os.environ.clear()
    os.environ.update(PATH=replay_path, LANG="C.UTF-8", TZ="UTC")
    os.environ["HOME"] = str(args.output)
    os.environ["HERMES_HOME"] = str(args.output / "hermes-home")
    os.environ["HERMES_KANBAN_DB"] = str(args.output / "unused-default.db")
    sys.path.insert(0, str(args.source_root.resolve()))
    try:
        from proactive.behavior_observation import capture
    except ModuleNotFoundError as exc:
        if exc.name != "proactive.behavior_observation":
            raise
        capture = lambda sink: nullcontext()  # Same runner supports the pre-change baseline.
    fixture = load_cases()
    events, results = [], {}
    with capture(events.append):
        for case in fixture["cases"]:
            results[case["id"]] = replay_case(fixture, case, args.output / (case["id"] + ".db"))
    loaded_sources = {}
    for name in ("hermes_cli.kanban_db", "proactive.loop_contract", "proactive.grace_task_compiler"):
        path = Path(sys.modules[name].__file__).resolve()
        assert path.is_relative_to(args.source_root.resolve()), f"Replay imported outside source root: {name}"
        loaded_sources[name] = {"path": str(path), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (args.output / "provenance.json").write_text(json.dumps({
        "fixture_sha256": hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest(),
        "loaded_sources": loaded_sources,
    }, indent=2) + "\n")
    (args.output / "raw-semantics.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    comparable = comparable_results(results, os.environ["HERMES_HOME"])
    (args.output / "semantics.json").write_text(json.dumps(comparable, ensure_ascii=False, indent=2) + "\n")
    (args.output / "observations.jsonl").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
    print(json.dumps({"output": str(args.output), "cases": len(results), "observations": len(events)}))


if __name__ == "__main__":
    main()
