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


def _plan_args(objective_id, revision):
    return {"objective_id": objective_id, "expected_revision": revision,
            "stage_keys": ["prepare", "repair", "execute"],
            "current_stage_key": "repair", "reason": "Repair review handoff"}


@pytest.mark.parametrize("topic", ["4641", "4642", "4643"])
def test_owner_can_plan_existing_objective_without_granting_execution(objective_env, topic):
    objective_env["HERMES_SESSION_THREAD_ID"] = topic
    created = json.loads(control.handle_grace_objective_create(_args()))
    objective_id = created["objective_id"]
    with kb.connect_closing() as conn:
        before = kb.get_grace_objective(conn, objective_id)
        # Accepted predecessor is immutable; the repair is inserted after it.
        conn.execute("UPDATE grace_objective_stages SET status='done',outcome_kind='intermediate',evidence='{}' WHERE objective_id=? AND stage_key='prepare'", (objective_id,))
    result = json.loads(control.handle_grace_objective_plan(_plan_args(objective_id, before["revision"])))
    assert result["status"] == "planned", result
    assert not result["tasks_created"] and not result["external_authority_granted"]
    with kb.connect_closing() as conn:
        after = kb.get_grace_objective(conn, objective_id)
        assert after["revision"] == before["revision"] + 1
        assert after["current_stage_key"] == "repair"
        assert after["acceptance_criteria"] == before["acceptance_criteria"]
        assert after["original_request_sha256"] == before["original_request_sha256"]
        assert conn.execute("SELECT status,outcome_kind,evidence FROM grace_objective_stages WHERE objective_id=? AND stage_key='prepare'", (objective_id,)).fetchone()[:] == ("done", "intermediate", "{}")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM task_external_effects").fetchone()[0] == 0
    assert json.loads(control.handle_grace_objective_plan(_plan_args(objective_id, before["revision"])))["status"] == "rejected"


@pytest.mark.parametrize("mode", ["worker", "callback", "nonowner", "foreign_topic", "private_field", "terminal_replacement"])
def test_plan_rejects_unowned_or_unsafe_changes(objective_env, monkeypatch, mode):
    created = json.loads(control.handle_grace_objective_create(_args()))
    with kb.connect_closing() as conn:
        before = kb.get_grace_objective(conn, created["objective_id"])
    args = _plan_args(created["objective_id"], before["revision"])
    if mode == "worker":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    elif mode == "callback":
        objective_env["HERMES_SESSION_INTERNAL"] = "true"
    elif mode == "nonowner":
        objective_env["HERMES_SESSION_USER_ID"] = "other"
    elif mode == "foreign_topic":
        objective_env["HERMES_SESSION_THREAD_ID"] = "other"
    elif mode == "private_field":
        args["_excluding_delegation_id"] = "skip"
    else:
        args["stage_keys"][-1] = "new_terminal"
    assert json.loads(control.handle_grace_objective_plan(args))["status"] == "rejected"
    with kb.connect_closing() as conn:
        assert kb.get_grace_objective(conn, created["objective_id"])["revision"] == before["revision"]
