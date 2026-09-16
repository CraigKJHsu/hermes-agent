import pytest
from hermes_cli import kanban_db as kb
from proactive.behavior_profiles import registry as br
from proactive.policy_registry import bind_topic_policies, create_policy_version
from scripts.replay_behavior_observation import load_cases, make_contract

@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()

def setup_objective(project="ai_bizweek", *, enabled=True, oid="go_profile", project_namespace=None):
    case = load_cases()["cases"][0 if project == "ai_bizweek" else 2]
    contract = make_contract(load_cases(), case)
    contract["objective_ref"]["objective_id"] = oid
    identity = contract["identity"]
    project_namespace = project_namespace or project
    identity["project"] = project_namespace
    namespace = f"telegram:{identity['chat_id']}:{identity['thread_id']}/{project_namespace}"
    contract["memory"]["namespace"] = namespace
    create_policy_version(project + "-policy", "v1", "Original complete policy", owner_scope="topic",
                          owner_id=project, activate=True)
    bind_topic_policies(namespace, [{"policy_id": project + "-policy", "resolution": "latest_active"}])
    with kb.connect_closing() as conn:
        if enabled:
            br.set_selection(conn, platform="telegram", chat_id=identity["chat_id"],
                             thread_id=identity["thread_id"], project=project_namespace, profile_id=project,
                             version="v8", expected_revision=0, reason="Local canary")
        kb.create_grace_objective(conn, objective_id=oid, platform="telegram", chat_id=identity["chat_id"],
                                  thread_id=identity["thread_id"], session_key="fixture", title="fixture",
                                  objective="fixture", original_request_sha256="a" * 64,
                                  required_stage_keys=["prepare", "publish"], terminal_stage_key="publish",
                                  acceptance_criteria=["verified"], behavior_project=project_namespace)
        return br.bind_contract(conn, contract)


@pytest.mark.parametrize("state,stage_status,outcome,owner,allowed", [
    ("cancelled", "done", "superseded_by_retry", None, True),
    ("cancelled", "done", "cancelled", None, True),
    ("cancelled", "done", "intermediate_blocked", None, True),
    ("cancelled", "blocked", "intermediate_blocked", None, False),
    ("cancelled", "done", "intermediate_blocked", "active-lease", False),
    ("attention", "done", "continued", None, True),
    ("attention", "done", "continued", "active-lease", False),
    ("cancelled", "blocked", "superseded_by_retry", None, False),
    ("cancelled", "done", "accepted", None, False),
    ("cancelled", "done", "superseded_by_retry", "active-lease", False),
    ("pending", "done", "superseded_by_retry", None, False),
])
@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
def test_migration_preserves_superseded_cancelled_callback(state, stage_status, outcome, owner, allowed, project):
    contract = setup_objective(project)
    with kb.connect_closing() as conn:
        conn.execute("UPDATE grace_objective_stages SET status=?,outcome_kind=? WHERE objective_id='go_profile' AND stage_key='prepare'", (stage_status, outcome))
        conn.execute("""INSERT INTO grace_loop_callbacks
            (review_task_id,execution_task_id,platform,chat_id,thread_id,contract_fingerprint,
             state,created_at,objective_id,stage_key,lease_owner)
            VALUES ('review','execution','telegram',?,?,'fingerprint',?,1,'go_profile','prepare',?)""",
            (contract['identity']['chat_id'],contract['identity']['thread_id'],state,owner))
        before = dict(conn.execute("SELECT * FROM grace_loop_callbacks WHERE review_task_id='review'").fetchone())
        pin = br.get_pin(conn, 'go_profile')
        args = dict(objective_id='go_profile',platform='telegram',chat_id=contract['identity']['chat_id'],
            thread_id=contract['identity']['thread_id'],profile_id=project,version='v8',
            expected_revision=1,expected_pin_hash=br.digest(pin),reason='Preserve cancelled history',apply=True)
        if allowed:
            result = br.migrate_objective(conn, **args)
            assert result['next_pin']['generation'] == pin['generation'] + 1
        else:
            with pytest.raises(br.BehaviorProfileError, match='migration_callback_pending'):
                br.migrate_objective(conn, **args)
            assert br.get_pin(conn, 'go_profile') == pin
        assert dict(conn.execute("SELECT * FROM grace_loop_callbacks WHERE review_task_id='review'").fetchone()) == before
