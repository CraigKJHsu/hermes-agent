"""Version pins exercise real SQLite, policy files, validator and compiler paths."""
from copy import deepcopy
import hashlib
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from proactive.behavior_profiles import registry as br
from proactive.grace_task_compiler import render_execution_body, render_review_body
from proactive.loop_contract import validate_loop_contract
from proactive.policy_registry import (
    bind_topic_policies, create_policy_version, resolve_task_policy_snapshots,
    validate_policy_completion,
)
from scripts.replay_behavior_observation import load_cases, make_contract, replay_case


CURRENT_VERSION = "v50"


@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
def test_current_kernel_seals_every_declared_source(project):
    manifest = br.profile(project, CURRENT_VERSION)
    kernel = json.loads((br.ROOT / f"kernel-v{manifest['safety_kernel_version']}.json").read_text())
    assert br.digest(kernel) == manifest["safety_kernel_hash"]
    for relative_path, expected in kernel.items():
        assert expected == hashlib.sha256((br.CODE_ROOT / relative_path).read_bytes()).hexdigest(), relative_path


@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
def test_current_browser_readonly_route_carries_pinned_url_authority(project):
    from proactive.grace_task_compiler import _browser_readonly_url
    from proactive.hubops_routing import route_clawops_objective

    contract = validate_loop_contract(setup_objective(project))
    routed = route_clawops_objective(
        "Read one exact authorized page",
        project=project,
        task_type="browser_readonly",
        risk_level="low",
        approved=True,
        behavior_contract=contract,
    )

    expected = [
        "https://example.com/",
        "https://www.linkedin.com/in/craig-k-j-hsu-6012b815",
    ]
    assert routed["status"] == "routed"
    assert routed["assignment"]["allowed_urls"] == expected
    assert routed["backend_role_card"]["allowed_urls"] == expected
    assert "http://127.0.0.1:8766/" not in expected

    delegated = deepcopy(contract)
    delegated["scope"]["allowed"] = ["Read only https://example.com/"]
    delegated["routing"]["resolved"] = routed
    assert _browser_readonly_url(delegated) == "https://example.com/"


def test_current_ai_bizweek_review_uses_controller_package_authority():
    contract = validate_loop_contract(setup_objective("ai_bizweek"))

    body = render_review_body(contract, "t_execution")

    assert "Controller content-package readback is authoritative" in body
    assert "Markdown body artifact increases the total attachment-row count" in body
    assert "resized thumbnail is an optional inspection aid" in body
    assert "`Controller state conflict:`" in body
    assert "contains facebook_page_post.text exactly equal to" in body
    assert "user_facing_report.sections.facebook_page_post" in body
    execution = render_execution_body(contract)
    assert "metadata.facebook_page_post={text:<exact Page body>}" in execution
    assert "match metadata.user_facing_report.sections.facebook_page_post" in execution
    assert "pinned_version_verified=true" in body
    assert "Read and obey the complete content of every policy snapshot before work." in execution


def setup_objective(project="ai_bizweek", *, enabled=True, oid="go_profile", project_namespace=None, version=CURRENT_VERSION):
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
                             version=version, expected_revision=0, reason="Local canary")
        kb.create_grace_objective(conn, objective_id=oid, platform="telegram", chat_id=identity["chat_id"],
                                  thread_id=identity["thread_id"], session_key="fixture", title="fixture",
                                  objective="fixture", original_request_sha256="a" * 64,
                                  required_stage_keys=["prepare", "publish"], terminal_stage_key="publish",
                                  acceptance_criteria=["verified"], behavior_project=project_namespace)
        return br.bind_contract(conn, contract)


@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
def test_pin_survives_policy_activation_and_uses_actual_versioned_compiler(project):
    contract = setup_objective(project)
    normalized = validate_loop_contract(contract)
    before = render_execution_body(normalized)
    create_policy_version(project + "-policy", "v2", "Changed complete policy", owner_scope="topic",
                          owner_id=project, activate=True, expected_active_version="v1")
    assert validate_loop_contract(normalized) == normalized
    assert render_execution_body(normalized) == before
    loaded = resolve_task_policy_snapshots(before)
    assert loaded["policies"][0]["content"] == "Original complete policy"
    receipt = {**{k: loaded["policies"][0][k] for k in ("policy_id", "version", "sha256")},
               "role": "review", "loaded": True, "pinned_version_verified": True}
    validate_policy_completion(render_review_body(normalized, "t_parent"), {"policy_receipts": [receipt]}, role="review")
    assert br.implementation(normalized, "compiler").__name__.endswith(f".{br.profile(project, CURRENT_VERSION)['bundle']}.compiler")


def test_contract_cannot_remove_or_replace_pin_or_change_topic():
    contract = setup_objective()
    for edit in (lambda c: c.pop("behavior_pin"),
                 lambda c: c["behavior_pin"].update(behavior_profile_version="v99"),
                 lambda c: c["identity"].update(thread_id="elsewhere")):
        altered = deepcopy(contract)
        edit(altered)
        with pytest.raises(br.BehaviorProfileError):
            validate_loop_contract(altered)


def test_disabling_canary_does_not_relabel_existing_objective():
    contract = setup_objective()
    identity = contract["identity"]
    with kb.connect_closing() as conn:
        before = br.get_pin(conn, "go_profile")
        br.set_selection(conn, platform="telegram", chat_id=identity["chat_id"],
                         thread_id=identity["thread_id"], project="ai_bizweek", profile_id=None,
                         version=None, expected_revision=1, reason="Rollback new admissions")
        assert br.get_pin(conn, "go_profile") == before
        with pytest.raises(br.BehaviorProfileError, match="selection_cas"):
            br.set_selection(conn, platform="telegram", chat_id=identity["chat_id"],
                             thread_id=identity["thread_id"], project="ai_bizweek", profile_id=None,
                             version=None, expected_revision=1, reason="Stale concurrent operator")
    assert validate_loop_contract(contract)["behavior_pin"] == before


def _drift_kernel(tmp_path, monkeypatch):
    import shutil

    manifest = br.profile("ai_bizweek", CURRENT_VERSION)
    kernel = json.loads((br.ROOT / f"kernel-v{manifest['safety_kernel_version']}.json").read_text())
    source_root = tmp_path / "kernel-source"
    for relative in kernel:
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(br.CODE_ROOT / relative, target)
    changed = {"proactive/grace_task_compiler.py", "proactive/hubops_routing.py", "hermes_cli/kanban_db.py"}
    for relative in changed:
        with (source_root / relative).open("a") as target:
            target.write("\n# Simulated unsealed release change.\n")
    monkeypatch.setattr(br, "CODE_ROOT", source_root)
    return changed


@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
@pytest.mark.parametrize("fault", ["kernel", "runtime"])
def test_selection_rejects_unhealthy_release_without_changing_revision(tmp_path, monkeypatch, project, fault):
    contract = setup_objective(project)
    identity = contract["identity"]
    if fault == "kernel":
        _drift_kernel(tmp_path, monkeypatch)
    else:
        monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", bytes(32))
    with kb.connect_closing() as conn:
        before = tuple(conn.execute("SELECT * FROM grace_behavior_selections").fetchone())
        spec = dict(platform="telegram", chat_id=identity["chat_id"], thread_id=identity["thread_id"],
                    project=project, expected_revision=1, reason="Candidate release")
        with pytest.raises(br.BehaviorProfileError, match="kernel_migration_required|runtime_reload_required"):
            br.set_selection(conn, **spec, profile_id=project, version=CURRENT_VERSION)
        assert tuple(conn.execute("SELECT * FROM grace_behavior_selections").fetchone()) == before
        disabled = br.set_selection(conn, **spec, profile_id=None, version=None)
        assert disabled["revision"] == 2
        assert conn.execute("SELECT profile_id FROM grace_behavior_selections").fetchone()[0] is None


@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
def test_kernel_failure_at_birth_leaves_no_objective_or_cards(tmp_path, monkeypatch, project):
    contract = setup_objective(project)
    identity = contract["identity"]
    _drift_kernel(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        before_tasks = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with pytest.raises(br.BehaviorProfileError, match="kernel_migration_required"):
            kb.create_grace_objective(
                conn, objective_id="go_unsealed", platform="telegram", chat_id=identity["chat_id"],
                thread_id=identity["thread_id"], session_key="fixture", title="fixture", objective="fixture",
                original_request_sha256="b" * 64, required_stage_keys=["prepare", "publish"],
                terminal_stage_key="publish", acceptance_criteria=["verified"], behavior_project=project,
            )
        assert kb.get_grace_objective(conn, "go_unsealed") is None
        assert br.get_pin(conn, "go_unsealed") is None
        assert conn.execute("SELECT count(*) FROM grace_objective_stages WHERE objective_id='go_unsealed'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM grace_delegations WHERE objective_id='go_unsealed'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before_tasks


def test_health_reports_all_kernel_drift_and_nonterminal_pins_read_only(tmp_path, monkeypatch, capsys):
    import sys
    from hermes_cli.behavior_profiles import main

    for project in ("ai_bizweek", "secondhand_commerce"):
        setup_objective(project, oid="go_" + project)
    with kb.connect_closing() as conn:
        pin = br.get_pin(conn, "go_ai_bizweek")
        pin["behavior_bundle_hash"] = "0" * 64
        conn.execute("UPDATE grace_objective_behavior_pins SET pin=? WHERE objective_id=?",
                     (json.dumps(pin), "go_ai_bizweek"))
        conn.execute("UPDATE grace_objectives SET status='blocked' WHERE objective_id='go_ai_bizweek'")
    changed = _drift_kernel(tmp_path, monkeypatch)
    path = kb.kanban_db_path()
    before = path.read_bytes()
    monkeypatch.setattr(kb, "connect_closing", lambda **_kwargs: pytest.fail("health must not open a writable connection"))
    monkeypatch.setattr(sys, "argv", ["behavior_profiles", "health"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["runtime"]["scope"] == "calling_process"
    assert report["runtime"]["ok"] is True
    assert len(report["selections"]) == 2
    assert {row["objective_id"] for row in report["objectives"]} == {"go_ai_bizweek", "go_secondhand_commerce"}
    for row in report["selections"] + report["objectives"]:
        assert {item["path"] for item in row["health"]["kernel_mismatches"]} == changed
    old = next(row for row in report["objectives"] if row["objective_id"] == "go_ai_bizweek")
    assert "behavior.manifest_mismatch" in old["health"]["errors"]
    assert path.read_bytes() == before


def test_health_candidate_does_not_open_board_and_missing_board_is_not_created(tmp_path, monkeypatch, capsys):
    import sys
    from hermes_cli.behavior_profiles import main

    monkeypatch.setattr(kb, "kanban_db_path", lambda **_kwargs: pytest.fail("candidate must not resolve a board"))
    monkeypatch.setattr(sys, "argv", ["behavior_profiles", "health", "--profile", "ai_bizweek", "--version", CURRENT_VERSION])
    main()
    assert json.loads(capsys.readouterr().out)["ok"] is True
    missing = tmp_path / "absent-board" / "kanban.db"
    monkeypatch.setattr(kb, "kanban_db_path", lambda **_kwargs: missing)
    monkeypatch.setattr(sys, "argv", ["behavior_profiles", "health"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert "board_unavailable" in report["errors"][0]
    assert not missing.parent.exists()


def test_shadow_requires_explicit_candidate_before_creating_output(tmp_path, monkeypatch):
    import sys
    from scripts.shadow_behavior_profiles import main

    output = tmp_path / "not-created"
    monkeypatch.setattr(sys, "argv", ["shadow_behavior_profiles", "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not output.exists()


def test_legacy_objective_remains_unpinned():
    contract = setup_objective(enabled=False)
    assert "behavior_pin" not in contract
    with kb.connect_closing() as conn:
        assert br.get_pin(conn, "go_profile") is None
    assert "behavior_pin" not in validate_loop_contract(contract)


def test_migration_cas_and_rollback_keep_history_and_reject_old_task_generation():
    contract = setup_objective()
    normalized = validate_loop_contract(contract)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="old generation", body=render_execution_body(normalized))
        kb.block_task(conn, task_id, reason="Paused for migration", kind="needs_input")
        saved_body = kb.get_task(conn, task_id).body
        pin = br.get_pin(conn, "go_profile")
        spec = dict(objective_id="go_profile", platform="telegram", chat_id=contract["identity"]["chat_id"],
                    thread_id=contract["identity"]["thread_id"], profile_id="ai_bizweek", version=CURRENT_VERSION,
                    expected_revision=1, expected_pin_hash=br.digest(pin), reason="Explicit compatibility checkpoint")
        preview = br.migrate_objective(conn, **spec)
        assert preview["applied"] is False
        assert br.get_pin(conn, "go_profile") == pin
        result = br.migrate_objective(conn, **spec, apply=True)
        assert result["next_pin"]["generation"] == 2
        assert kb.get_task(conn, task_id).body == saved_body
        assert resolve_task_policy_snapshots(saved_body)["policies"][0]["content"] == "Original complete policy"
        with pytest.raises(br.BehaviorProfileError):
            br.validate_task_policy_completion(saved_body, {"policy_receipts": []}, "execution")
        with pytest.raises(br.BehaviorProfileError):
            kb.claim_task(conn, task_id)
        with pytest.raises(br.BehaviorProfileError, match="migration_cas"):
            br.migrate_objective(conn, **spec, apply=True)
        restored = br.migrate_objective(conn, **{**spec, "expected_revision": 2,
            "expected_pin_hash": br.digest(result["next_pin"]), "rollback_generation": 2}, apply=True)
        assert restored["next_pin"]["generation"] == 3
        assert restored["next_pin"]["policy_snapshot_hash"] == pin["policy_snapshot_hash"]
        assert conn.execute("SELECT count(*) FROM grace_behavior_migrations").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM task_external_effects").fetchone()[0] == 0


def test_migration_treats_terminal_callback_review_as_idle():
    contract = setup_objective(version=CURRENT_VERSION)
    with kb.connect_closing() as conn:
        review_id = kb.create_task(
            conn,
            title="finished dependency review",
            body=render_review_body(contract, "t_execution"),
        )
        conn.execute(
            "INSERT INTO task_runs(task_id,status,outcome,started_at,ended_at) "
            "VALUES(?,?,?,?,?)",
            (review_id, "blocked", "blocked", 1_789_183_400, 1_789_183_500),
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',outcome_kind='continued',"
            "review_task_id=? WHERE objective_id='go_profile' AND stage_key='prepare'",
            (review_id,),
        )
        conn.execute(
            "UPDATE tasks SET status='todo',block_kind='dependency' WHERE id=?",
            (review_id,),
        )
        conn.execute(
            """INSERT INTO grace_loop_callbacks
                (review_task_id,execution_task_id,platform,chat_id,thread_id,
                 contract_fingerprint,state,created_at,objective_id,stage_key,
                 outcome_kind)
                VALUES (?,'t_execution','telegram',?,?,'fingerprint','delivered',1,
                        'go_profile','prepare','continued')""",
            (
                review_id, contract["identity"]["chat_id"],
                contract["identity"]["thread_id"],
            ),
        )
        pin = br.get_pin(conn, "go_profile")

        result = br.migrate_objective(
            conn,
            objective_id="go_profile",
            platform="telegram",
            chat_id=contract["identity"]["chat_id"],
            thread_id=contract["identity"]["thread_id"],
            profile_id="ai_bizweek",
            version=CURRENT_VERSION,
            expected_revision=1,
            expected_pin_hash=br.digest(pin),
            reason="Finished dependency review is not in flight",
            apply=True,
        )

        assert result["next_pin"]["generation"] == 2


def test_migrated_system_checkpoint_can_replay_historical_tasks():
    contract = setup_objective(version=CURRENT_VERSION)
    payload = {
        "action": "confirm_content_package",
        "platform": "internal",
        "scope": {},
        "exact_question": "Confirm the already delivered package.",
        "next_stage_key": "publish",
    }
    with kb.connect_closing() as conn:
        execution_id = kb.create_task(
            conn,
            title="historical checkpoint execution",
            body=render_execution_body(contract),
        )
        review_id = kb.create_task(
            conn,
            title="historical checkpoint review",
            body=render_review_body(contract, execution_id),
            parents=(execution_id,),
        )
        conn.execute(
            "UPDATE tasks SET status='done' WHERE id IN (?,?)",
            (execution_id, review_id),
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review_id,
            execution_task_id=execution_id,
            platform="telegram",
            chat_id=contract["identity"]["chat_id"],
            thread_id=contract["identity"]["thread_id"],
            contract_fingerprint="c" * 64,
            objective_id="go_profile",
            stage_key="prepare",
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',"
            "outcome_kind='approval_blocked' "
            "WHERE objective_id='go_profile' AND stage_key='prepare'"
        )
        conn.execute(
            "UPDATE grace_objective_stages SET status='waiting_approval' "
            "WHERE objective_id='go_profile' AND stage_key='publish'"
        )
        conn.execute(
            "UPDATE grace_objectives SET status='waiting_approval',"
            "current_stage_key='publish' WHERE objective_id='go_profile'"
        )
        conn.execute(
            "UPDATE grace_loop_callbacks SET state='attention',"
            "outcome_kind='approval_blocked',outcome_payload=? "
            "WHERE review_task_id=?",
            ("{}", review_id),
        )
        pin = br.get_pin(conn, "go_profile")
        objective = kb.get_grace_objective(conn, "go_profile")
        br.migrate_objective(
            conn,
            objective_id="go_profile",
            platform="telegram",
            chat_id=contract["identity"]["chat_id"],
            thread_id=contract["identity"]["thread_id"],
            profile_id="ai_bizweek",
            version=CURRENT_VERSION,
            expected_revision=objective["revision"],
            expected_pin_hash=br.digest(pin),
            reason="Replay a durable system checkpoint after migration",
            apply=True,
        )
        callback = dict(conn.execute(
            "SELECT * FROM grace_loop_callbacks WHERE review_task_id=?",
            (review_id,),
        ).fetchone())

        with pytest.raises(br.BehaviorProfileError, match="pin_mismatch"):
            kb._apply_grace_objective_callback_outcome(
                conn,
                callback=callback,
                kind="approval_blocked",
                payload=payload,
            )
        kb._apply_grace_objective_callback_outcome(
            conn,
            callback=callback,
            kind="approval_blocked",
            payload=payload,
            system_checkpoint_authorized=True,
        )
        assert kb.get_grace_objective(conn, "go_profile")["status"] == (
            "waiting_approval"
        )


@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
@pytest.mark.parametrize("old_version", ["v1", "v3"])
def test_callback_kernel_release_retains_behavior_and_migrates_policy(
    project, old_version,
):
    import json

    old_manifest = br.profile(project, old_version)
    new_manifest = br.profile(project, CURRENT_VERSION)
    for field in (
        "profile_id", "contract_schema_version",
        "validator_set_hash",
    ):
        assert new_manifest[field] == old_manifest[field]
    assert old_manifest["route_snapshot"] == f"v1/{project}"
    assert new_manifest["route_snapshot"] == f"v2/{project}"
    bundle = new_manifest["bundle"]
    for component in (
        "contract", "domain", "preflight", "prompt", "review", "workflow",
    ):
        assert new_manifest["files"][f"{bundle}/{component}.py"] == old_manifest["files"][
            f"v1/{component}.py"
        ]
    assert new_manifest["files"][f"{bundle}/compiler.py"] != old_manifest["files"][
        "v1/compiler.py"
    ]
    assert new_manifest["files"][f"{bundle}/routing.py"] != old_manifest["files"][
        "v1/routing.py"
    ]
    contract = setup_objective(project, enabled=False)
    identity = contract["identity"]
    policy = br._policy_snapshot("telegram", identity["chat_id"], identity["thread_id"])
    old_pin = br._make_pin(project, old_version, policy)
    with kb.connect_closing() as conn:
        # Historical v1 Objective: migration, not rewriting its immutable pin.
        conn.execute("INSERT INTO grace_objective_behavior_pins VALUES (?,?,?)",
                     ("go_profile", json.dumps(old_pin), json.dumps(policy)))
        with pytest.raises(br.BehaviorProfileError, match="kernel_migration_required"):
            br.guard_objective(conn, "go_profile")
        result = br.migrate_objective(
            conn, objective_id="go_profile", platform="telegram", chat_id=identity["chat_id"],
            thread_id=identity["thread_id"], profile_id=project, version=CURRENT_VERSION,
            expected_revision=1, expected_pin_hash=br.digest(old_pin),
            reason="Callback kernel compatibility release", apply=True,
        )
        assert br.guard_objective(conn, "go_profile") == result["next_pin"]
        assert result["next_pin"]["policy_snapshot_hash"] == old_pin["policy_snapshot_hash"]
        assert result["next_pin"]["generation"] == old_pin["generation"] + 1


@pytest.mark.parametrize("case", load_cases()["cases"], ids=lambda case: case["id"])
def test_profile_full_local_replay_preserves_decision_stage_and_package(tmp_path, case):
    fixture = load_cases()
    legacy = replay_case(fixture, case, tmp_path / "legacy.db")
    pinned = replay_case(
        fixture, case, tmp_path / "pinned-current.db", behavior_version=CURRENT_VERSION,
    )
    for key in ("decision", "review_outcome", "objective", "stages", "callback", "package",
                "task_states", "external_effect_count", "successor_task_count", "approval_states"):
        assert pinned.get(key) == legacy.get(key), key
    if "normalized_contract" in pinned:
        assert pinned["normalized_contract"]["behavior_pin"]["behavior_profile_id"] == case["project"]


def test_secondhand_route_change_does_not_change_ai_bizweek_replay(tmp_path, monkeypatch):
    import hashlib
    import json
    import shutil
    import yaml

    fixture = load_cases()
    case = fixture["cases"][0]
    before = replay_case(fixture, case, tmp_path / "before.db", behavior_version=CURRENT_VERSION)
    catalog = tmp_path / "catalog"
    shutil.copytree(br.ROOT, catalog)
    monkeypatch.setattr(br, "ROOT", catalog)
    manifest = json.loads((catalog / f"secondhand_commerce@{CURRENT_VERSION}.json").read_text())
    old_routes = manifest["route_snapshot"]
    new_routes = "v23/secondhand_commerce"
    shutil.copytree(catalog / old_routes, catalog / new_routes)
    route_path = catalog / new_routes / "routing-rules.yaml"
    rules = yaml.safe_load(route_path.read_text())
    rules["worker_routes"] = [r for r in rules["worker_routes"] if r.get("match", {}).get("task_type") != "facebook_marketplace_readonly"]
    route_path.write_text(yaml.safe_dump(rules, allow_unicode=True))
    manifest.update(version="v23", route_snapshot=new_routes)
    for name in ("routing-rules.yaml", "agent-registry.yaml"):
        del manifest["files"][old_routes + "/" + name]
        manifest["files"][new_routes + "/" + name] = hashlib.sha256((catalog / new_routes / name).read_bytes()).hexdigest()
    (catalog / "secondhand_commerce@v23.json").write_text(json.dumps(manifest))
    contract = setup_objective("secondhand_commerce")
    normalized = validate_loop_contract(contract)
    routing = br.implementation(normalized, "routing")
    before_route = routing.route_clawops_objective("Read listing", task_type="facebook_marketplace_readonly", hub_ops_dir=br.route_directory(normalized))
    with kb.connect_closing() as conn:
        pin = br.get_pin(conn, "go_profile")
        br.migrate_objective(conn, objective_id="go_profile", platform="telegram", chat_id=contract["identity"]["chat_id"], thread_id=contract["identity"]["thread_id"], profile_id="secondhand_commerce", version="v23", expected_revision=1, expected_pin_hash=br.digest(pin), reason="Test changed route", apply=True)
        contract.pop("behavior_pin")
        candidate = br.bind_contract(conn, contract)
    after_route = routing.route_clawops_objective("Read listing", task_type="facebook_marketplace_readonly", hub_ops_dir=br.route_directory(candidate))
    assert "Unsupported task_type" in str(after_route)
    assert "Unsupported task_type" not in str(before_route)
    # Preserve the same input binding bytes; rebinding changes its timestamp hash.
    monkeypatch.setattr("proactive.policy_registry.bind_topic_policies", lambda *a, **k: None)
    after = replay_case(fixture, case, tmp_path / "after.db", behavior_version=CURRENT_VERSION)
    assert after == before


def test_migration_refuses_unfinished_card_before_delegation_link():
    contract = setup_objective()
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="Pending pinned work", body=render_execution_body(validate_loop_contract(contract)))
        pin = br.get_pin(conn, "go_profile")
        with pytest.raises(br.BehaviorProfileError, match="migration_inflight"):
            br.migrate_objective(conn, objective_id="go_profile", platform="telegram", chat_id=contract["identity"]["chat_id"], thread_id=contract["identity"]["thread_id"], profile_id="ai_bizweek", version=CURRENT_VERSION, expected_revision=1, expected_pin_hash=br.digest(pin), reason="Cannot bypass unfinished work", apply=True)
        assert br.get_pin(conn, "go_profile") == pin
        assert conn.execute("SELECT count(*) FROM grace_behavior_migrations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "settled_outcome",
    ["intermediate_blocked", "terminal_blocked"],
)
def test_migration_accepts_resolved_callback_with_unstarted_review_card(
    settled_outcome,
):
    contract = setup_objective()
    with kb.connect_closing() as conn:
        execution = kb.create_task(
            conn, title="Blocked execution", body=render_execution_body(contract),
        )
        review = kb.create_task(
            conn, title="Unstarted review", body=render_review_body(contract, execution),
            parents=(execution,),
        )
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (execution,))
        conn.execute(
            """UPDATE grace_objective_stages
                  SET status='done', execution_task_id=?, review_task_id=?,
                      outcome_kind=?
                WHERE objective_id='go_profile' AND stage_key='prepare'""",
            (execution, review, settled_outcome),
        )
        conn.execute(
            """INSERT INTO grace_loop_callbacks
                (review_task_id,execution_task_id,platform,chat_id,thread_id,
                 contract_fingerprint,state,created_at,objective_id,stage_key,
                 outcome_kind)
                VALUES (?,?,?,?,?,'fingerprint','delivered',1,'go_profile',
                        'prepare',?)""",
            (
                review, execution, "telegram", contract["identity"]["chat_id"],
                contract["identity"]["thread_id"],
                settled_outcome,
            ),
        )
        pin = br.get_pin(conn, "go_profile")
        result = br.migrate_objective(
            conn, objective_id="go_profile", platform="telegram",
            chat_id=contract["identity"]["chat_id"],
            thread_id=contract["identity"]["thread_id"], profile_id="ai_bizweek",
            version=CURRENT_VERSION, expected_revision=1,
            expected_pin_hash=br.digest(pin), reason="Resolved callback is idle",
            apply=True,
        )
        assert result["next_pin"]["generation"] == pin["generation"] + 1
        assert kb.get_task(conn, review).status == "todo"


def test_changed_profile_bytes_block_existing_pin(tmp_path, monkeypatch):
    import shutil
    contract = setup_objective()
    bundle = br.profile("ai_bizweek", CURRENT_VERSION)["bundle"]
    catalog = tmp_path / "catalog"
    shutil.copytree(br.ROOT, catalog)
    monkeypatch.setattr(br, "ROOT", catalog)
    (catalog / bundle / "contract.py").write_text("# incompatible replacement\n")
    with pytest.raises(br.BehaviorProfileError, match="bundle_changed"):
        validate_loop_contract(contract)


def test_real_ai_namespace_is_bound_separately_from_business_profile():
    namespace = "telegram_1003938559457_4641_bff429b6e587"
    contract = setup_objective(project_namespace=namespace)
    normalized = validate_loop_contract(contract)
    assert normalized["identity"]["project"] == namespace
    assert normalized["behavior_pin"]["behavior_profile_id"] == "ai_bizweek"
    assert normalized["behavior_pin"]["project_namespace"] == namespace
    assert br.implementation(normalized, "compiler").__name__.endswith(f".{br.profile('ai_bizweek', CURRENT_VERSION)['bundle']}.compiler")
    saved_body = render_execution_body(normalized)
    with kb.connect_closing() as conn:
        pin = br.get_pin(conn, "go_profile")
        result = br.migrate_objective(conn, objective_id="go_profile", platform="telegram", chat_id=contract["identity"]["chat_id"], thread_id=contract["identity"]["thread_id"], profile_id="ai_bizweek", version=CURRENT_VERSION, expected_revision=1, expected_pin_hash=br.digest(pin), reason="Preserve actual project namespace", apply=True)
        assert result["next_pin"]["project_namespace"] == namespace
    assert resolve_task_policy_snapshots(saved_body)["policies"][0]["content"] == "Original complete policy"
    wrong = deepcopy(contract)
    wrong.pop("behavior_pin")
    wrong["identity"]["project"] = "ai_bizweek"
    with pytest.raises(br.BehaviorProfileError, match="project_mismatch"):
        br.attach_contract(wrong)


def test_migration_cli_preview_is_read_only(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from hermes_cli.behavior_profiles import main

    contract = setup_objective()
    with kb.connect_closing() as conn:
        pin = br.get_pin(conn, "go_profile")
    spec = dict(objective_id="go_profile", platform="telegram", chat_id=contract["identity"]["chat_id"], thread_id=contract["identity"]["thread_id"], profile_id="ai_bizweek", version=CURRENT_VERSION, expected_revision=1, expected_pin_hash=br.digest(pin), reason="Read-only CLI preview")
    path = tmp_path / "migration.json"
    path.write_text(json.dumps(spec))
    monkeypatch.setattr(sys, "argv", ["behavior_profiles", "migrate", str(path)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is False
    with kb.connect_closing() as conn:
        assert br.get_pin(conn, "go_profile") == pin
        assert kb.get_grace_objective(conn, "go_profile")["revision"] == 1
        assert conn.execute("SELECT count(*) FROM grace_behavior_migrations").fetchone()[0] == 0


@pytest.mark.parametrize("versioned", [False, True])
def test_disabled_bridge_rejects_even_when_tool_names_are_present(tmp_path, monkeypatch, versioned):
    import json
    from proactive import hubops_routing

    routing = br.implementation(setup_objective(), "routing") if versioned else hubops_routing
    home = tmp_path / "bridge-home"
    config_dir = home / ".openclaw"
    config_dir.mkdir(parents=True)
    required = ["facebook_page_graph_status", "facebook_page_graph_publish"]
    (config_dir / "openclaw.json").write_text(json.dumps({
        "agents": {"list": [{"id": routing.OPENCLAW_FACEBOOK_PAGE_PROFILE, "tools": {"allow": required}}]},
        "plugins": {"entries": {"hermes-bridge": {"enabled": False, "config": {"allowedTools": required}}}},
    }))
    monkeypatch.setenv("HOME", str(home))
    probe = routing._probe_openclaw_facebook_page_tools(set(required))
    assert probe["available_tools"] == sorted(required)
    assert probe["ok"] is False
    result = routing.route_clawops_objective("Read reviewed page package", project="ai_bizweek", task_type="facebook_page_api_publish", approved=True, hub_ops_dir=br.ROOT / "v1" / "ai_bizweek")
    assert result["status"] == "blocked"
    assert "bridge plugin is disabled" in str(result)


def test_failed_birth_pin_rolls_back_the_objective(monkeypatch):
    def unavailable(*args):
        raise br.BehaviorProfileError("policy unavailable")
    monkeypatch.setattr(br, "_policy_snapshot", unavailable)
    with pytest.raises(br.BehaviorProfileError, match="policy unavailable"):
        setup_objective()
    with kb.connect_closing() as conn:
        assert kb.get_grace_objective(conn, "go_profile") is None
        assert br.get_pin(conn, "go_profile") is None
        assert conn.execute("SELECT count(*) FROM grace_objective_stages").fetchone()[0] == 0


def test_legacy_migration_requires_explicit_current_policy_and_project():
    contract = setup_objective(enabled=False)
    with kb.connect_closing() as conn:
        original = kb.get_grace_objective(conn, "go_profile")
        spec = dict(objective_id="go_profile", platform="telegram", chat_id=contract["identity"]["chat_id"], thread_id=contract["identity"]["thread_id"], profile_id="ai_bizweek", version=CURRENT_VERSION, expected_revision=1, expected_pin_hash=br.digest(None), reason="Explicit legacy adoption")
        with pytest.raises(br.BehaviorProfileError, match="legacy_policy_unknown"):
            br.migrate_objective(conn, **spec, apply=True)
        with pytest.raises(br.BehaviorProfileError, match="migration_project_unknown"):
            br.migrate_objective(conn, **spec, policy_mode="current", apply=True)
        assert br.get_pin(conn, "go_profile") is None
        result = br.migrate_objective(conn, **spec, policy_mode="current", project_namespace="ai_bizweek", apply=True)
        assert result["previous_pin"] is None
        assert result["next_pin"]["generation"] == 1
        now = kb.get_grace_objective(conn, "go_profile")
        for key in ("current_stage_key", "status", "objective", "original_request_sha256"):
            assert now[key] == original[key]
        assert conn.execute("SELECT previous_pin FROM grace_behavior_migrations").fetchone()[0] is None


@pytest.mark.parametrize("initially_pinned", [False, True])
def test_idempotent_objective_reuse_never_rebinds(initially_pinned):
    contract = setup_objective(enabled=initially_pinned)
    identity = contract["identity"]
    with kb.connect_closing() as conn:
        before = kb.get_grace_objective(conn, "go_profile")
        pin = br.get_pin(conn, "go_profile")
        if not initially_pinned:
            br.set_selection(conn, platform="telegram", chat_id=identity["chat_id"], thread_id=identity["thread_id"], project="ai_bizweek", profile_id="ai_bizweek", version=CURRENT_VERSION, expected_revision=0, reason="Enable after this legacy Objective already exists")
        reused = kb.create_grace_objective(conn, objective_id="go_profile", platform="telegram", chat_id=identity["chat_id"], thread_id=identity["thread_id"], session_key="fixture", title="fixture", objective="fixture", original_request_sha256="a" * 64, required_stage_keys=["prepare", "publish"], terminal_stage_key="publish", acceptance_criteria=["verified"], behavior_project="ai_bizweek")
        assert reused == before
        assert br.get_pin(conn, "go_profile") == pin


def test_generated_commerce_namespace_uses_pinned_business_route():
    from proactive.hubops_routing import route_clawops_objective

    namespace = "telegram_generated_commerce_namespace"
    contract = setup_objective("secondhand_commerce", project_namespace=namespace)
    routed = route_clawops_objective("Prepare scoped commerce work", project=namespace, task_type="facebook_marketplace_group_publish", approved=True, behavior_contract=contract)
    assert routed["status"] == "routed"
    assert routed["project"] == namespace
    assert routed["agent_assignment"]["assigned_agent"] == "secondhand_commerce"
    with pytest.raises(br.BehaviorProfileError, match="routing_project_mismatch"):
        route_clawops_objective("Mismatched routing identity", project="secondhand_commerce", behavior_contract=contract)


@pytest.mark.parametrize("initially_pinned", [False, True])
@pytest.mark.parametrize("operation", ["plan", "request_plan"])
def test_workflow_dispatch_holds_off_concurrent_migration(monkeypatch, initially_pinned, operation):
    import sqlite3
    from contextlib import closing
    from hermes_cli import objective_workflow as workflow

    contract = setup_objective(enabled=initially_pinned)
    original = br.workflow_implementation
    observations = []
    def checked_dispatch(conn, objective_id):
        implementation = original(conn, objective_id)
        # A second connection cannot start the write transaction required by
        # migration between selecting a behavior and executing its operation.
        with closing(sqlite3.connect(kb.kanban_db_path(), timeout=0)) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN IMMEDIATE")
        observations.append(objective_id)
        return implementation
    monkeypatch.setattr(br, "workflow_implementation", checked_dispatch)
    specification = dict(objective_id="go_profile", expected_revision=1, platform="telegram", chat_id=contract["identity"]["chat_id"], thread_id=contract["identity"]["thread_id"], required_stage_keys=["prepare", "extra", "publish"], current_stage_key="extra", reason="Check dispatch/migration serialization")
    with kb.connect_closing() as conn:
        before = br.get_pin(conn, "go_profile")
        if operation == "plan":
            workflow.plan(conn, **specification)
            assert kb.get_grace_objective(conn, "go_profile")["revision"] == 2
        else:
            with pytest.raises(ValueError, match="active objective execution"):
                workflow.request_plan(conn, origin_execution_task_id="missing", **specification)
            assert kb.get_grace_objective(conn, "go_profile")["revision"] == 1
        assert br.get_pin(conn, "go_profile") == before
    assert observations


def test_generated_ai_namespace_keeps_business_requirements():
    contract = setup_objective(project_namespace="opaque_123")
    body = render_execution_body(validate_loop_contract(contract))
    assert "Page Hero must be exact 16:9" in body
    domain = br.implementation(contract, "domain")
    generic = deepcopy(contract)
    generic["goal"]["objective"] = "Prepare the deliverable"
    generic["identity"].pop("topic_name", None)
    generic["routing"]["task_type"] = "generic"
    assert "ai_bizweek" in domain._contract_text(generic)
    assert domain.infer_builtin_domain_memory(generic)


def test_named_board_is_persisted_before_dispatch(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.create_board("profile-test")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "profile-test")
    contract = setup_objective()
    assert contract["identity"]["board"] == "profile-test"
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    assert validate_loop_contract(contract)["behavior_pin"] == contract["behavior_pin"]


def test_preupgrade_migration_preview_needs_no_schema_write():
    contract = setup_objective(enabled=False)
    with kb.connect_closing() as conn:
        conn.execute("DROP TABLE grace_objective_behavior_pins")
        conn.execute("DROP TABLE grace_behavior_migrations")
        conn.execute("DROP TABLE grace_behavior_selections")
        spec = dict(objective_id="go_profile", platform="telegram", chat_id=contract["identity"]["chat_id"], thread_id=contract["identity"]["thread_id"], profile_id="ai_bizweek", version=CURRENT_VERSION, expected_revision=1, expected_pin_hash=br.digest(None), reason="Preview legacy adoption", policy_mode="current", project_namespace="ai_bizweek")
        conn.execute("PRAGMA query_only=ON")
        assert br.migrate_objective(conn, **spec)["applied"] is False
        with pytest.raises(br.BehaviorProfileError, match="rollback_target_unavailable"):
            br.migrate_objective(conn, **spec, rollback_generation=1)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='grace_objective_behavior_pins'").fetchone()


def test_catalog_install_requires_restart_then_preserves_pinned_review_auth(tmp_path, monkeypatch):
    import json
    contract = setup_objective()
    catalog = tmp_path / "process-catalog"
    catalog.mkdir()
    monkeypatch.setattr(kb, "_behavior_root", catalog)
    monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", kb._review_runtime_digest())
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="Pinned auth", body=render_execution_body(contract))
        claimed = kb.claim_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        receipt = kb._review_runtime_receipt(conn, task_id)
        metadata = dict(run.metadata or {}, workflow_review_source=receipt)
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run.id))
        auth = dict(task_id=task_id, run_id=str(run.id), claim_lock=claimed.claim_lock, worker_auth_token=claimed.worker_auth_token)
        assert kb.validate_kanban_worker_auth(conn, **auth)
        (catalog / "new-profile.json").write_text('{}')
        with pytest.raises(br.BehaviorProfileError, match="runtime_reload_required"):
            kb.validate_kanban_worker_auth(conn, **auth)
        # Model a fresh process importing the expanded catalog. The selected
        # profile/kernel/policy remain identical across that restart.
        monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", kb._review_runtime_digest())
        assert kb._review_runtime_receipt(conn, task_id) == receipt
        assert kb.validate_kanban_worker_auth(conn, **auth)
        metadata['workflow_review_source'] = {**receipt, 'review_runtime_sha256': '0' * 64}
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run.id))
        with pytest.raises(kb.WorkerAuthorizationError):
            kb.validate_kanban_worker_auth(conn, **auth)

@pytest.mark.parametrize('project', ['ai_bizweek', 'secondhand_commerce'])
def test_migrated_callback_correlates_successor_without_accepting_old_work(project):
    contract = validate_loop_contract(setup_objective(project))
    with kb.connect_closing() as conn:
        old = [kb.create_task(conn, title='old', body=render_execution_body(contract)),
               kb.create_task(conn, title='old review', body=render_review_body(contract, 'old'))]
        for task_id in old:
            kb.block_task(conn, task_id, reason='Migration checkpoint', kind='capability')
        pin = br.get_pin(conn, 'go_profile')
        br.migrate_objective(conn, objective_id='go_profile', platform='telegram',
            chat_id=contract['identity']['chat_id'], thread_id=contract['identity']['thread_id'],
            profile_id=project, version=CURRENT_VERSION, expected_revision=1, expected_pin_hash=br.digest(pin),
            reason='Retire old callback generation', apply=True)
        fresh = deepcopy(contract)
        fresh.pop('behavior_pin')
        fresh = validate_loop_contract(br.bind_contract(conn, fresh))
        new = [kb.create_task(conn, title='new', body=render_execution_body(fresh)),
               kb.create_task(conn, title='new review', body=render_review_body(fresh, 'new'))]
        callback = dict(objective_id='go_profile', stage_key='prepare', execution_task_id=old[0], review_task_id=old[1])
        successor = dict(objective_id='go_profile', stage_key='publish', execution_task_id=new[0], review_task_id=new[1])
        def apply(kind='continued', **changes):
            kb._apply_grace_objective_callback_outcome(conn, callback=callback, kind=kind,
                payload={}, successor={**successor, **changes})
        with pytest.raises(br.BehaviorProfileError):
            apply('closed')
        with pytest.raises(br.BehaviorProfileError):
            apply(execution_task_id=old[0])
        with pytest.raises(br.BehaviorProfileError, match='continuation_pin_missing'):
            apply(execution_task_id='missing')
        apply()
        assert kb.get_grace_objective(conn, 'go_profile')['current_stage_key'] == 'publish'
        with pytest.raises(br.BehaviorProfileError):
            kb.claim_task(conn, old[0])
        conn.execute('DELETE FROM grace_behavior_migrations WHERE objective_id=?', ('go_profile',))
        with pytest.raises(br.BehaviorProfileError):
            apply()


@pytest.mark.parametrize("state,stage_status,outcome,callback_outcome,owner,allowed", [
    ("cancelled", "done", "superseded_by_retry", "superseded_by_retry", None, True),
    ("cancelled", "done", "cancelled", "cancelled", None, True),
    ("attention", "done", "continued", "continued", None, True),
    ("delivered", "done", "continued", "continued", None, True),
    ("attention", "done", "continued", "continued", "active-lease", False),
    ("delivered", "planned", "continued", "continued", None, False),
    ("delivered", "done", "continued", "intermediate_blocked", None, False),
    ("cancelled", "blocked", "superseded_by_retry", "superseded_by_retry", None, False),
    ("cancelled", "done", "accepted", "accepted", None, False),
    ("cancelled", "done", "superseded_by_retry", "superseded_by_retry", "active-lease", False),
    ("pending", "done", "superseded_by_retry", "superseded_by_retry", None, False),
])
def test_migration_preserves_superseded_cancelled_callback(
    state, stage_status, outcome, callback_outcome, owner, allowed,
):
    contract = setup_objective()
    with kb.connect_closing() as conn:
        conn.execute("UPDATE grace_objective_stages SET status=?,outcome_kind=? WHERE objective_id='go_profile' AND stage_key='prepare'", (stage_status, outcome))
        conn.execute("""INSERT INTO grace_loop_callbacks
            (review_task_id,execution_task_id,platform,chat_id,thread_id,contract_fingerprint,
             state,created_at,objective_id,stage_key,lease_owner,outcome_kind)
            VALUES ('review','execution','telegram',?,?,'fingerprint',?,1,
                    'go_profile','prepare',?,?)""",
            (
                contract['identity']['chat_id'], contract['identity']['thread_id'],
                state, owner, callback_outcome,
            ))
        before = dict(conn.execute("SELECT * FROM grace_loop_callbacks WHERE review_task_id='review'").fetchone())
        pin = br.get_pin(conn, 'go_profile')
        args = dict(objective_id='go_profile',platform='telegram',chat_id=contract['identity']['chat_id'],
            thread_id=contract['identity']['thread_id'],profile_id='ai_bizweek',version=CURRENT_VERSION,
            expected_revision=1,expected_pin_hash=br.digest(pin),reason='Preserve cancelled history',apply=True)
        if allowed:
            result = br.migrate_objective(conn, **args)
            assert result['next_pin']['generation'] == pin['generation'] + 1
        else:
            with pytest.raises(br.BehaviorProfileError, match='migration_callback_pending'):
                br.migrate_objective(conn, **args)
            assert br.get_pin(conn, 'go_profile') == pin
        assert dict(conn.execute("SELECT * FROM grace_loop_callbacks WHERE review_task_id='review'").fetchone()) == before


@pytest.mark.parametrize("fault", [
    None,
    "wrong_error",
    "pending",
    "lease",
    "rejected_review",
    "stage_mismatch",
    "external_effect",
    "lane_mismatch",
])
def test_migration_allows_only_exact_accepted_session_reset_handoff(fault):
    contract = setup_objective(version=CURRENT_VERSION)
    identity = contract["identity"]
    with kb.connect_closing() as conn:
        execution = kb.create_task(
            conn,
            title="accepted execution",
            body=render_execution_body(contract),
        )
        assert kb.complete_task(conn, execution, summary="done")
        review = kb.create_task(
            conn,
            title="accepted review",
            body=render_review_body(contract, execution),
            parents=(execution,),
        )
        now = 1
        conn.execute(
            """INSERT INTO grace_delegations (
                delegation_id,contract_fingerprint,request_instance_id,
                platform,chat_id,thread_id,session_key,session_id,
                resolved_route,approval_required,state,execution_task_id,
                review_task_id,objective_id,stage_key,created_at,updated_at
            ) VALUES (
                'gd-reset','ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                'request-reset','telegram',?,?,'session','old-session','{}',0,
                'queued',?,?,'go_profile','prepare',?,?
            )""",
            (
                identity["chat_id"], identity["thread_id"], execution, review,
                now, now,
            ),
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_profile",
            stage_key="prepare",
            delegation_id="gd-reset",
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review,
            execution_task_id=execution,
            platform="telegram",
            chat_id=identity["chat_id"],
            thread_id=identity["thread_id"],
            user_id="kj",
            session_key="session",
            session_id="old-session",
            contract_fingerprint="f" * 64,
            completion_mode="intermediate",
            objective_id="go_profile",
            stage_key="prepare",
        )
        review_run = kb._synthesize_ended_run(
            conn,
            review,
            outcome="completed",
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (review,))
        kb._append_event(
            conn,
            review,
            "completed",
            {"summary": "accepted"},
            run_id=review_run,
        )
        due = kb.list_due_grace_loop_callbacks(conn)[0]
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review,
            event_id=due["event_id"],
            lease_owner="gateway",
        )
        assert kb.escalate_grace_loop_callback(
            conn,
            review_task_id=review,
            event_id=due["event_id"],
            lease_owner="gateway",
            error=kb._GRACE_SESSION_RESET_HANDOFF_ERROR,
        )
        if fault == "wrong_error":
            conn.execute(
                "UPDATE grace_loop_callbacks SET last_error='other' "
                "WHERE review_task_id=?", (review,),
            )
        elif fault == "pending":
            conn.execute(
                "UPDATE grace_loop_callbacks SET state='pending' "
                "WHERE review_task_id=?", (review,),
            )
        elif fault == "lease":
            conn.execute(
                "UPDATE grace_loop_callbacks SET lease_owner='gateway' "
                "WHERE review_task_id=?", (review,),
            )
        elif fault == "rejected_review":
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"review_outcome": "rejected"}), due["event_run_id"]),
            )
        elif fault == "stage_mismatch":
            conn.execute(
                "UPDATE grace_objectives SET current_stage_key='publish' "
                "WHERE objective_id='go_profile'"
            )
        elif fault == "external_effect":
            kb._upsert_external_effect(
                conn,
                task_id=execution,
                platform="facebook",
                state="absent_verified",
                external_id=None,
                details=None,
                run_id=kb.latest_run(conn, execution).id,
                now=now,
            )
        elif fault == "lane_mismatch":
            conn.execute(
                "UPDATE grace_loop_callbacks SET thread_id='other' "
                "WHERE review_task_id=?", (review,),
            )
        before = dict(kb.get_grace_loop_callback(conn, review))
        pin = br.get_pin(conn, "go_profile")
        migrate = dict(
            objective_id="go_profile",
            platform="telegram",
            chat_id=identity["chat_id"],
            thread_id=identity["thread_id"],
            profile_id="ai_bizweek",
            version=CURRENT_VERSION,
            expected_revision=1,
            expected_pin_hash=br.digest(pin),
            reason="Resume an exact accepted session-reset handoff",
            apply=True,
        )
        if fault is None:
            result = br.migrate_objective(conn, **migrate)
            assert result["next_pin"]["generation"] == 2
        else:
            with pytest.raises(
                br.BehaviorProfileError,
                match="migration_callback_pending",
            ):
                br.migrate_objective(conn, **migrate)
            assert br.get_pin(conn, "go_profile") == pin
        assert dict(kb.get_grace_loop_callback(conn, review)) == before


@pytest.mark.parametrize(
    "fault", [
        None,
        "active_lease",
        "unrepaired_predecessor",
        "missing_receipt",
        "event_mismatch",
        "repeated_migration",
    ],
)
def test_migration_allows_exact_terminal_closure_repair(fault):
    contract = setup_objective(version=CURRENT_VERSION)
    identity = contract["identity"]
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE grace_objective_stages SET status='done',"
            "outcome_kind='continued' WHERE objective_id='go_profile' "
            "AND stage_key='prepare'"
        )
        execution = kb.create_task(
            conn, title="terminal execution", body=render_execution_body(contract),
        )
        execution_run = kb.claim_task(conn, execution, claimer="worker")
        assert execution_run is not None
        assert kb.complete_task(
            conn, execution, summary="done",
            expected_run_id=execution_run.current_run_id,
        )
        review = kb.create_task(
            conn,
            title="terminal review",
            body=render_review_body(contract, execution),
            parents=(execution,),
        )
        now = int(time.time())
        conn.execute(
            """INSERT INTO grace_delegations (
                delegation_id,contract_fingerprint,request_instance_id,
                platform,chat_id,thread_id,session_key,session_id,
                resolved_route,approval_required,state,execution_task_id,
                review_task_id,objective_id,stage_key,created_at,updated_at
            ) VALUES (
                'gd-closure','ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                'request-closure','telegram',?,?,'session','session-id','{}',0,
                'queued',?,?,'go_profile','publish',?,?
            )""",
            (identity["chat_id"], identity["thread_id"], execution, review, now, now),
        )
        kb._bind_grace_objective_stage(
            conn,
            objective_id="go_profile",
            stage_key="publish",
            delegation_id="gd-closure",
        )
        conn.execute(
            "UPDATE grace_objective_stages SET execution_task_id=?,review_task_id=? "
            "WHERE objective_id='go_profile' AND stage_key='publish'",
            (execution, review),
        )
        kb.add_grace_loop_callback(
            conn,
            review_task_id=review,
            execution_task_id=execution,
            platform="telegram",
            chat_id=identity["chat_id"],
            thread_id=identity["thread_id"],
            user_id="kj",
            session_key="session",
            session_id="session-id",
            contract_fingerprint="f" * 64,
            completion_mode="terminal",
            objective_id="go_profile",
            stage_key="publish",
        )
        review_run_id = kb._synthesize_ended_run(
            conn,
            review,
            outcome="completed",
            summary="accepted",
            metadata={"review_outcome": "accepted"},
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (review,))
        kb._append_event(
            conn,
            review,
            "completed",
            {"summary": "accepted"},
            run_id=review_run_id,
        )
        due = kb.list_due_grace_loop_callbacks(conn)[0]
        conn.execute(
            "UPDATE grace_loop_callbacks SET attempts=1,attempt_event_id=?,"
            "last_error=? WHERE review_task_id=?",
            (
                due["event_id"],
                "Grace objective cannot close before required stages complete: old",
                review,
            ),
        )
        if fault == "active_lease":
            conn.execute(
                "UPDATE grace_loop_callbacks SET lease_owner='other' "
                "WHERE review_task_id=?",
                (review,),
            )
        elif fault == "unrepaired_predecessor":
            conn.execute(
                "UPDATE grace_objective_stages SET outcome_kind='cancelled' "
                "WHERE objective_id='go_profile' AND stage_key='prepare'"
            )
        execution_body_before = kb.get_task(conn, execution).body
        review_body_before = kb.get_task(conn, review).body
        callback_before = dict(kb.get_grace_loop_callback(conn, review))
        pin = br.get_pin(conn, "go_profile")
        migrate = dict(
            conn=conn,
            objective_id="go_profile",
            platform="telegram",
            chat_id=identity["chat_id"],
            thread_id=identity["thread_id"],
            profile_id="ai_bizweek",
            version=CURRENT_VERSION,
            expected_revision=1,
            expected_pin_hash=br.digest(pin),
            reason="Install the reviewed closure lifecycle repair",
            closure_review_task_id=review,
            apply=True,
        )
        if fault in {"active_lease", "unrepaired_predecessor"}:
            with pytest.raises(
                br.BehaviorProfileError, match="migration_callback_pending",
            ):
                br.migrate_objective(**migrate)
            assert br.get_pin(conn, "go_profile") == pin
            return
        result = br.migrate_objective(**migrate)
        assert result["next_pin"]["generation"] == pin["generation"] + 1
        assert kb.get_task(conn, execution).body == execution_body_before
        assert kb.get_task(conn, review).body == review_body_before
        assert dict(kb.get_grace_loop_callback(conn, review)) == callback_before
        claim_event_id = due["event_id"]
        if fault == "repeated_migration":
            current = br.get_pin(conn, "go_profile")
            with pytest.raises(
                br.BehaviorProfileError,
                match="migration_closure_recovery_already_applied",
            ):
                br.migrate_objective(
                    **{
                        **migrate,
                        "expected_revision": 2,
                        "expected_pin_hash": br.digest(current),
                    }
                )
            assert br.get_pin(conn, "go_profile") == current
            return
        if fault == "event_mismatch":
            kb._append_event(
                conn,
                review,
                "completed",
                {"summary": "different completed event"},
                run_id=review_run_id,
            )
            replacement_event_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            conn.execute(
                "UPDATE grace_loop_callbacks SET attempt_event_id=? "
                "WHERE review_task_id=?",
                (replacement_event_id, review),
            )
            claim_event_id = replacement_event_id
        assert kb.claim_grace_loop_callback(
            conn,
            review_task_id=review,
            event_id=claim_event_id,
            lease_owner="gateway",
        )
        callback = kb.get_grace_loop_callback(conn, review)
        if fault in {"missing_receipt", "event_mismatch"}:
            if fault == "missing_receipt":
                conn.execute(
                    "UPDATE grace_behavior_migrations SET reason='{}' "
                    "WHERE objective_id='go_profile' AND generation=?",
                    (result["next_pin"]["generation"],),
                )
            with pytest.raises(
                br.BehaviorProfileError,
                match="closure_recovery_receipt_missing",
            ):
                kb._apply_grace_objective_callback_outcome(
                    conn,
                    callback=callback,
                    kind="closed",
                    payload={"summary": "accepted terminal result"},
                )
            assert kb.get_grace_objective(conn, "go_profile")["status"] == "active"
            return
        kb._apply_grace_objective_callback_outcome(
            conn,
            callback=callback,
            kind="closed",
            payload={"summary": "accepted terminal result"},
        )
        assert kb.get_grace_objective(conn, "go_profile")["status"] == "completed"


@pytest.mark.parametrize('project', ['ai_bizweek','secondhand_commerce'])
@pytest.mark.parametrize('fault', [None,'missing_challenge','stale_pin','wrong_fingerprint'])
def test_migrated_accepted_callback_needs_current_sealed_approval(project, fault):
    import json
    contract=setup_objective(project)
    identity=contract['identity']
    with kb.connect_closing() as conn:
        execution=kb.create_task(conn,title='historical execution',body=render_execution_body(contract))
        review=kb.create_task(conn,title='historical review',body=render_review_body(contract,execution))
        conn.execute("UPDATE tasks SET status='done' WHERE id IN (?,?)",(execution,review))
        previous=br.get_pin(conn,'go_profile')
        br.migrate_objective(conn,objective_id='go_profile',platform='telegram',chat_id=identity['chat_id'],
            thread_id=identity['thread_id'],profile_id=project,version=CURRENT_VERSION,expected_revision=1,
            expected_pin_hash=br.digest(previous),reason='Idle compatibility repair',apply=True)
        current=deepcopy(contract);current.pop('behavior_pin');current=br.bind_contract(conn,current)
        selected=contract if fault=='stale_pin' else current
        callback=dict(objective_id='go_profile',stage_key='prepare',execution_task_id=execution,
                      review_task_id=review,lease_event_id=99)
        if fault!='missing_challenge':
            kb.create_grace_approval_challenge(conn,contract_fingerprint='0'*64 if fault=='wrong_fingerprint' else br.digest(selected),
                request_instance_id='request',platform='telegram',chat_id=identity['chat_id'],thread_id=identity['thread_id'],
                session_key='key',session_id='session',user_id_sha256='a'*64,requested_message_id='message',
                action_summary='publish',approval_platform='Facebook',approval_scope='["exact bytes"]',
                compiled_contract=selected,origin_review_task_id=review,origin_event_id=99)
        args=dict(callback=callback,kind='approval_blocked',payload={'next_stage_key':'publish','action':'publish','exact_question':'pending approval'})
        if fault:
            with pytest.raises(br.BehaviorProfileError):
                kb._apply_grace_objective_callback_outcome(conn,**args)
            assert kb.get_grace_objective(conn,'go_profile')['status']=='active'
        else:
            kb._apply_grace_objective_callback_outcome(conn,**args)
            obj=kb.get_grace_objective(conn,'go_profile')
            assert obj['status']=='waiting_approval' and obj['current_stage_key']=='publish'
        with pytest.raises(br.BehaviorProfileError):
            kb.claim_task(conn,execution)
