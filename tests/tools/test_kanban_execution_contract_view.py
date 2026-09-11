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
    if mode in {"explicit_full", "other_default"}:
        assert result["task"]["body"] == body
        assert "worker_context" in result
    else:
        assert len(raw) < 6000
        assert result["view"] == "contract"
        assert result["task"]["stage"] == stage
        assert result["contract"] == {k:v for k,v in contract.items() if k != "policy_snapshots"}
        assert result["policy_references"] == [{"policy_id":"p1","version":"v1","sha256":"a"*64}]
        assert "managed_policy_read" in result["policy_read_instruction"]
        assert policy_text not in raw
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, worker_env).body == body
