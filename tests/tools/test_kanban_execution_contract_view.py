import hashlib
import json
import pytest
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt
from tests.tools.test_kanban_tools import worker_env

@pytest.mark.parametrize("topic", ["2", "4641"])
@pytest.mark.parametrize("mode", ["own_default", "explicit_contract", "explicit_full", "other_default", "review_contract"])
def test_large_policy_does_not_hide_execution_scope(worker_env, monkeypatch, topic, mode):
    policy_text = "完整政策，不是工作目標。" * 12000
    contract = {
        "identity": {"thread_id": topic},
        "goal": {"objective": "Reconcile only Touban post 531289396730654_122182365038694189"},
        "scope": {"allowed": ["existing receipt reconcile:run2159"], "forbidden": ["No Facebook writes; no Carter case"]},
        "verification": {"checks": ["Compare exact source run2159"]},
        "memory": {"working": ["Source file /tmp/published.json"]},
        "original_request": "Complete this exact case", "domain_memory": {"mode": "mutate"},
        "policy_snapshots": [{"policy_id": "p1", "version": "v1", "sha256": "a" * 64, "content": policy_text}],
    }
    stage = "review" if mode == "review_contract" else "execution"
    body = f"GRACE_LOOP_CONTRACT_STAGE: {'grace_review' if stage == 'review' else stage}\n```json\n{json.dumps(contract, ensure_ascii=False)}\n```"
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, worker_env))
    args = {"task_id": worker_env}
    if mode in {"explicit_contract", "review_contract"}: args["view"] = "contract"
    if mode == "explicit_full": args["view"] = "full"
    if mode == "other_default": monkeypatch.setenv("HERMES_KANBAN_TASK", "another-task")
    raw = kt._handle_show(args)
    result = json.loads(raw)
    if mode == "explicit_full":
        assert result["task"]["body"].startswith("GRACE_LOOP_CONTRACT_STAGE:")
        assert "<truncated chars=" in result["task"]["body"]
        assert "worker_context" in result
    elif mode == "other_default":
        assert result["view"] == "summary"
        assert "body" not in result["task"]
    else:
        assert len(raw) < 20_000
        assert result["view"] == "contract_chunk"
        assert result["task"]["stage"] == stage
        assert result["policy_references"] == [{"policy_id":"p1","version":"v1","sha256":"a"*64}]
        chunks = [result["contract_json_chunk"]]
        while not result["complete"]:
            result = json.loads(kt._handle_show({
                "task_id": worker_env,
                "view": "contract",
                "offset": result["next_offset"],
            }))
            chunks.append(result["contract_json_chunk"])
        contract_json = "".join(chunks)
        assert json.loads(contract_json) == contract
        assert hashlib.sha256(contract_json.encode("utf-8")).hexdigest() == result[
            "contract_json_sha256"
        ]
        assert policy_text in contract_json
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, worker_env).body == body


def test_grace_review_can_inspect_blocked_parent_draft(worker_env, monkeypatch):
    report = {
        "kind": "content_package",
        "delivery": "inline_with_attachment",
        "complete": False,
        "title": "Blocked package draft",
        "body": "Complete inline body.",
        "observed_at": 1_789_183_500,
        "assets": [{"filename": "hero.png", "path": "/tmp/hero.png", "sha256": "a" * 64}],
    }
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="blocked execution")
        run_id = conn.execute(
            "INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at,metadata) "
            "VALUES(?,?,?,?,?,?)",
            (
                parent,
                "blocked",
                "blocked",
                1_789_183_400,
                1_789_183_500,
                json.dumps(
                    {
                        "loop_contract_blocked_result": {
                            "status": "succeeded",
                            "summary": "Draft produced before controller rejection.",
                            "artifacts": ["/tmp/draft-source.png"],
                            "acceptanceEvidence": {"source_integrity": "verified"},
                            "externalEffects": [],
                            "metadata": {"user_facing_report": report},
                        }
                    }
                ),
            ),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET status='blocked',block_kind='capability' WHERE id=?",
            (parent,),
        )
        conn.execute(
            "UPDATE tasks SET body=? WHERE id=?",
            ("GRACE_LOOP_CONTRACT_STAGE: grace_review\n{}", worker_env),
        )
        conn.execute(
            "INSERT INTO task_links(parent_id,child_id) VALUES(?,?)",
            (parent, worker_env),
        )

    result = json.loads(kt._handle_show({"task_id": worker_env}))

    evidence = result["parent_evidence"][0]
    assert evidence["run_id"] == run_id
    assert evidence["evidence_mode"] == "blocked_draft"
    assert evidence["summary"] == "Draft produced before controller rejection."
    assert evidence["acceptance_evidence"] == {"source_integrity": "verified"}
    assert evidence["external_effects"] == []
    assert evidence["user_facing_report"] == report
    assert evidence["review_evidence"]["artifacts"] == ["/tmp/draft-source.png"]
    assert evidence["declared_artifacts"] == ["/tmp/draft-source.png"]
