"""Real SQLite regressions for operator correction recovery across Topics."""

import hashlib
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.review_correction_recovery import recover_correction


@pytest.fixture
def pair(tmp_path):
    db = tmp_path / "board.db"
    with kb.connect_closing(db) as conn:
        execution = kb.create_task(
            conn,
            title="Execution",
            body="GRACE_LOOP_CONTRACT_STAGE: execution\nOriginal sealed scope.",
        )
        review = kb.create_task(
            conn,
            title="Review",
            body="GRACE_LOOP_CONTRACT_STAGE: grace_review\nCheck original scope.",
            parents=(execution,),
        )
        contract = json.dumps({"external_effect_budget": 0, "external_targets": []})
        conn.execute(
            "UPDATE tasks SET status='done',completed_at=2 WHERE id=?", (execution,)
        )
        parent_id = conn.execute(
            "INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) VALUES (?,'done','completed',1,2,?)",
            (execution, json.dumps({"external_effects": []})),
        ).lastrowid
        parent = kb.get_run(conn, parent_id)
        source = {
            "parent_execution_task_id": execution,
            "parent_execution_run_id": parent_id,
            "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
                parent
            ),
            "parent_task_body_sha256": hashlib.sha256(
                kb.get_task(conn, execution).body.encode()
            ).hexdigest(),
            "review_task_body_sha256": hashlib.sha256(
                kb.get_task(conn, review).body.encode()
            ).hexdigest(),
        }
        rejection_id = conn.execute(
            "INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) VALUES (?,'blocked','blocked',3,4,?)",
            (
                review,
                json.dumps({
                    "review_outcome": "rejected",
                    "workflow_review_source": source,
                }),
            ),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status='blocked',block_kind='needs_input',block_recurrences=1 WHERE id=?",
            (review,),
        )
        event_id = conn.execute(
            "INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES (?,?,'blocked',?,4)",
            (
                review,
                rejection_id,
                json.dumps({
                    "reason": "Translate wrapper; preserve original quoted section and recalculate digests.",
                    "kind": "needs_input",
                    "recurrences": 1,
                }),
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO grace_delegations(delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,session_key,session_id,resolved_route,approval_required,state,execution_task_id,review_task_id,created_at,updated_at,contract_snapshot) VALUES ('gd-test',?,'gri-test','telegram','chat','4641','lane','session-test','{}',0,'queued',?,?,1,4,?)",
            ("a" * 64, execution, review, contract),
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review,
            execution_task_id=execution,
            platform="telegram",
            chat_id="chat",
            thread_id="4641",
            contract_fingerprint="a" * 64,
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='delivered',last_event_id=? WHERE review_task_id=?",
            (event_id, review),
        )
        conn.commit()
        args = dict(
            review_task_id=review,
            execution_run_id=parent_id,
            review_run_id=rejection_id,
            blocked_event_id=event_id,
            platform="telegram",
            chat_id="chat",
            thread_id="4641",
            contract_sha256=hashlib.sha256(contract.encode()).hexdigest(),
            operator_evidence="User requested control-plane repair; no missing human decision; content correction stays with Grace.",
        )
        yield conn, args, execution, review


@pytest.mark.parametrize("topic", ["4641", "other-topic"])
def test_recovery_preserves_original_pair_evidence_and_scope(pair, topic):
    # Topic 4641 is the historical failure sample, not special-case behavior.
    conn, args, execution, review = pair
    conn.execute("UPDATE grace_delegations SET thread_id=?", (topic,))
    conn.execute("UPDATE grace_loop_callbacks SET thread_id=?", (topic,))
    conn.commit()
    args["thread_id"] = topic
    before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    original_run = dict(
        conn.execute(
            "SELECT * FROM task_runs WHERE id=?", (args["review_run_id"],)
        ).fetchone()
    )
    result = recover_correction(conn, **args)
    assert result["correction_attempt"] == 1
    assert kb.get_task(conn, execution).status == "ready"
    assert kb.get_task(conn, review).status == "todo"
    assert kb.get_task(conn, review).block_recurrences == 1
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before
    assert not conn.execute("SELECT 1 FROM grace_objectives").fetchone()
    assert (
        dict(
            conn.execute(
                "SELECT * FROM task_runs WHERE id=?", (args["review_run_id"],)
            ).fetchone()
        )
        == original_run
    )
    with pytest.raises(ValueError, match="state changed"):
        recover_correction(conn, **args)
    # A second actual rejected correction must still reach triage.
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    assert kb.unblock_task(conn, review) is False
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    conn.commit()
    assert kb.block_task(
        conn,
        review,
        reason="Still rejected",
        kind="dependency",
        review_outcome="rejected",
    )
    assert kb.get_task(conn, review).status == "triage"
    assert kb.get_task(conn, execution).status == "done"


@pytest.mark.parametrize(
    "fault",
    [
        "run",
        "contract",
        "lane",
        "source",
        "effect",
        "review_effect",
        "callback_fingerprint",
        "callback_objective",
        "graph_removed",
        "graph_added",
        "sibling_child",
        "execution_parent",
        "review_child",
        "callback",
        "limit",
        "triage",
    ],
)
def test_recovery_rejects_changed_authority_and_rolls_back(pair, fault):
    conn, args, execution, review = pair
    if fault == "run":
        args["execution_run_id"] += 10
    elif fault == "contract":
        args["contract_sha256"] = "b" * 64
    elif fault == "lane":
        args["thread_id"] = "wrong-topic"
    elif fault == "source":
        conn.execute("UPDATE tasks SET body=body||' changed' WHERE id=?", (execution,))
    elif fault == "effect":
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (
                json.dumps({"external_effects": [{"state": "verified"}]}),
                args["execution_run_id"],
            ),
        )
    elif fault == "review_effect":
        metadata = json.loads(
            conn.execute(
                "SELECT metadata FROM task_runs WHERE id=?", (args["review_run_id"],)
            ).fetchone()[0]
        )
        metadata["external_effects"] = [{"state": "verified"}]
        conn.execute(
            "UPDATE task_runs SET metadata=? WHERE id=?",
            (json.dumps(metadata), args["review_run_id"]),
        )
    elif fault == "execution_parent":
        other = kb.create_task(conn, title="New unresolved dependency")
        conn.execute(
            "INSERT INTO task_links(parent_id,child_id) VALUES (?,?)",
            (other, execution),
        )
    elif fault == "review_child":
        kb.create_task(conn, title="Other review dependent", parents=(review,))
    elif fault == "sibling_child":
        kb.create_task(conn, title="Other execution dependent", parents=(execution,))
    elif fault == "graph_removed":
        conn.execute("DELETE FROM task_links WHERE child_id=?", (review,))
    elif fault == "graph_added":
        sibling = kb.create_task(conn, title="Unrelated parent")
        conn.execute(
            "INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (sibling, review)
        )
    elif fault == "callback_fingerprint":
        conn.execute(
            "UPDATE grace_loop_callbacks SET contract_fingerprint=?", ("b" * 64,)
        )
    elif fault == "callback_objective":
        conn.execute("UPDATE grace_loop_callbacks SET objective_id='unrelated'")
    elif fault == "callback":
        conn.execute("UPDATE grace_loop_callbacks SET state='delivering'")
    elif fault == "triage":
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (review,))
    elif fault == "limit":
        conn.execute(
            "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'grace_correction_requested',?,1)",
            (execution, json.dumps({"review_task_id": review})),
        )
    conn.commit()
    before = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
    with pytest.raises(ValueError):
        recover_correction(conn, **args)
    assert kb.get_task(conn, execution).status == "done"
    assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == before


def test_recovery_rolls_back_partial_transition(pair, monkeypatch):
    conn, args, execution, review = pair

    def fail_event(*args, **kwargs):
        raise ValueError("transition failed after state updates")

    monkeypatch.setattr(kb, "_append_event", fail_event)
    with pytest.raises(ValueError, match="transition failed"):
        recover_correction(conn, **args)
    assert kb.get_task(conn, review).status == "blocked"
    assert kb.get_task(conn, execution).status == "done"


@pytest.mark.parametrize(
    "env", ["HERMES_KANBAN_TASK", "HERMES_SESSION_INTERNAL", "HERMES_SESSION_SOURCE"]
)
def test_workers_and_callbacks_cannot_recover(pair, monkeypatch, env):
    conn, args, execution, review = pair
    monkeypatch.setenv(env, "cron" if env == "HERMES_SESSION_SOURCE" else "true")
    with pytest.raises(ValueError, match="local operator"):
        recover_correction(conn, **args)
    assert kb.get_task(conn, review).status == "blocked"
