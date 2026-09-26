import json

import pytest

from hermes_cli import kanban_db as kb
from plugins.openclaw_bridge import objective_control as control


@pytest.fixture
def objective_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_WORKER_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    values = {
        "HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "chat",
        "HERMES_SESSION_THREAD_ID": "1", "HERMES_SESSION_USER_ID": "owner",
        "HERMES_SESSION_OWNER_USER_ID": "owner", "HERMES_SESSION_MESSAGE_ID": "42",
        "HERMES_SESSION_MESSAGE_TEXT": "\n  Please plan and verify the connection.\n",
        "HERMES_SESSION_KEY": "agent:main:telegram:group:chat:1",
    }
    monkeypatch.setattr(control, "get_session_env", lambda k, default="": values.get(k, default))
    monkeypatch.setattr(control, "resolve_thread_context", lambda **kw: {"project": "test"})
    return values


def _args():
    return {"title": "Connection check", "stage_keys": ["prepare", "execute"],
            "acceptance_criteria": ["Verified connection with evidence"]}


def test_grace_creates_ordered_objective_before_any_execution(objective_env):
    result = json.loads(control.handle_grace_objective_create(_args()))
    assert result["status"] == "created"
    assert [s["stage_key"] for s in result["stages"]] == ["prepare", "execute"]
    with kb.connect() as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 0
        objective = kb.get_grace_objective(conn, result["objective_id"])
        assert objective["objective"] == objective_env["HERMES_SESSION_MESSAGE_TEXT"]
        assert objective["terminal_stage_key"] == "execute"
    replay = json.loads(control.handle_grace_objective_create(_args()))
    assert replay["objective_id"] == result["objective_id"]
    changed = _args(); changed["stage_keys"] = ["different"]
    assert json.loads(control.handle_grace_objective_create(changed))["status"] == "rejected"


@pytest.mark.parametrize("mode", ["worker", "worker_run_only", "callback", "cron", "nonowner", "missing_source"])
def test_non_grace_origin_cannot_materialize_objective(objective_env, monkeypatch, mode):
    if mode == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "task")
    elif mode == "worker_run_only":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    elif mode == "callback":
        objective_env["HERMES_SESSION_INTERNAL"] = "true"
    elif mode == "cron":
        objective_env["HERMES_SESSION_SOURCE"] = "cron"
    elif mode == "nonowner":
        objective_env["HERMES_SESSION_USER_ID"] = "other"
    else:
        objective_env["HERMES_SESSION_MESSAGE_TEXT"] = ""
    assert json.loads(control.handle_grace_objective_create(_args()))["status"] == "rejected"
    with kb.connect() as conn:
        assert conn.execute("select count(*) from grace_objectives").fetchone()[0] == 0
