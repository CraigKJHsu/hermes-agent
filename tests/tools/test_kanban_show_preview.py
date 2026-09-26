import json
from tests.tools.test_kanban_tools import worker_env

def test_preview_recovery(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    body = "large-contract " * 15000
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, worker_env))
        conn.commit()
        kb.add_comment(conn, worker_env, "worker", "old blocker " * 2000)
        kb.add_comment(conn, worker_env, "operator", "Readback source: task-scoped live Kanban DB")
    output = kt._handle_show({"view": "full"})
    assert "Readback source: task-scoped live Kanban DB" in output[:1500]
    shown = json.loads(output)
    assert shown["task"]["body"].startswith("large-contract ")
    assert "<truncated chars=" in shown["task"]["body"]
    assert len(shown["comments"]) == 2
    assert "<truncated chars=" in shown["comments"][0]["body"]
    assert "worker_context" in shown and "runs" in shown
    assert shown["latest_comment"] == shown["comments"][-1]
