"""Real SQLite workflow boundaries, including restart/readback and rollback."""
import hashlib
import json
import time

import pytest
from PIL import Image

from hermes_cli import kanban_db as kb
from hermes_cli import objective_workflow as wf
from proactive.grace_task_compiler import render_execution_body


@pytest.fixture
def db(tmp_path):
    with kb.connect_closing(tmp_path / "workflow.db") as conn:
        kb.create_grace_objective(
            conn, objective_id="go_test", platform="telegram", chat_id="chat",
            thread_id="2", session_key="session", title="Relist", objective="Relist 12345",
            original_request_sha256="a" * 64, required_stage_keys=["prepare", "terminal"],
            terminal_stage_key="terminal", acceptance_criteria=["publish 2 new destinations"],
        )
        yield conn


def _plan(db, **changes):
    args = dict(
        objective_id="go_test", expected_revision=1, platform="telegram", chat_id="chat", thread_id="2",
        required_stage_keys=["prepare", "preflight", "publish", "terminal"],
        current_stage_key="preflight", reason="Repair plan",
        workflow={"project": "secondhand", "source_listing_id": "12345", "expected_destinations": 2},
    )
    args.update(changes)
    return wf.plan(db, **args)


def _pair(db, *, objective_id=None, publish_groups=()):
    execution = kb.create_task(db, title="preflight", body="Marketplace listing 12345")
    review = kb.create_task(db, title="review", parents=(execution,))
    evidence = {
        "sideEffectsPerformed": False,
        "sourceListing": {"listing_id": "12345", "list_in_more_places_available": True, "observed_at": 100},
        "coverageReconciliation": {"verified_eligible_for_later_approval": ["222"]},
        "groups": [{"group_id": "222", "canonical_url": "https://www.facebook.com/groups/222",
                    "readable_name": "Telescope trade", "chooser_group_id": "222",
                    "chooser_canonical_url": "https://www.facebook.com/groups/222",
                    "chooser_presence": "present", "chooser_selectability": "selectable_unchecked"}],
    }
    contract = {"identity": {"project": "secondhand"}, "objective_ref": {"objective_id": objective_id}}
    contract.update(routing={"task_type": "facebook_marketplace_readonly", "resolved": {"assignment": {"interaction_mode": "interactive_readonly"}}},
                    scope={"allowed": ["Read Marketplace source listing 12345 and group 222"]})
    if publish_groups:
        names = {"222": "Telescope trade", "333": "New telescope group"}
        contract["facebook_group_publish"] = {
            "mode": "listing_bound_chooser",
            "source_listing_id": "12345",
            "destinations": [
                {
                    "group_id": group,
                    "canonical_name": names.get(group, f"Group {group}"),
                    "canonical_url": f"https://www.facebook.com/groups/{group}",
                }
                for group in publish_groups
            ],
        }
    contract["evidence_contract"] = "facebook_group_preflight/v1"
    execution_run = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,summary,metadata) "
        "VALUES (?,'default','done',1,200,'completed','preflight',?)",
        (execution, json.dumps({"loop_contract": contract, "acceptance_evidence": evidence})),
    ).lastrowid
    review_source = {
        "parent_execution_task_id": execution,
        "parent_execution_run_id": execution_run,
        "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
            kb.get_run(db, execution_run)
        ),
    }
    review_run = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',201,202,'completed',?)",
        (review, json.dumps({"review_outcome": "accepted", "evidence": review_source,
                             "workflow_review_source": review_source})),
    ).lastrowid
    db.execute("UPDATE tasks SET status='done' WHERE id IN (?,?)", (execution, review))
    db.execute(
        "INSERT INTO grace_delegations(delegation_id,contract_fingerprint,request_instance_id,platform,chat_id,thread_id,"
        "session_key,session_id,resolved_route,approval_required,state,execution_task_id,review_task_id,"
        "objective_id,created_at,updated_at) VALUES (?,?,?,'telegram','chat','2','session','session','{}',0,'queued',?,?,?,1,1)",
        ("gd_" + execution, execution.ljust(64, "0"), execution, execution, review, objective_id),
    )
    return execution, review, execution_run, review_run


def _publish():
    return {
        "identity": {"platform": "telegram", "chat_id": "chat", "thread_id": "2", "project": "secondhand"},
        "objective_ref": {"objective_id": "go_test", "stage_key": "publish"},
        "scope": {"forbidden": ["Create new listing", "Share to Group"]},
        "facebook_group_publish": {
            "mode": "listing_bound_chooser", "source_listing_id": "12345",
            "destinations": [{"group_id": "222", "canonical_name": "Telescope trade", "canonical_url": "https://www.facebook.com/groups/222"}],
        },
    }


def test_plan_reorders_existing_reverse_spine_without_changing_bindings(db):
    for stage in ("publish", "preflight"):
        kb.ensure_grace_objective_stage(db, objective_id="go_test", stage_key=stage)
    db.execute("UPDATE grace_objective_stages SET delegation_id='gd_keep',execution_task_id='t_keep',status='queued' WHERE stage_key='prepare'")
    result = _plan(db)
    assert [r["stage_key"] for r in result["stages"]] == ["prepare", "preflight", "publish", "terminal"]
    assert result["stages"][0]["execution_task_id"] == "t_keep"
    assert result["objective"]["current_stage_key"] == "preflight"
    assert result["objective"]["revision"] == 2
    assert result["publication_progress"]["publication_gap"] == 2
    snapshot = json.loads(db.execute("SELECT snapshot FROM grace_objective_revisions").fetchone()[0])
    assert snapshot["objective"]["required_stage_keys"].index("publish") < snapshot["objective"]["required_stage_keys"].index("preflight")
    with pytest.raises(ValueError, match="revision changed"):
        _plan(db)


def test_blocked_objective_can_register_internal_repair_stage(db):
    db.execute(
        "UPDATE grace_objectives SET status='blocked' WHERE objective_id='go_test'"
    )

    mode = kb.grace_objective_stage_mode(
        db,
        objective_id="go_test",
        stage_key="repair_runtime_capability_123",
        platform="telegram",
        chat_id="chat",
        thread_id="2",
    )

    assert mode == "intermediate"
    objective = kb.get_grace_objective(db, "go_test")
    assert objective["current_stage_key"] == "repair_runtime_capability_123"
    assert "repair_runtime_capability_123" in json.loads(
        objective["required_stage_keys"]
    )
    assert json.loads(objective["required_stage_keys"])[-1] == "terminal"


def test_transaction_failure_rolls_back_entire_plan(db):
    db.execute("CREATE TRIGGER fail_stage BEFORE INSERT ON grace_objective_stages WHEN NEW.stage_key='publish' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(Exception, match="injected failure"):
        _plan(db)
    assert json.loads(kb.get_grace_objective(db, "go_test")["required_stage_keys"]) == ["prepare", "terminal"]
    assert db.execute("SELECT COUNT(*) FROM grace_objective_revisions").fetchone()[0] == 0
    assert wf.configuration(db, "go_test") is None


def test_cancelled_required_stage_cannot_satisfy_objective_closure(db):
    db.execute("UPDATE grace_objective_stages SET status='done',outcome_kind='cancelled' WHERE objective_id='go_test' AND stage_key='prepare'")
    db.execute("UPDATE grace_objectives SET current_stage_key='terminal' WHERE objective_id='go_test'")
    with pytest.raises(ValueError, match="prepare"):
        kb._apply_grace_objective_callback_outcome(
            db,
            callback={"objective_id": "go_test", "stage_key": "terminal"},
            kind="closed",
            payload={"summary": "done"},
        )
    assert kb.get_grace_objective(db, "go_test")["status"] != "completed"


@pytest.mark.parametrize("changes,error", [
    ({"chat_id": "other"}, "another Topic"),
    ({"required_stage_keys": ["preflight", "terminal"]}, "remove bound"),
    ({"required_stage_keys": ["terminal", "prepare", "preflight"]}, "terminal stage"),
])
def test_plan_rejects_scope_or_history_loss(db, changes, error):
    db.execute("UPDATE grace_objective_stages SET status='done' WHERE stage_key='prepare'")
    with pytest.raises(ValueError, match=error):
        _plan(db, **changes)


def test_historical_evidence_is_pinned_but_does_not_grant_publication(db):
    execution, review, run, _ = _pair(db)
    result = _plan(db, workflow={"project": "secondhand", "source_listing_id": "12345", "expected_destinations": 2,
                               "historical_evidence": [{"execution_task_id": execution, "review_task_id": review}]})
    history = result["publication_progress"]["historical_evidence"][0]
    assert history["execution_run_id"] == run
    assert history["use"] == "historical_context_only"
    assert db.execute("SELECT objective_id FROM grace_delegations").fetchone()[0] is None
    db.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,details,created_at,updated_at) VALUES (?,'facebook','group:222','verified',?,1,1)", (execution, json.dumps({"action": "publish_existing_listing", "source_listing_id": "12345"})))
    progress = wf.progress(db, "go_test")
    assert progress["submission_capacity"] == 2
    assert progress["destinations"][0]["publication_state"] != "not_submitted"


def test_real_preflight_binding_and_stale_review_rejection(db, monkeypatch):
    from contextlib import nullcontext
    from plugins.openclaw_bridge.clawops_delegate import _bind_accepted_facebook_group_preflight

    _plan(db)
    execution, review, run, review_run = _pair(db, objective_id="go_test")
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight", preflight_source={"execution_task_id": execution, "review_task_id": review})
    monkeypatch.setattr(kb, "connect_closing", lambda **_: nullcontext(db))
    bound = _bind_accepted_facebook_group_preflight(contract, board="default")
    assert bound["mode"] == "listing_bound_chooser"
    assert bound["preflight_evidence"]["execution_run_id"] == run
    contract["facebook_group_publish"] = bound
    wf.validate_publication(db, contract)
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"review_outcome": "accepted", "evidence": {"parent_execution_task_id": execution, "parent_execution_run_id": run + 100}}), review_run))
    contract["facebook_group_publish"]["mode"] = "accepted_preflight"
    with pytest.raises(ValueError, match="exact execution run"):
        _bind_accepted_facebook_group_preflight(contract, board="default")


@pytest.mark.parametrize("invalid", [None, {}, "prose", True, "run", "stale", "wrong_task"])
def test_workflow_review_requires_exact_run_receipt_before_completion(db, invalid):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    receipt = {"parent_execution_task_id": execution, "parent_execution_run_id": run}
    if invalid in ("run", "stale", "wrong_task"):
        receipt.update({"run": {"parent_execution_run_id": str(run)},
                        "stale": {"parent_execution_run_id": run + 100},
                        "wrong_task": {"parent_execution_task_id": "t_wrong"}}[invalid])
    else:
        receipt = invalid
    before = db.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (review,)).fetchone()[0]
    with pytest.raises(ValueError, match="Workflow review requires"):
        kb.complete_task(db, review, summary="accepted in prose", metadata={"review_outcome": "accepted", "evidence": receipt})
    assert kb.get_task(db, review).status == "ready"
    assert db.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (review,)).fetchone()[0] == before
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, summary="verified exact run", metadata={
        "review_outcome": "accepted", "evidence": {
            "parent_execution_task_id": execution, "parent_execution_run_id": run}})
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight", preflight_source={
        "execution_task_id": execution, "review_task_id": review})
    assert wf.resolve_preflight(db, contract)["preflight_evidence"]["execution_run_id"] == run


def test_blocked_workflow_parent_evidence_is_visible_only_to_its_reviewer(db):
    _plan(db)
    execution, review, run, review_run = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='blocked' WHERE id=?", (execution,))
    db.execute("UPDATE task_runs SET status='blocked',outcome='blocked',metadata=? WHERE id=?", (
        json.dumps({"acceptance_evidence": {"groups": [{"group_id": "222", "evidence": "actual chooser readback"}]},
                    "external_effects": [], "worker_auth_sha256": "not-for-review", "private_field": "omit-me"}), run))
    review_source = {
        "parent_execution_task_id": execution,
        "parent_execution_run_id": run,
        "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
            kb.get_run(db, run)
        ),
    }
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (
            json.dumps({
                "review_outcome": "rejected",
                "evidence": review_source,
                "workflow_review_source": review_source,
            }),
            review_run,
        ),
    )
    context = kb.build_worker_context(db, review)
    assert f'"parent_execution_run_id": {run}' in context
    assert '"parent_run_started_at": 1' in context
    assert '"parent_run_ended_at": 200' in context
    assert '"run_outcome": "blocked"' in context
    assert '"acceptance_not_established": true' in context
    assert "actual chooser readback" in context
    assert "not-for-review" not in context and "omit-me" not in context
    active_review_source = kb._workflow_review_source(db, review)
    active_review_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,metadata) "
        "VALUES (?,'default','running',203,?)",
        (review, json.dumps({"workflow_review_source": active_review_source})),
    ).lastrowid
    db.execute(
        "UPDATE tasks SET status='running',current_run_id=? WHERE id=?",
        (active_review_run_id, review),
    )
    with pytest.raises(ValueError, match="cannot accept a blocked execution parent"):
        kb.complete_task(
            db,
            review,
            summary="blocked parent was truthful",
            metadata={
                "review_outcome": "accepted",
                "evidence": review_source,
            },
            expected_run_id=active_review_run_id,
        )
    assert kb.get_task(db, review).status == "running"
    other = kb.create_task(db, title="ordinary dependent", parents=[execution])
    assert "actual chooser readback" not in kb.build_worker_context(db, other)


def test_workflow_review_context_counts_only_canonical_package_assets(db, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "workflow.db"))
    _plan(db)
    execution, review, run_id, review_run_id = _pair(db, objective_id="go_test")
    body = tmp_path / "package.md"
    page = tmp_path / "page.png"
    audio = tmp_path / "audio.png"
    sections = {
        "facebook_page_post": "Page 內文",
        "facebook_group_post": "Group 附文",
        "gemini_notebook_prompt": "Gemini prompt",
        "podcast_title": "Podcast title",
        "podcast_description": "Podcast description",
    }
    package_body = "\n\n".join(sections.values())
    body.write_text(package_body + "\n", encoding="utf-8")
    Image.new("RGB", (1600, 900)).save(page)
    Image.new("RGB", (1200, 1200)).save(audio)
    delivery = {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_with_attachment",
        "asset_filenames": [page.name, audio.name],
        "body_field": "acceptance_evidence.inline_content_package",
    }
    for path in (body, page, audio):
        for _ in range(2):
            kb.add_attachment(
                db,
                execution,
                filename=path.name,
                stored_path=str(path),
                content_type=("text/markdown" if path == body else "image/png"),
                size=path.stat().st_size,
                uploaded_by="historical-replay",
            )
    run = kb.get_run(db, run_id)
    metadata = dict(run.metadata)
    metadata["loop_contract"] = {
        **metadata["loop_contract"],
        "user_facing_delivery": delivery,
    }
    db.commit()
    db.execute(
        "UPDATE tasks SET body=? WHERE id=?",
        (render_execution_body(metadata["loop_contract"]), execution),
    )
    metadata["user_facing_report"] = {
        "kind": "content_package",
        "delivery": "inline_with_attachment",
        "complete": True,
        "title": "完整發布包",
        "body": package_body,
        "observed_at": int(time.time()),
        "assets": [
            {
                "filename": path.name,
                "label": path.stem,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in (page, audio)
        ],
    }
    metadata["acceptance_evidence"].update({
        "inline_content_package": package_body,
        "sections": sections,
        "asset_manifest": [
            {
                "filename": page.name,
                "asset_family": "page_hero",
                "path": str(page),
                "sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
                "width": 1600,
                "height": 900,
            },
            {
                "filename": audio.name,
                "asset_family": "audio_brief",
                "path": str(audio),
                "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "width": 1200,
                "height": 1200,
            },
        ],
    })
    metadata["policy_receipts"] = []
    metadata["external_effects"] = []
    metadata["attachment_manifest"] = kb.task_attachment_manifest(db, execution)
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(metadata), run_id),
    )
    source = {
        "parent_execution_task_id": execution,
        "parent_execution_run_id": run_id,
        "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
            kb.get_run(db, run_id)
        ),
    }
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps({"workflow_review_source": source}), review_run_id),
    )

    report = kb.grace_inline_content_package_report(db, execution)
    assert report is not None
    assert report["package_kind"] == "full_publication_package"
    assert report["complete"] is True
    assert report["body"] == package_body
    context = kb.build_worker_context(db, review)

    assert "## Controller content-package readback" in context
    assert '"canonical_asset_count": 2' in context
    assert '"task_attachment_row_count": 6' in context
    assert '"package_complete": true' in context
    assert "Markdown body artifact is separate" in context


def test_cancelled_workflow_stage_finishes_lifecycle_without_acceptance(db):
    _plan(db)
    execution, review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE grace_delegations SET stage_key='preflight' WHERE execution_task_id=?", (execution,))
    db.execute("UPDATE tasks SET status='ready' WHERE id IN (?,?)", (execution, review))
    db.execute("UPDATE grace_objective_stages SET delegation_id=?,execution_task_id=?,review_task_id=?,status='queued' WHERE stage_key='preflight'",
               ("gd_" + execution, execution, review))
    kwargs = dict(platform="telegram", chat_id="chat", thread_id="2", requested_by="owner",
                  owner_user_id="owner", requested_message_id="operator-withdraw-duplicate", reason="Withdraw duplicate preflight")
    assert kb.cancel_grace_delegation(db, execution, **kwargs)
    stage = dict(db.execute("SELECT * FROM grace_objective_stages WHERE stage_key='preflight'").fetchone())
    assert stage["status"] == "done" and stage["outcome_kind"] == "cancelled"
    assert json.loads(stage["evidence"])["accepted"] is False
    assert kb.get_grace_objective(db, "go_test")["status"] == "active"
    assert wf.progress(db, "go_test")["complete"] is False
    assert kb.cancel_grace_delegation(db, execution, **kwargs)["idempotent_replay"] is True


def test_plan_can_retry_cancelled_workflow_stage(db):
    _plan(db)
    execution, review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE grace_delegations SET stage_key='preflight' WHERE execution_task_id=?", (execution,))
    db.execute("UPDATE tasks SET status='ready' WHERE id IN (?,?)", (execution, review))
    db.execute(
        "UPDATE grace_objective_stages SET delegation_id=?,execution_task_id=?,"
        "review_task_id=?,status='queued' WHERE stage_key='preflight'",
        ("gd_" + execution, execution, review),
    )
    kb.cancel_grace_delegation(
        db, execution, platform="telegram", chat_id="chat", thread_id="2",
        requested_by="owner", owner_user_id="owner",
        requested_message_id="operator-withdraw-retry", reason="Retry cleanly",
    )

    result = _plan(db, expected_revision=2, current_stage_key="preflight")

    stage = next(row for row in result["stages"] if row["stage_key"] == "preflight")
    assert stage["status"] == "planned"
    assert stage["delegation_id"] is None
    assert stage["execution_task_id"] is None
    assert stage["review_task_id"] is None
    assert stage["outcome_kind"] is None
    assert stage["evidence"] is None
    assert result["objective"]["current_stage_key"] == "preflight"
    assert result["objective"]["revision"] == 3


@pytest.mark.parametrize("observed_at", [None, True, "100", 0, 100.5, 261])
def test_review_rejects_preflight_timestamp_that_publication_cannot_use(db, observed_at):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    metadata = kb.latest_run(db, execution).metadata
    metadata["acceptance_evidence"]["sourceListing"]["observed_at"] = observed_at
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run))
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    with pytest.raises(ValueError, match="browser-observed"):
        kb.complete_task(db, review, summary="preflight accepted", metadata={"review_outcome": "accepted",
            "evidence": {"parent_execution_task_id": execution, "parent_execution_run_id": run}})
    assert kb.get_task(db, review).status == "running"


@pytest.mark.parametrize("mutation,error", [
    (lambda c: c["facebook_group_publish"].update(mode="canonical_url_per_group"), "accepted_preflight"),
    (lambda c: c["scope"].update(forbidden=["Never use List in more places"]), "forbids"),
    (lambda c: c["facebook_group_publish"].update(source_listing_id="54321"), "listing/project"),
])
def test_publication_route_matches_plan_and_preflight(db, mutation, error):
    _plan(db)
    contract = _publish()
    mutation(contract)
    with pytest.raises(ValueError, match=error):
        wf.validate_publication(db, contract)


def test_publication_waits_for_latest_objective_run_effect_reconciliation(db):
    _plan(db)
    preflight_execution, preflight_review, _, _ = _pair(db, objective_id="go_test")
    contract = _publish()
    contract["facebook_group_publish"].update(
        mode="accepted_preflight",
        preflight_source={
            "execution_task_id": preflight_execution,
            "review_task_id": preflight_review,
        },
    )
    contract["facebook_group_publish"] = wf.resolve_preflight(db, contract)
    execution, _, run, _ = _pair(db, objective_id="go_test")
    metadata = kb.latest_run(db, execution).metadata
    metadata["external_effect_reconciliation_required"] = True
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run))

    with pytest.raises(ValueError, match="external-effect reconciliation"):
        wf.validate_publication(db, contract)

    metadata["external_effect_reconciliation_required"] = False
    db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','blocked',300,301,'blocked',?)",
        (execution, json.dumps(metadata)),
    )
    wf.validate_publication(db, contract)


def test_publication_waits_for_older_unreconciled_effect_run(db):
    _plan(db)
    preflight_execution, preflight_review, _, _ = _pair(db, objective_id="go_test")
    contract = _publish()
    contract["facebook_group_publish"].update(
        mode="accepted_preflight",
        preflight_source={
            "execution_task_id": preflight_execution,
            "review_task_id": preflight_review,
        },
    )
    contract["facebook_group_publish"] = wf.resolve_preflight(db, contract)
    execution, _, run, _ = _pair(db, objective_id="go_test")
    metadata = kb.latest_run(db, execution).metadata
    metadata["external_effect_reconciliation_required"] = True
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run))
    newer_metadata = dict(metadata)
    newer_metadata.pop("external_effect_reconciliation_required")
    db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','completed',300,301,'completed',?)",
        (execution, json.dumps(newer_metadata)),
    )

    with pytest.raises(ValueError, match="external-effect reconciliation"):
        wf.validate_publication(db, contract)

    newer_metadata["external_effect_reconciliation_required"] = False
    db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','blocked',302,303,'blocked',?)",
        (execution, json.dumps(newer_metadata)),
    )
    wf.validate_publication(db, contract)


def test_join_pending_and_publication_are_distinct_and_prevent_replay(db):
    _plan(db)
    execution, _, execution_run, _ = _pair(db, objective_id="go_test", publish_groups=("222", "333"))
    def effect(group, details):
        db.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,external_id,details,created_at,updated_at) VALUES (?,'facebook',?,'verified',?,?,1,1)",
                   (execution, "group:" + group, group, json.dumps(details)))
    effect("111", {"action": "join"})
    effect("222", {"action": "publish_existing_listing", "source_listing_id": "12345", "readback": "pending review"})
    effect("333", {"action": "publish_existing_listing", "publication_state": "published", "source_listing_id": "12345", "canonical_name": "New telescope group", "canonical_url": "https://www.facebook.com/groups/333", "post_url": "https://www.facebook.com/groups/333/posts/444",
                   "post_submit_or_pending_readback": {"status": "published", "visible": True, "pending_review": False, "group_id": "333", "source_listing_id": "12345", "post_url": "https://www.facebook.com/groups/333/posts/444", "observed_at": 100}})
    db.execute("UPDATE task_external_effects SET run_id=? WHERE task_id=?", (execution_run, execution))
    progress = wf.progress(db, "go_test")
    assert progress["published_count"] == 1 and progress["publication_gap"] == 1
    assert not progress["complete"]
    assert progress["submission_capacity"] == 0
    with pytest.raises(ValueError, match="already submitted"):
        wf.validate_publication(db, _publish())
    another = _publish()
    another["facebook_group_publish"]["destinations"][0].update(group_id="444", canonical_url="https://www.facebook.com/groups/444")
    with pytest.raises(ValueError, match="submission capacity"):
        wf.validate_publication(db, another)
    with pytest.raises(ValueError, match="publication remains incomplete"):
        kb._apply_grace_objective_callback_outcome(db, callback={"objective_id": "go_test", "stage_key": "terminal"}, kind="closed", payload={})


def test_prompt_and_worker_snapshot_retain_progress(db, monkeypatch):
    from contextlib import nullcontext
    from proactive.prompt_policy import active_objectives_prompt
    from proactive.openclaw_async_executor import _objective_durable_evidence_snapshot

    _plan(db)
    monkeypatch.setattr(kb, "connect_closing", lambda **_: nullcontext(db))
    prompt = active_objectives_prompt(platform="telegram", chat_id="chat", thread_id="2")
    assert '"publication_gap": 2' in prompt
    snapshot = _objective_durable_evidence_snapshot(db, _publish())
    assert snapshot["publication_progress"]["expected_destinations"] == 2


def test_cli_plan_survives_process_restart(db, tmp_path):
    import os
    import subprocess
    import sys

    database = db.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, HERMES_KANBAN_DB=database)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "objective_id": "go_test", "expected_revision": 1, "platform": "telegram",
        "chat_id": "chat", "thread_id": "2", "required_stage_keys": ["prepare", "terminal"],
        "current_stage_key": "prepare", "reason": "CLI recovery",
    }))
    command = [sys.executable, "-m", "hermes_cli.objective_workflow"]
    written = subprocess.run(command + ["plan", str(plan_path)], env=env, capture_output=True, text=True, check=True)
    readback = subprocess.run(command + ["show", "go_test"], env=env, capture_output=True, text=True, check=True)
    assert json.loads(written.stdout) == json.loads(readback.stdout)
    assert json.loads(readback.stdout)["objective"]["revision"] == 2


def test_cli_plan_from_active_worker_is_deferred_for_exact_review(db, tmp_path):
    import os
    import subprocess
    import sys

    execution, _, _, _ = _active_self_planner(db)
    db.commit()
    database = db.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, HERMES_KANBAN_DB=database, HERMES_KANBAN_TASK=execution)
    plan_path = tmp_path / "worker-plan.json"
    payload = _deferred_spec(execution)
    payload.pop("origin_execution_task_id")
    plan_path.write_text(json.dumps(payload))

    command = [sys.executable, "-m", "hermes_cli.objective_workflow"]
    requested = subprocess.run(
        command + ["plan", str(plan_path)], env=env,
        capture_output=True, text=True, check=True,
    )

    result = json.loads(requested.stdout)
    assert result["execution_task_id"] == execution
    assert result["state"] == "pending"
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1


def test_cli_plan_detects_active_worker_when_environment_marker_is_cleared(db, tmp_path):
    import os
    import subprocess
    import sys

    execution, _, _, _ = _active_self_planner(db)
    db.commit()
    database = db.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, HERMES_KANBAN_DB=database)
    env.pop("HERMES_KANBAN_TASK", None)
    plan_path = tmp_path / "worker-plan-without-env.json"
    payload = _deferred_spec(execution)
    payload.pop("origin_execution_task_id")
    plan_path.write_text(json.dumps(payload))

    command = [sys.executable, "-m", "hermes_cli.objective_workflow"]
    attempted = subprocess.run(
        command + ["plan", str(plan_path)], env=env,
        capture_output=True, text=True,
    )

    assert attempted.returncode != 0
    assert "worker-authenticated request requires HERMES_KANBAN_TASK" in attempted.stderr
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1


def test_workflow_review_hash_binds_zero_effect_assertion(db):
    execution, _, run_id, _ = _pair(db)
    run = kb.get_run(db, run_id)
    before = kb.workflow_review_evidence_hash(run)
    metadata = run.metadata
    metadata["read_only_zero_external_effects"] = True
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run_id))

    assert kb.workflow_review_evidence_hash(kb.get_run(db, run_id)) != before


def test_cli_worker_cannot_spoof_deferred_plan_origin(db, tmp_path):
    import os
    import subprocess
    import sys

    execution, _, _, _ = _active_self_planner(db)
    db.commit()
    database = db.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, HERMES_KANBAN_DB=database, HERMES_KANBAN_TASK=execution)
    plan_path = tmp_path / "spoofed-worker-plan.json"
    payload = _deferred_spec(execution)
    payload["origin_execution_task_id"] = "t_someone_else"
    plan_path.write_text(json.dumps(payload))

    command = [sys.executable, "-m", "hermes_cli.objective_workflow"]
    attempted = subprocess.run(
        command + ["request-plan", str(plan_path)], env=env,
        capture_output=True, text=True,
    )

    assert attempted.returncode != 0
    assert "origin does not match" in attempted.stderr
    assert db.execute("SELECT COUNT(*) FROM grace_objective_plan_requests").fetchone()[0] == 0


def test_reservation_serializes_objective_publication_and_allows_exact_replay(db):
    _plan(db)
    contract = _publish()
    execution, review, _, _ = _pair(db, objective_id="go_test")
    contract["facebook_group_publish"].update(mode="accepted_preflight", preflight_source={"execution_task_id": execution, "review_task_id": review})
    contract["facebook_group_publish"] = wf.resolve_preflight(db, contract)
    args = dict(contract_fingerprint="b" * 64, request_instance_id="request-1",
                platform="telegram", chat_id="chat", thread_id="2", session_key="session",
                session_id="session", resolved_route={"backend": "test"}, approval_required=False,
                objective_id="go_test", stage_key="publish", publication_contract=contract)
    first = kb.reserve_grace_delegation(db, **args)
    assert kb.reserve_grace_delegation(db, **args)["delegation_id"] == first["delegation_id"]
    with pytest.raises(ValueError, match="in-flight"):
        kb.reserve_grace_delegation(db, **dict(args, contract_fingerprint="c" * 64, request_instance_id="request-2"))


def test_historical_name_exclusion_survives_missing_old_numeric_id(db):
    _plan(db, workflow={"project": "secondhand", "source_listing_id": "12345", "expected_destinations": 2,
                       "excluded_destination_names": ["Telescope trade"]})
    with pytest.raises(ValueError, match="historical destination name"):
        wf.validate_publication(db, _publish())


@pytest.mark.parametrize("field,value", [("source_listing_id", 12345), ("excluded_destination_ids", [111]), ("excluded_destination_ids", ["111", 222])])
def test_numeric_json_ids_are_rejected_without_persisting_plan(db, field, value):
    spec = {"project": "secondhand", "source_listing_id": "12345", "expected_destinations": 2, field: value}
    with pytest.raises(ValueError):
        _plan(db, workflow=spec)
    assert wf.configuration(db, "go_test") is None
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1


def test_reservation_revalidates_changed_preflight_before_authorization(db):
    _plan(db)
    execution, review, _, review_run = _pair(db, objective_id="go_test")
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight", preflight_source={"execution_task_id": execution, "review_task_id": review})
    contract["facebook_group_publish"] = wf.resolve_preflight(db, contract)
    # Simulate a changed review after initial binding but before reservation.
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"review_outcome": "rejected"}), review_run))
    with pytest.raises(ValueError, match="completed Grace review"):
        kb.reserve_grace_delegation(
            db, contract_fingerprint="d" * 64, request_instance_id="stale-preflight",
            platform="telegram", chat_id="chat", thread_id="2", session_key="session",
            session_id="session", resolved_route={"backend": "test"}, approval_required=False,
            objective_id="go_test", stage_key="publish", publication_contract=contract,
        )
    assert db.execute("SELECT COUNT(*) FROM grace_delegations").fetchone()[0] == 1


def test_fabricated_preflight_does_not_authorize_publication(db):
    _plan(db)
    with pytest.raises(ValueError, match="preflight_source"):
        wf.validate_publication(db, _publish())


def test_preflight_allows_exact_threadless_identity(db):
    _plan(db)
    execution, review, _, review_run_id = _pair(db, objective_id="go_test")
    db.execute("UPDATE grace_delegations SET thread_id='' WHERE execution_task_id=?", (execution,))
    contract = _publish()
    contract["identity"]["thread_id"] = ""
    contract["facebook_group_publish"].update(mode="accepted_preflight", preflight_source={"execution_task_id": execution, "review_task_id": review})
    assert wf.resolve_preflight(db, contract)["mode"] == "listing_bound_chooser"
    run = kb.latest_run(db, execution)
    run.metadata["loop_contract"]["routing"]["task_type"] = "ops"
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run.id))
    review_metadata = kb.get_run(db, review_run_id).metadata
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(
        kb.get_run(db, run.id)
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    with pytest.raises(ValueError, match="read-only route"):
        wf.resolve_preflight(db, contract)


@pytest.mark.parametrize("text", ["relist this item to these Facebook groups", "relist in these groups", "relist to groups"])
def test_relist_objective_recognizes_natural_objects(text):
    from plugins.openclaw_bridge.clawops_delegate import _EXTERNAL_ACTION_OBJECTIVE
    assert _EXTERNAL_ACTION_OBJECTIVE.search(text)


@pytest.mark.parametrize("mutation,error", [
    (lambda e: e["sourceListing"].pop("observed_at"), "timestamp"),
    (lambda e: e["sourceListing"].update(observed_at="yesterday"), "timestamp"),
    (lambda e: e["sourceListing"].update(observed_at=0.5), "timestamp"),
    (lambda e: e["groups"].append(dict(e["groups"][0])), "duplicate"),
    (lambda e: e["coverageReconciliation"].update(verified_eligible_for_later_approval="222"), "eligibility"),
])
def test_preflight_rejects_missing_time_or_duplicate_destinations(db, mutation, error):
    _plan(db)
    execution, review, run_id, review_run_id = _pair(db, objective_id="go_test")
    run = kb.get_run(db, run_id)
    mutation(run.metadata["acceptance_evidence"])
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    review_metadata = kb.get_run(db, review_run_id).metadata
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(
        kb.get_run(db, run_id)
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight", preflight_source={"execution_task_id": execution, "review_task_id": review})
    with pytest.raises(ValueError, match=error):
        wf.resolve_preflight(db, contract)


@pytest.mark.parametrize("project", [[], {"name": "secondhand"}, "   "])
def test_plan_rejects_invalid_immutable_project(db, project):
    with pytest.raises(ValueError, match="requires project"):
        _plan(db, workflow={"project": project, "source_listing_id": "12345", "expected_destinations": 2})
    assert wf.configuration(db, "go_test") is None


@pytest.mark.parametrize("malformed", [True, 1.0])
def test_preflight_requires_integer_run_binding(db, malformed):
    _plan(db)
    execution, review, run_id, review_run = _pair(db, objective_id="go_test")
    assert run_id == 1
    metadata = {"review_outcome": "accepted", "evidence": {"parent_execution_task_id": execution, "parent_execution_run_id": malformed}}
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), review_run))
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight", preflight_source={"execution_task_id": execution, "review_task_id": review})
    with pytest.raises(ValueError, match="exact execution run"):
        wf.resolve_preflight(db, contract)


@pytest.mark.parametrize("execution_status,review_status", [("ready", "todo"), ("done", "todo"), ("done", "scheduled"), ("done", "triage"), ("blocked", "ready")])
def test_plan_rejects_admitted_work_without_active_run(db, execution_status, review_status):
    execution, review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status=? WHERE id=?", (execution_status, execution))
    db.execute("UPDATE tasks SET status=? WHERE id=?", (review_status, review))
    with pytest.raises(ValueError, match="in-flight delegation"):
        _plan(db)
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1


def test_plan_allows_repair_of_blocked_parent_with_waiting_review(db):
    execution, review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='blocked' WHERE id=?", (execution,))
    db.execute("UPDATE tasks SET status='todo' WHERE id=?", (review,))
    assert _plan(db)["objective"]["revision"] == 2


def test_finished_stage_does_not_block_plan_when_its_old_review_is_todo(db):
    execution, review, _, _ = _pair(db, objective_id="go_test")
    delegation_id = "gd_" + execution
    db.execute("UPDATE tasks SET status='todo' WHERE id=?", (review,))
    db.execute(
        "UPDATE grace_objective_stages SET status='done',delegation_id=?,"
        "execution_task_id=?,review_task_id=? "
        "WHERE objective_id='go_test' AND stage_key='prepare'",
        (delegation_id, execution, review),
    )

    assert _plan(db)["objective"]["revision"] == 2


def test_plan_rejects_an_already_bound_current_stage(db):
    db.execute(
        "UPDATE grace_objective_stages SET status='done',delegation_id='gd_old',"
        "execution_task_id='t_old',review_task_id='t_old_review' WHERE stage_key='prepare'"
    )
    with pytest.raises(ValueError, match="current stage must be unbound"):
        _plan(db, current_stage_key="prepare")


def _active_self_planner(db):
    execution, review, run_id, review_run_id = _pair(db, objective_id="go_test")
    delegation_id = "gd_" + execution
    db.execute("DELETE FROM task_runs WHERE id=?", (review_run_id,))
    db.execute(
        "UPDATE task_runs SET status='running',ended_at=NULL,outcome=NULL WHERE id=?",
        (run_id,),
    )
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (execution,))
    db.execute("UPDATE tasks SET status='todo' WHERE id=?", (review,))
    db.execute(
        "UPDATE grace_delegations SET stage_key='prepare' WHERE delegation_id=?",
        (delegation_id,),
    )
    db.execute(
        "UPDATE grace_objective_stages SET status='queued',delegation_id=?,"
        "execution_task_id=?,review_task_id=? WHERE objective_id='go_test' AND stage_key='prepare'",
        (delegation_id, execution, review),
    )
    return execution, review, run_id, delegation_id


def _deferred_spec(execution):
    return dict(
        origin_execution_task_id=execution, objective_id="go_test",
        expected_revision=1, platform="telegram", chat_id="chat", thread_id="2",
        required_stage_keys=["prepare", "preflight", "terminal"],
        current_stage_key="preflight", reason="Reviewed self-plan",
    )


def _plan_review_receipt(db, execution, run_id, request):
    source = {
        "parent_execution_task_id": execution,
        "parent_execution_run_id": run_id,
        "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
            kb.get_run(db, run_id)
        ),
        "objective_plan_request_id": request["request_id"],
        "objective_plan_specification_sha256": hashlib.sha256(
            request["specification"].encode()
        ).hexdigest(),
    }
    return {"review_outcome": "accepted", "evidence": source,
            "workflow_review_source": source}


def test_self_planner_request_is_idempotent_and_does_not_change_spine(db):
    execution, _, _, _ = _active_self_planner(db)
    first = wf.request_plan(db, **_deferred_spec(execution))
    second = wf.request_plan(db, **_deferred_spec(execution))
    assert first["request_id"] == second["request_id"]
    assert first["state"] == "pending"
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1
    assert [r[0] for r in db.execute(
        "SELECT stage_key FROM grace_objective_stages WHERE objective_id='go_test' ORDER BY position"
    )] == ["prepare", "terminal"]


def test_self_planner_rejects_unsupported_fields_before_persisting(db):
    execution, _, _, _ = _active_self_planner(db)
    specification = _deferred_spec(execution)
    specification["comment"] = "not accepted by plan()"

    with pytest.raises(ValueError, match="unsupported fields: comment"):
        wf.request_plan(db, **specification)

    assert db.execute(
        "SELECT COUNT(*) FROM grace_objective_plan_requests"
    ).fetchone()[0] == 0


def test_self_planner_requires_integer_expected_revision(db):
    execution, _, _, _ = _active_self_planner(db)
    specification = _deferred_spec(execution)
    specification["expected_revision"] = "1"

    with pytest.raises(ValueError, match="expected_revision must be an integer"):
        wf.request_plan(db, **specification)

    assert db.execute(
        "SELECT COUNT(*) FROM grace_objective_plan_requests"
    ).fetchone()[0] == 0


def test_exact_accepted_review_applies_self_plan_after_execution_ends(db):
    execution, review, run_id, _ = _active_self_planner(db)
    request = wf.request_plan(db, **_deferred_spec(execution))
    db.execute(
        "UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?",
        (run_id,),
    )
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    metadata = _plan_review_receipt(db, execution, run_id, request)
    review_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',3,4,'completed',?)", (review, json.dumps(metadata)),
    ).lastrowid
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (review,))
    result = wf.apply_reviewed_plan_request(
        db, review_task_id=review, review_run_id=review_run_id,
    )
    assert result["objective"]["revision"] == 2
    assert result["objective"]["current_stage_key"] == "prepare"
    assert db.execute(
        "SELECT state FROM grace_objective_plan_requests WHERE request_id=?",
        (request["request_id"],),
    ).fetchone()[0] == "applied"
    assert wf.apply_reviewed_plan_request(
        db, review_task_id=review, review_run_id=review_run_id,
    ) is None


def test_later_exact_rejection_supersedes_reviewed_self_plan(db):
    execution, review, run_id, _ = _active_self_planner(db)
    request = wf.request_plan(db, **_deferred_spec(execution))
    db.execute(
        "UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?",
        (run_id,),
    )
    accepted = _plan_review_receipt(db, execution, run_id, request)
    accepted_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',3,4,'completed',?)",
        (review, json.dumps(accepted)),
    ).lastrowid
    rejected = _plan_review_receipt(db, execution, run_id, request)
    rejected["review_outcome"] = "rejected"
    db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',5,6,'completed',?)",
        (review, json.dumps(rejected)),
    )

    with pytest.raises(ValueError, match="superseded by a later exact verdict"):
        wf.apply_reviewed_plan_request(
            db,
            review_task_id=review,
            review_run_id=accepted_run_id,
        )

    assert kb.get_grace_objective(db, "go_test")["revision"] == 1
    assert db.execute(
        "SELECT state FROM grace_objective_plan_requests WHERE request_id=?",
        (request["request_id"],),
    ).fetchone()[0] == "pending"


def test_reviewed_plan_cannot_replace_controller_pinned_execution_run(db):
    execution, review, run_id, _ = _active_self_planner(db)
    request = wf.request_plan(db, **_deferred_spec(execution))
    db.execute(
        "UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?",
        (run_id,),
    )
    newer_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',3,4,'completed','{}')",
        (execution,),
    ).lastrowid
    evidence = {
        "parent_execution_task_id": execution,
        "parent_execution_run_id": run_id,
        "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
            kb.get_run(db, run_id)
        ),
        "objective_plan_request_id": request["request_id"],
        "objective_plan_specification_sha256": hashlib.sha256(
            request["specification"].encode()
        ).hexdigest(),
    }
    review_source = {
        "parent_execution_task_id": execution,
        "parent_execution_run_id": newer_run_id,
        "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
            kb.get_run(db, newer_run_id)
        ),
    }
    review_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',5,6,'completed',?)",
        (
            review,
            json.dumps(
                {
                    "review_outcome": "accepted",
                    "evidence": evidence,
                    "workflow_review_source": review_source,
                }
            ),
        ),
    ).lastrowid

    with pytest.raises(ValueError, match="controller-pinned"):
        wf.apply_reviewed_plan_request(
            db,
            review_task_id=review,
            review_run_id=review_run_id,
        )
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1
    assert db.execute(
        "SELECT state FROM grace_objective_plan_requests WHERE request_id=?",
        (request["request_id"],),
    ).fetchone()[0] == "pending"


def test_reviewed_self_plan_can_resume_before_its_completed_origin(db):
    execution, review, run_id, _ = _active_self_planner(db)
    db.execute("UPDATE grace_objective_stages SET position=2 WHERE stage_key='prepare'")
    db.execute("UPDATE grace_objective_stages SET position=3 WHERE stage_key='terminal'")
    db.execute(
        "INSERT INTO grace_objective_stages "
        "(objective_id,stage_key,position,status,created_at,updated_at) "
        "VALUES ('go_test','preflight',1,'planned',1,1)"
    )
    db.execute(
        "UPDATE grace_objectives SET required_stage_keys=? WHERE objective_id='go_test'",
        (json.dumps(["preflight", "prepare", "terminal"]),),
    )
    specification = _deferred_spec(execution)
    specification.update(
        required_stage_keys=["repair", "preflight", "prepare", "terminal"],
        current_stage_key="repair",
    )
    request = wf.request_plan(db, **specification)

    db.execute(
        "UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?",
        (run_id,),
    )
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    metadata = _plan_review_receipt(db, execution, run_id, request)
    review_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',3,4,'completed',?)", (review, json.dumps(metadata)),
    ).lastrowid
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (review,))

    result = wf.apply_reviewed_plan_request(
        db, review_task_id=review, review_run_id=review_run_id,
    )
    assert result["objective"]["current_stage_key"] == "prepare"
    assert [stage["stage_key"] for stage in result["stages"]] == [
        "repair", "preflight", "prepare", "terminal",
    ]
    assert result["stages"][2]["execution_task_id"] == execution


def test_self_plan_cannot_resume_at_an_already_bound_stage(db):
    execution, _, _, _ = _active_self_planner(db)
    specification = _deferred_spec(execution)
    specification.update(
        required_stage_keys=["older", "prepare", "terminal"],
        current_stage_key="older",
    )
    db.execute("UPDATE grace_objective_stages SET position=1 WHERE stage_key='prepare'")
    db.execute("UPDATE grace_objective_stages SET position=2 WHERE stage_key='terminal'")
    db.execute(
        "INSERT INTO grace_objective_stages "
        "(objective_id,stage_key,position,status,delegation_id,execution_task_id,"
        "review_task_id,created_at,updated_at) "
        "VALUES ('go_test','older',0,'done','gd_old','t_old','t_old_review',1,1)"
    )
    db.execute(
        "UPDATE grace_objectives SET required_stage_keys=? WHERE objective_id='go_test'",
        (json.dumps(["older", "prepare", "terminal"]),),
    )

    with pytest.raises(ValueError, match="resume stage must be unbound"):
        wf.request_plan(db, **specification)


def test_callback_claim_applies_exact_reviewed_self_plan(db):
    execution, review, run_id, _ = _active_self_planner(db)
    wf.request_plan(db, **_deferred_spec(execution))
    kb.add_grace_loop_callback(
        db, review_task_id=review, execution_task_id=execution,
        platform="telegram", chat_id="chat", thread_id="2",
        session_key="session", session_id="session", contract_fingerprint="f" * 64,
        completion_mode="intermediate", objective_id="go_test", stage_key="prepare",
    )
    db.execute(
        "UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?",
        (run_id,),
    )
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, summary="accepted", metadata={
        "review_outcome": "accepted", "evidence": {
            "parent_execution_task_id": execution, "parent_execution_run_id": run_id,
            "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(kb.get_run(db, run_id)),
        },
    })
    callback = kb.list_due_grace_loop_callbacks(db)[0]
    # A later run on the same review card must not replace the run named by
    # this completed callback event.
    db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',5,6,'completed',?)",
        (review, json.dumps({"review_outcome": "rejected"})),
    )
    assert kb.claim_grace_loop_callback(
        db, review_task_id=review, event_id=callback["event_id"], lease_owner="gateway",
    )
    objective = kb.get_grace_objective(db, "go_test")
    assert objective["revision"] == 2
    assert objective["current_stage_key"] == "prepare"


def test_callback_rejects_self_plan_when_reviewed_execution_changes(db):
    execution, review, run_id, _ = _active_self_planner(db)
    wf.request_plan(db, **_deferred_spec(execution))
    kb.add_grace_loop_callback(
        db, review_task_id=review, execution_task_id=execution,
        platform="telegram", chat_id="chat", thread_id="2",
        session_key="session", session_id="session", contract_fingerprint="f" * 64,
        completion_mode="intermediate", objective_id="go_test", stage_key="prepare",
    )
    db.execute("UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?", (run_id,))
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, summary="accepted", metadata={
        "review_outcome": "accepted", "evidence": {
            "parent_execution_task_id": execution, "parent_execution_run_id": run_id,
            "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(kb.get_run(db, run_id)),
        },
    })
    run = kb.get_run(db, run_id)
    run.metadata["acceptance_evidence"]["groups"][0]["readable_name"] = "Changed"
    assert kb.edit_completed_task_result(db, execution, result="corrected", metadata=run.metadata)
    callback = kb.list_due_grace_loop_callbacks(db)[0]
    assert kb.claim_grace_loop_callback(
        db, review_task_id=review, event_id=callback["event_id"], lease_owner="gateway",
    )
    request = db.execute("SELECT state,result FROM grace_objective_plan_requests").fetchone()
    assert request["state"] == "rejected"
    assert "evidence changed" in request["result"]


def test_callback_rejects_plan_specification_changed_after_exact_review(db):
    execution, review, run_id, _ = _active_self_planner(db)
    request = wf.request_plan(db, **_deferred_spec(execution))
    kb.add_grace_loop_callback(
        db, review_task_id=review, execution_task_id=execution,
        platform="telegram", chat_id="chat", thread_id="2",
        session_key="session", session_id="session", contract_fingerprint="f" * 64,
        completion_mode="intermediate", objective_id="go_test", stage_key="prepare",
    )
    db.execute(
        "UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?",
        (run_id,),
    )
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(
        db, review, summary="accepted", metadata={"review_outcome": "accepted"},
    )
    changed = json.loads(request["specification"])
    changed["reason"] = "unreviewed replacement"
    db.execute(
        "UPDATE grace_objective_plan_requests SET specification=? WHERE request_id=?",
        (json.dumps(changed, sort_keys=True, separators=(",", ":")), request["request_id"]),
    )
    db.execute(
        "INSERT INTO grace_objective_plan_requests("
        "request_id,objective_id,delegation_id,execution_task_id,execution_run_id,"
        "expected_revision,specification,state,created_at) VALUES "
        "('gpr_newer','go_test',?,?,?,1,?,'pending',1)",
        ("gd_" + execution, execution, run_id + 100, request["specification"]),
    )

    callback = kb.list_due_grace_loop_callbacks(db)[0]
    assert kb.claim_grace_loop_callback(
        db, review_task_id=review, event_id=callback["event_id"], lease_owner="gateway",
    )

    stored = db.execute(
        "SELECT state,result FROM grace_objective_plan_requests WHERE request_id=?",
        (request["request_id"],),
    ).fetchone()
    assert stored["state"] == "rejected"
    assert "exact request and specification" in stored["result"]
    assert db.execute(
        "SELECT state FROM grace_objective_plan_requests WHERE request_id='gpr_newer'"
    ).fetchone()[0] == "pending"
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1


def test_stale_self_plan_is_rejected_without_blocking_callback_claim(db):
    execution, review, run_id, _ = _active_self_planner(db)
    wf.request_plan(db, **_deferred_spec(execution))
    kb.add_grace_loop_callback(
        db, review_task_id=review, execution_task_id=execution,
        platform="telegram", chat_id="chat", thread_id="2",
        session_key="session", session_id="session", contract_fingerprint="f" * 64,
        completion_mode="intermediate", objective_id="go_test", stage_key="prepare",
    )
    db.execute("UPDATE grace_objectives SET revision=2 WHERE objective_id='go_test'")
    db.execute("UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?", (run_id,))
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, summary="accepted", metadata={
        "review_outcome": "accepted", "evidence": {
            "parent_execution_task_id": execution, "parent_execution_run_id": run_id,
            "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(kb.get_run(db, run_id)),
        },
    })
    callback = kb.list_due_grace_loop_callbacks(db)[0]
    assert kb.claim_grace_loop_callback(
        db, review_task_id=review, event_id=callback["event_id"], lease_owner="gateway",
    )
    request = db.execute("SELECT state,result FROM grace_objective_plan_requests").fetchone()
    assert request["state"] == "rejected"
    assert "revision changed" in request["result"]
    assert kb.get_grace_loop_callback(db, review)["state"] == "delivering"


def test_rejected_or_wrong_run_review_cannot_apply_self_plan(db):
    execution, review, run_id, _ = _active_self_planner(db)
    wf.request_plan(db, **_deferred_spec(execution))
    db.execute(
        "UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?",
        (run_id,),
    )
    metadata = {"review_outcome": "accepted", "evidence": {
        "parent_execution_task_id": execution, "parent_execution_run_id": run_id + 99}}
    review_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',3,4,'completed',?)", (review, json.dumps(metadata)),
    ).lastrowid
    with pytest.raises(ValueError, match="controller-pinned"):
        wf.apply_reviewed_plan_request(
            db, review_task_id=review, review_run_id=review_run_id,
        )
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1
    assert db.execute("SELECT state FROM grace_objective_plan_requests").fetchone()[0] == "pending"


def test_unrelated_inflight_delegation_still_blocks_reviewed_self_plan(db):
    execution, review, run_id, _ = _active_self_planner(db)
    request = wf.request_plan(db, **_deferred_spec(execution))
    db.execute("UPDATE task_runs SET status='done',ended_at=2,outcome='completed' WHERE id=?", (run_id,))
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (execution,))
    metadata = _plan_review_receipt(db, execution, run_id, request)
    review_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',3,4,'completed',?)", (review, json.dumps(metadata)),
    ).lastrowid
    db.execute("UPDATE tasks SET status='done' WHERE id=?", (review,))
    other_execution, other_review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (other_execution,))
    db.execute("UPDATE tasks SET status='todo' WHERE id=?", (other_review,))
    with pytest.raises(ValueError, match="in-flight delegation"):
        wf.apply_reviewed_plan_request(
            db, review_task_id=review, review_run_id=review_run_id,
        )
    assert kb.get_grace_objective(db, "go_test")["revision"] == 1
    assert db.execute("SELECT state FROM grace_objective_plan_requests").fetchone()[0] == "pending"


def test_progress_tolerates_legacy_scalar_readback(db):
    _plan(db)
    execution, _, execution_run, _ = _pair(db, objective_id="go_test")
    details = {"readback": "pending review"}
    db.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,details,created_at,updated_at) VALUES (?,'facebook','group:222','verified',?,1,1)", (execution, json.dumps(details)))
    result = wf.progress(db, "go_test")
    assert result["published_count"] == 0
    assert result["submission_capacity"] == 1


@pytest.mark.parametrize("details", [
    {"action": "publish_existing_listing", "source_listing_id": "12345", "publication_state": "pending"},
    {"readback": "unknown"},
    {"action": "unknown_write", "source_listing_id": "99999"},
])
def test_excluding_native_submission_never_releases_capacity(db, details):
    _plan(db)
    execution, _, _, _ = _pair(db, objective_id="go_test")
    db.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,details,created_at,updated_at) VALUES (?,'facebook','group:222','verified',?,1,1)", (execution, json.dumps(details)))
    spec = wf.configuration(db, "go_test")
    spec["excluded_destination_ids"] = ["222"]
    _plan(db, expected_revision=2, workflow=spec)
    assert wf.progress(db, "go_test")["submission_capacity"] == 1


def test_effect_approval_remains_bound_to_originating_run_across_retries(db):
    _plan(db)
    execution, review, first_run, first_review = _pair(db, objective_id="go_test")
    assert wf._accepted_execution_runs(db, "go_test", execution) == {first_run}
    retry = db.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) VALUES (?,'default','done',5,6,'completed')", (execution,)).lastrowid
    assert wf._accepted_execution_runs(db, "go_test", execution) == {first_run}
    review_source = {
        "parent_execution_task_id": execution,
        "parent_execution_run_id": retry,
        "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
            kb.get_run(db, retry)
        ),
    }
    metadata = {
        "review_outcome": "accepted",
        "evidence": review_source,
        "workflow_review_source": review_source,
    }
    db.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) VALUES (?,'default','done',7,8,'completed',?)", (review, json.dumps(metadata)))
    assert wf._accepted_execution_runs(db, "go_test", execution) == {first_run, retry}
    db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','blocked',9,10,'blocked',?)",
        (review, json.dumps({"review_outcome": "rejected", "evidence": {
            "parent_execution_task_id": execution,
            "parent_execution_run_id": first_run,
            "parent_execution_evidence_sha256": kb.workflow_review_evidence_hash(
                kb.get_run(db, first_run)
            ),
        }})),
    )
    assert wf._accepted_execution_runs(db, "go_test", execution) == {first_run, retry}
    db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',9,10,'completed',?)",
        (review, json.dumps({"review_outcome": "rejected", "evidence": {
            "parent_execution_task_id": execution,
            "parent_execution_run_id": first_run,
        }})),
    )
    assert wf._accepted_execution_runs(db, "go_test", execution) == {first_run, retry}
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(
        db,
        review,
        summary="new exact rejection",
        metadata={"review_outcome": "rejected"},
    )
    assert wf._accepted_execution_runs(db, "go_test", execution) == {first_run}
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"review_outcome": "rejected", "evidence": {"parent_execution_task_id": execution, "parent_execution_run_id": first_run}}), first_review))
    assert wf._accepted_execution_runs(db, "go_test", execution) == set()


def test_preflight_resolution_rejects_empty_or_duplicate_destinations(db):
    _plan(db)
    execution, review, _, _ = _pair(db, objective_id="go_test")
    contract = _publish()
    contract["facebook_group_publish"].update(
        mode="accepted_preflight",
        preflight_source={"execution_task_id": execution, "review_task_id": review},
    )
    contract["facebook_group_publish"]["destinations"] = []
    with pytest.raises(ValueError, match="at least one destination"):
        wf.resolve_preflight(db, contract)
    contract["facebook_group_publish"]["destinations"] = [
        _publish()["facebook_group_publish"]["destinations"][0],
        _publish()["facebook_group_publish"]["destinations"][0],
    ]
    with pytest.raises(ValueError, match="must be unique"):
        wf.resolve_preflight(db, contract)


def test_hashless_review_cannot_authorize_preflight_or_progress(db):
    _plan(db)
    execution, review, run_id, review_run_id = _pair(db, objective_id="go_test")
    review_metadata = kb.get_run(db, review_run_id).metadata
    review_metadata["evidence"].pop("parent_execution_evidence_sha256")
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    assert wf._accepted_execution_runs(db, "go_test", execution) == set()
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight",
        preflight_source={"execution_task_id": execution, "review_task_id": review})
    with pytest.raises(ValueError, match="unpinned"):
        wf.resolve_preflight(db, contract)


def test_worker_review_evidence_cannot_override_controller_pinned_run(db):
    _plan(db)
    execution, review, _, review_run_id = _pair(db, objective_id="go_test")
    forged_run_id = db.execute(
        "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
        "VALUES (?,'default','done',203,204,'completed','{}')",
        (execution,),
    ).lastrowid
    review_metadata = kb.get_run(db, review_run_id).metadata
    review_metadata["evidence"].update(
        parent_execution_run_id=forged_run_id,
        parent_execution_evidence_sha256=kb.workflow_review_evidence_hash(
            kb.get_run(db, forged_run_id)
        ),
    )
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(review_metadata), review_run_id),
    )

    assert wf._accepted_execution_runs(db, "go_test", execution) == set()


def test_unclaimed_review_metadata_cannot_grant_preflight_authority(db):
    _plan(db)
    execution, review, _, review_run_id = _pair(db, objective_id="go_test")
    review_metadata = kb.get_run(db, review_run_id).metadata
    review_metadata.pop("workflow_review_source")
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(review_metadata), review_run_id),
    )
    contract = _publish()
    contract["facebook_group_publish"].update(
        mode="accepted_preflight",
        preflight_source={"execution_task_id": execution, "review_task_id": review},
    )

    with pytest.raises(ValueError, match="controller-pinned"):
        wf.resolve_preflight(db, contract)


def test_preflight_resolution_rejects_source_that_never_requested_schema(db):
    _plan(db)
    execution, review, run_id, review_run_id = _pair(
        db, objective_id="go_test"
    )
    run_metadata = kb.get_run(db, run_id).metadata
    source_contract = run_metadata["loop_contract"]
    source_contract.pop("evidence_contract")
    source_contract["goal"] = {
        "deliverables": ["Inspect the listing without group preflight evidence"]
    }
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(run_metadata), run_id),
    )
    review_metadata = kb.get_run(db, review_run_id).metadata
    reviewed_hash = kb.workflow_review_evidence_hash(kb.get_run(db, run_id))
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = reviewed_hash
    review_metadata["workflow_review_source"][
        "parent_execution_evidence_sha256"
    ] = reviewed_hash
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(review_metadata), review_run_id),
    )
    contract = _publish()
    contract["facebook_group_publish"].update(
        mode="accepted_preflight",
        preflight_source={
            "execution_task_id": execution,
            "review_task_id": review,
        },
    )

    with pytest.raises(ValueError, match="did not request.*preflight schema"):
        wf.resolve_preflight(db, contract)


def test_openclaw_receives_route_guidance_and_nested_effects_remain_readable(db):
    from proactive.openclaw_async_executor import _worker_safe_loop_contract, _normalize_openclaw_external_effects

    _plan(db)
    execution, _, execution_run, _ = _pair(db, objective_id="go_test", publish_groups=("222",))
    contract = _publish()
    safe = _worker_safe_loop_contract(contract, external_effect_budget=1)
    assert any("List in more places" in line for line in safe["memory"]["working"])
    assert any("publication_state" in line for line in safe["memory"]["working"])
    nested = {"action": "publish_existing_listing", "source_listing_id": "12345", "publication_state": "pending",
              "canonical_name": "Telescope trade", "canonical_url": "https://www.facebook.com/groups/222",
              "post_url": "https://www.facebook.com/groups/222/posts/555"}
    effects = _normalize_openclaw_external_effects([
        {"target": "group:222", "effectKey": "group:222", "state": "verified", "externalId": "222", "readback": nested},
    ], metadata={"loop_contract": safe})
    db.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,external_id,details,created_at,updated_at) VALUES (?,'facebook','group:222','verified','222',?,1,1)",
               (execution, json.dumps(effects[0]["details"])))
    db.execute("UPDATE task_external_effects SET run_id=? WHERE task_id=?", (execution_run, execution))
    assert wf.progress(db, "go_test")["published_count"] == 0
    assert wf.progress(db, "go_test")["submission_capacity"] == 1
    nested["publication_state"] = "published"
    nested["post_submit_or_pending_readback"] = {"status": "published", "visible": True, "pending_review": False, "group_id": "222", "source_listing_id": "12345", "post_url": nested["post_url"], "observed_at": 100}
    db.execute("UPDATE task_external_effects SET details=? WHERE task_id=?", (json.dumps({"readback": nested}), execution))
    assert wf.progress(db, "go_test")["published_count"] == 1
    for field, wrong_value in (
        ("canonical_name", "Different group"),
        ("canonical_url", "https://www.facebook.com/groups/999"),
    ):
        original_value = nested[field]
        nested[field] = wrong_value
        db.execute(
            "UPDATE task_external_effects SET details=? WHERE task_id=?",
            (json.dumps({"readback": nested}), execution),
        )
        assert wf.progress(db, "go_test")["published_count"] == 0
        nested[field] = original_value
    db.execute(
        "UPDATE task_external_effects SET details=?,external_id='999' WHERE task_id=?",
        (json.dumps({"readback": nested}), execution),
    )
    assert wf.progress(db, "go_test")["published_count"] == 0
    db.execute(
        "UPDATE task_external_effects SET external_id='222' WHERE task_id=?",
        (execution,),
    )
    assert wf.progress(db, "go_test")["published_count"] == 1
    origin = kb.get_run(db, execution_run)
    nested["post_submit_or_pending_readback"]["observed_at"] = 0.5
    db.execute("UPDATE task_external_effects SET details=? WHERE task_id=?", (json.dumps({"readback": nested}), execution))
    assert wf.progress(db, "go_test")["published_count"] == 0
    nested["post_submit_or_pending_readback"]["observed_at"] = 100
    db.execute("UPDATE task_external_effects SET details=? WHERE task_id=?", (json.dumps({"readback": nested}), execution))
    publishing_scope = origin.metadata["loop_contract"].pop("facebook_group_publish")
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(origin.metadata), execution_run))
    assert wf.progress(db, "go_test")["published_count"] == 0
    origin.metadata["loop_contract"]["facebook_group_publish"] = publishing_scope
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(origin.metadata), execution_run))
    nested["post_submit_or_pending_readback"].update(status="pending", pending_review=True)
    db.execute("UPDATE task_external_effects SET details=? WHERE task_id=?", (json.dumps({"readback": nested}), execution))
    assert wf.progress(db, "go_test")["published_count"] == 0
    nested["post_submit_or_pending_readback"].update(status="published", pending_review=False)
    db.execute("UPDATE task_external_effects SET details=?,updated_at=999 WHERE task_id=?", (json.dumps({"readback": nested}), execution))
    assert wf.progress(db, "go_test")["published_count"] == 0


def test_corrected_plan_removes_only_unbound_planned_stage_and_keeps_snapshot(db):
    kb.ensure_grace_objective_stage(db, objective_id="go_test", stage_key="typo")
    result = _plan(db)
    assert "typo" not in [s["stage_key"] for s in result["stages"]]
    before = json.loads(db.execute("SELECT snapshot FROM grace_objective_revisions").fetchone()[0])
    assert "typo" in [s["stage_key"] for s in before["stages"]]


def test_plan_preserves_omitted_next_action(db):
    _plan(db, next_action="Refresh chooser")
    assert _plan(db, expected_revision=2)["objective"]["next_action"] == "Refresh chooser"


def test_plan_canonicalizes_project_before_immutable_binding(db):
    _plan(db, workflow={"project": " secondhand ", "source_listing_id": "12345", "expected_destinations": 2, "excluded_destination_names": [" Old group "]})
    assert wf.configuration(db, "go_test")["project"] == "secondhand"
    assert wf.configuration(db, "go_test")["excluded_destination_names"] == ["Old group"]
    _plan(db, expected_revision=2, workflow=wf.configuration(db, "go_test"))


def test_reservation_rejects_contract_objective_mismatch(db):
    _plan(db)
    with pytest.raises(ValueError, match="objective/stage differs"):
        kb.reserve_grace_delegation(
            db, contract_fingerprint="e" * 64, request_instance_id="mismatch",
            platform="telegram", chat_id="chat", thread_id="2", session_key="session",
            session_id="session", resolved_route={"backend": "test"}, approval_required=False,
            objective_id="go_test", stage_key="preflight", publication_contract=_publish(),
        )


def test_legacy_review_is_context_only_and_mismatched_run_is_rejected(db):
    execution, review, _, review_run = _pair(db)
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"review_outcome": "accepted"}), review_run))
    spec = {"project": "secondhand", "source_listing_id": "12345", "expected_destinations": 2,
            "historical_evidence": [{"execution_task_id": execution, "review_task_id": review}]}
    result = _plan(db, workflow=spec)
    assert result["publication_progress"]["historical_evidence"][0]["review_binding"] == "unverified_legacy_context_only"
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"review_outcome": "accepted", "evidence": {"parent_execution_task_id": execution, "parent_execution_run_id": 99999}}), review_run))
    replanned = _plan(db, expected_revision=2, workflow=spec)
    assert replanned["publication_progress"]["historical_evidence"] == result["publication_progress"]["historical_evidence"]
    with pytest.raises(ValueError, match="different execution run"):
        wf._history_reference(db, kb.get_grace_objective(db, "go_test"), "secondhand", "12345", spec["historical_evidence"][0])
    spec["historical_evidence"][0]["execution_run_id"] = 99999
    with pytest.raises(ValueError, match="pinned historical"):
        _plan(db, expected_revision=3, workflow=spec)


def test_readonly_reconciliation_advances_pending_without_new_external_effect(db):
    _plan(db)
    publisher, _, publisher_run, _ = _pair(db, objective_id="go_test", publish_groups=("222",))
    post_url = "https://www.facebook.com/groups/222/posts/555"
    details = {"action": "publish_existing_listing", "source_listing_id": "12345", "canonical_name": "Telescope trade",
               "canonical_url": "https://www.facebook.com/groups/222", "publication_state": "pending", "post_url": post_url}
    db.execute("INSERT INTO task_external_effects(task_id,platform,effect_key,state,external_id,details,created_at,updated_at) VALUES (?,'facebook','group:222','verified','222',?,1,1)", (publisher, json.dumps(details)))
    observer, _, run_id, review_run_id = _pair(db, objective_id="go_test")
    run = kb.get_run(db, run_id)
    receipt = {"status": "published", "visible": True, "pending_review": False, "group_id": "222", "source_listing_id": "12345", "post_url": post_url, "observed_at": 100}
    run.metadata["acceptance_evidence"]["publication_reconciliation"] = [receipt]
    receipt["observed_at"] = 9999999999999
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    assert wf.progress(db, "go_test")["published_count"] == 0
    receipt["observed_at"] = 100
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    review_metadata = kb.get_run(db, review_run_id).metadata
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(
        kb.get_run(db, run_id)
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    assert wf.progress(db, "go_test")["published_count"] == 0
    db.execute("UPDATE task_external_effects SET run_id=? WHERE task_id=?", (publisher_run, publisher))
    receipt["post_url"] = "https://www.facebook.com/groups/222/posts/other"
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(
        kb.get_run(db, run_id)
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    assert wf.progress(db, "go_test")["published_count"] == 0
    receipt["post_url"] = post_url
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(
        kb.get_run(db, run_id)
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    assert wf.progress(db, "go_test")["published_count"] == 1
    run.metadata["loop_contract"]["scope"]["allowed"] = ["Read listing 12345 and unrelated group 333"]
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    assert wf.progress(db, "go_test")["published_count"] == 0
    run.metadata["loop_contract"]["scope"]["allowed"] = ["Read listing 12345 and group 222"]
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    assert kb.list_external_effects(db, observer) == []
    assert kb.list_external_effects(db, publisher)[0]["details"] == details
    receipt.update(status="rejected", visible=False, observed_at=200)
    receipt.pop("post_url")
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(
        kb.get_run(db, run_id)
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    assert wf.progress(db, "go_test")["submission_capacity"] == 1
    receipt["post_url"] = post_url
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(run.metadata), run_id))
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = kb.workflow_review_evidence_hash(
        kb.get_run(db, run_id)
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(review_metadata), review_run_id))
    assert wf.progress(db, "go_test")["published_count"] == 0
    assert wf.progress(db, "go_test")["submission_capacity"] == 2
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"review_outcome": "accepted"}), review_run_id))
    assert wf.progress(db, "go_test")["submission_capacity"] == 1


def test_readonly_scope_requires_exact_group_alias():
    contract = {
        "routing": {
            "task_type": "facebook_marketplace_readonly",
            "resolved": {"assignment": {"interaction_mode": "interactive_readonly"}},
        },
        "scope": {
            "allowed": [
                "Read listing 12345 and Facebook group Telescope trade archive"
            ]
        },
    }

    assert not wf._readonly_contract_scope(
        contract, "12345", "222", "Telescope trade"
    )
    contract["scope"]["allowed"] = [
        "Read listing 12345 and Facebook group Telescope trade"
    ]
    assert wf._readonly_contract_scope(
        contract, "12345", "222", "Telescope trade"
    )


@pytest.mark.parametrize("stored_report", [None, "bad", ["bad"], {"complete": 1}, {"complete": False}])
def test_content_report_and_gateway_rebuild_do_not_claim_objective_success(db, monkeypatch, stored_report):
    _plan(db)
    execution, _, run_id, _ = _pair(db, objective_id="go_test")
    report = {"kind": "content_package", "delivery": "inline_only", "complete": True,
              "title": "Inventory", "body": "0 published; 2 remain", "body_field": "inventory",
              "observed_at": 100, "assets": []}
    canonical = kb.canonical_objective_report(db, execution, report)
    assert canonical["complete"] is False
    assert report["complete"] is True  # Keep raw producer evidence unchanged.
    monkeypatch.setattr(kb, "grace_user_facing_delivery_contract", lambda *_: {
        "required": True, "kind": "content_package", "delivery": "inline_only", "body_field": "inventory"})
    db.execute("UPDATE task_runs SET metadata=?,started_at=?,ended_at=? WHERE id=?", (
        json.dumps({"acceptance_evidence": {"inventory": report["body"]}, "user_facing_report": stored_report}),
        1_700_000_000, 1_700_000_001, run_id))
    rebuilt = kb.grace_inline_content_package_report(db, execution)
    if stored_report == {"complete": 1}:
        assert rebuilt is None  # A malformed producer boolean is rejected.
    else:
        assert rebuilt["complete"] is False
        assert rebuilt["body"] == report["body"]
    assert wf.progress(db, "go_test")["complete"] is False


def test_intermediate_full_publication_package_is_complete_without_closing_objective(db):
    _plan(db)
    execution, _, _, _ = _pair(db, objective_id="go_test")
    report = {
        "kind": "content_package",
        "package_kind": "full_publication_package",
        "complete": True,
    }

    canonical = kb.canonical_objective_report(db, execution, report)

    assert canonical["complete"] is True
    assert wf.progress(db, "go_test")["complete"] is False


def test_unbound_content_report_preserves_completion(db):
    task = kb.create_task(db, title="Standalone content")
    for complete in (True, False):
        report = {"kind": "content_package", "complete": complete}
        assert kb.canonical_objective_report(db, task, report) == report


def test_terminal_report_preserves_claim_until_post_review_progress_gate(db):
    _plan(db)
    execution, _, _, _ = _pair(db, objective_id="go_test")
    db.execute(
        "UPDATE grace_delegations SET stage_key='terminal' WHERE execution_task_id=?",
        (execution,),
    )
    report = {"kind": "content_package", "complete": True}

    canonical = kb.canonical_objective_report(db, execution, report)

    assert canonical["complete"] is True
    assert wf.progress(db, "go_test")["complete"] is False


@pytest.mark.parametrize("fault", ["missing_coverage", "missing_listing", "missing_availability", "duplicate_row", "unknown_eligible", "selected", "wrong_url"])
def test_shared_preflight_rejects_same_evidence_at_review_and_publication(db, fault):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    metadata = kb.latest_run(db, execution).metadata
    evidence = metadata["acceptance_evidence"]
    if fault == "missing_coverage":
        evidence.pop("coverageReconciliation")
    elif fault == "missing_listing":
        evidence.pop("sourceListing")
    elif fault == "missing_availability":
        evidence["sourceListing"].pop("list_in_more_places_available")
    elif fault == "duplicate_row":
        evidence["groups"].append(dict(evidence["groups"][0]))
    elif fault == "unknown_eligible":
        evidence["coverageReconciliation"]["verified_eligible_for_later_approval"].append("333")
    elif fault == "selected":
        evidence["groups"][0]["chooser_selectability"] = "selected"
    else:
        evidence["groups"][0]["chooser_canonical_url"] = "https://www.facebook.com/groups/333"
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run))
    review_run = kb.latest_run(db, review)
    review_metadata = review_run.metadata
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = (
        kb.workflow_review_evidence_hash(kb.get_run(db, run))
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(review_metadata), review_run.id),
    )
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight",
        preflight_source={"execution_task_id": execution, "review_task_id": review})
    with pytest.raises(ValueError, match="(?i)preflight"):
        wf.resolve_preflight(db, contract)
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    with pytest.raises(ValueError, match="(?i)preflight"):
        kb.complete_task(db, review, summary="accepted", metadata={"review_outcome": "accepted",
            "evidence": {"parent_execution_task_id": execution, "parent_execution_run_id": run}})
    assert kb.get_task(db, review).status == "running"
    assert wf.progress(db, "go_test")["published_count"] == 0


def test_alias_scoped_preflight_resolves_discovered_group_identity(db):
    _plan(db)
    execution, review, run, review_run = _pair(db, objective_id="go_test")
    execution_metadata = kb.get_run(db, run).metadata
    execution_metadata["loop_contract"]["scope"]["allowed"] = [
        "Read Marketplace source listing 12345 and Facebook group Telescope trade"
    ]
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(execution_metadata), run),
    )
    review_metadata = kb.get_run(db, review_run).metadata
    review_metadata["evidence"]["parent_execution_evidence_sha256"] = (
        kb.workflow_review_evidence_hash(kb.get_run(db, run))
    )
    review_metadata["workflow_review_source"]["parent_execution_evidence_sha256"] = (
        review_metadata["evidence"]["parent_execution_evidence_sha256"]
    )
    db.execute(
        "UPDATE task_runs SET metadata=? WHERE id=?",
        (json.dumps(review_metadata), review_run),
    )
    contract = _publish()
    contract["facebook_group_publish"].update(
        mode="accepted_preflight",
        preflight_source={"execution_task_id": execution, "review_task_id": review},
    )

    resolved = wf.resolve_preflight(db, contract)

    identity = resolved["preflight_evidence"]["destination_identity"][0]
    assert identity["group_id"] == "222"
    assert identity["requested_alias"] == "Telescope trade"
    assert identity["canonical_name"] == "Telescope trade"


def test_unavailable_chooser_is_valid_evidence_but_never_publication_authority(db):
    from hermes_cli.facebook_group_preflight import validate
    _, _, run, _ = _pair(db, objective_id="go_test")
    evidence = json.loads(db.execute("SELECT metadata FROM task_runs WHERE id=?", (run,)).fetchone()[0])["acceptance_evidence"]
    evidence["sourceListing"]["list_in_more_places_available"] = False
    evidence["coverageReconciliation"]["verified_eligible_for_later_approval"] = []
    evidence["groups"][0].update(chooser_presence="absent", chooser_selectability="unknown",
                                 chooser_group_id=None, chooser_canonical_url=None)
    assert list(validate(evidence, started_at=1, ended_at=200)) == ["222"]


@pytest.mark.parametrize("source_changes", [False, True])
def test_claimed_review_binds_system_input_and_rejects_changed_parent(db, source_changes):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    claimed = kb.claim_task(db, review)
    assert claimed is not None
    attempt = kb.latest_run(db, review)
    assert attempt.metadata["workflow_review_source"]["parent_execution_run_id"] == run
    if source_changes:
        db.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome,metadata) "
                   "SELECT task_id,profile,status,started_at,ended_at,outcome,metadata FROM task_runs WHERE id=?", (run,))
        with pytest.raises(ValueError, match="source changed after claim"):
            kb.complete_task(db, review, metadata={"review_outcome": "accepted"})
        assert kb.get_task(db, review).status == "running"
    else:
        assert kb.complete_task(db, review, metadata={"review_outcome": "accepted",
            "evidence": {"parent_execution_task_id": "t_wrong", "parent_execution_run_id": 999}})
        receipt = kb.latest_run(db, review).metadata["evidence"]
        assert receipt["parent_execution_task_id"] == execution
        assert receipt["parent_execution_run_id"] == run
        contract = _publish()
        contract["facebook_group_publish"].update(mode="accepted_preflight",
            preflight_source={"execution_task_id": execution, "review_task_id": review})
        assert wf.resolve_preflight(db, contract)["preflight_evidence"]["execution_run_id"] == run


@pytest.mark.parametrize("status,claim", [
    ("ready", kb.claim_task), ("review", kb.claim_review_task),
])
def test_review_source_drift_prevents_claim_without_consuming_attempt(
    db, tmp_path, monkeypatch, status, claim,
):
    _plan(db)
    _, review, _, previous_run = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status=? WHERE id=?", (status, review))
    source = tmp_path / "kanban_db.py"
    source.write_bytes(b"loaded version")
    monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SOURCES", (source,))
    monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", kb._review_runtime_digest())
    source.write_bytes(b"updated version")

    with pytest.raises(RuntimeError, match="restart the dispatcher/gateway"):
        claim(db, review)
    task = kb.get_task(db, review)
    assert task.status == status
    assert task.current_run_id is None
    assert task.claim_lock is None
    assert kb.latest_run(db, review).id == previous_run

    # Loading the updated code allows a fresh attempt with the exact source.
    monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", kb._review_runtime_digest())
    assert claim(db, review) is not None
    assert kb.latest_run(db, review).metadata["workflow_review_source"] == kb._workflow_review_source(db, review)


def test_fresh_worker_rejects_runtime_changed_after_review_claim(db, monkeypatch):
    _plan(db)
    _, review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    task = kb.claim_task(db, review)
    auth = dict(task_id=review, run_id=str(task.current_run_id),
                claim_lock=task.claim_lock, worker_auth_token=task.worker_auth_token)
    assert kb.validate_kanban_worker_auth(db, **auth)
    monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", hashlib.sha256(b"new worker source").digest())

    with pytest.raises(kb.WorkerAuthorizationError, match="runtime changed after claim"):
        kb.validate_kanban_worker_auth(db, **auth)
    assert kb.get_task(db, review).current_run_id == task.current_run_id
    assert kb.latest_run(db, review).outcome is None


def test_unclaimed_workflow_review_cannot_mint_accepted_authority(db):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    with pytest.raises(ValueError, match="requires a claimed attempt before acceptance"):
        kb.complete_task(db, review, metadata={"review_outcome": "accepted", "evidence": {
            "parent_execution_task_id": execution,
            "parent_execution_run_id": run,
        }})
    assert kb.get_task(db, review).status == "ready"


def test_publication_review_does_not_apply_zero_effect_preflight_schema(db):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    metadata = kb.latest_run(db, execution).metadata
    metadata["loop_contract"]["routing"]["task_type"] = "browser_publish"
    metadata["loop_contract"].pop("evidence_contract")
    metadata["acceptance_evidence"] = {"sourceListing": {"listing_id": "12345",
        "list_in_more_places_available": True}, "sideEffectsPerformed": True}
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run))
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, metadata={"review_outcome": "accepted"})
    assert kb.latest_run(db, review).metadata["evidence"]["parent_execution_run_id"] == run


def test_generic_readonly_review_does_not_require_undeclared_chooser_evidence(db):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    metadata = kb.latest_run(db, execution).metadata
    metadata["loop_contract"].pop("evidence_contract")
    metadata["acceptance_evidence"] = {"candidate_research": "observed candidate descriptions"}
    db.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run))
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, metadata={"review_outcome": "accepted"})
    contract = _publish()
    contract["facebook_group_publish"].update(mode="accepted_preflight",
        preflight_source={"execution_task_id": execution, "review_task_id": review})
    with pytest.raises(ValueError):
        wf.resolve_preflight(db, contract)


@pytest.mark.parametrize("after_acceptance", [False, True])
@pytest.mark.parametrize("edit_contract", [False, True])
def test_native_evidence_edit_invalidates_claimed_or_accepted_review(db, after_acceptance, edit_contract):
    _plan(db)
    execution, review, run, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    if after_acceptance:
        assert kb.complete_task(db, review, metadata={"review_outcome": "accepted"})
    edited = kb.latest_run(db, execution).metadata
    if edit_contract:
        edited["loop_contract"].pop("evidence_contract")
    else:
        edited["acceptance_evidence"]["groups"][0]["readable_name"] = "Changed evidence"
    assert kb.edit_completed_task_result(db, execution, result="corrected", metadata=edited)
    if after_acceptance:
        contract = _publish()
        contract["facebook_group_publish"].update(mode="accepted_preflight",
            preflight_source={"execution_task_id": execution, "review_task_id": review})
        with pytest.raises(ValueError, match="changed after review"):
            wf.resolve_preflight(db, contract)
        assert run not in wf._accepted_execution_runs(db, "go_test", execution)
    else:
        with pytest.raises(ValueError, match="source changed after claim"):
            kb.complete_task(db, review, metadata={"review_outcome": "accepted"})


@pytest.mark.parametrize("suffix,valid", [("/", True), ("", True), ("//", False), ("?id=other", False)])
def test_preflight_canonical_url_optional_trailing_slash(db, suffix, valid):
    from hermes_cli.facebook_group_preflight import validate
    _, _, run_id, _ = _pair(db)
    run = kb.get_run(db, run_id)
    evidence = run.metadata["acceptance_evidence"]
    evidence["groups"][0]["canonical_url"] += suffix
    if valid:
        assert "222" in validate(evidence, started_at=1, ended_at=200)
    else:
        with pytest.raises(ValueError, match="group identity"):
            validate(evidence, started_at=1, ended_at=200)


@pytest.mark.parametrize("field", ["parent_execution_evidence_sha256", "parent_execution_run_id", "workflow_review_source"])
def test_completed_review_correction_cannot_downgrade_binding(db, field):
    _plan(db)
    _, review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, metadata={"review_outcome": "accepted"})
    original = kb.latest_run(db, review).metadata
    edited = json.loads(json.dumps(original))
    (edited if field == "workflow_review_source" else edited["evidence"]).pop(field)
    with pytest.raises(ValueError, match="metadata is immutable"):
        kb.edit_completed_task_result(db, review, result="correction", metadata=edited)
    assert kb.latest_run(db, review).metadata == original
    assert kb.edit_completed_task_result(db, review, result="prose correction", metadata=original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("review_outcome", "rejected"),
        ("policy_receipts", []),
        ("external_effects", [{"platform": "facebook"}]),
        ("verification_notes", ["replacement evidence"]),
    ],
)
def test_completed_workflow_review_cannot_rewrite_acceptance_metadata(
    db, field, value,
):
    _plan(db)
    _, review, _, _ = _pair(db, objective_id="go_test")
    db.execute("UPDATE tasks SET status='ready' WHERE id=?", (review,))
    assert kb.claim_task(db, review)
    assert kb.complete_task(db, review, metadata={"review_outcome": "accepted"})
    original = kb.latest_run(db, review).metadata
    edited = json.loads(json.dumps(original))
    edited[field] = value

    with pytest.raises(ValueError, match="metadata is immutable"):
        kb.edit_completed_task_result(
            db,
            review,
            result="authority rewrite",
            metadata=edited,
        )

    assert kb.latest_run(db, review).metadata == original


def test_recovery_excludes_only_its_own_reserved_delegation(db):
    _plan(db)
    first, _, _, _ = _pair(db, objective_id='go_test')
    second, _, _, _ = _pair(db, objective_id='go_test')
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (first,))
    assert wf.has_in_flight_delegation(db, 'go_test')
    assert not wf.has_in_flight_delegation(db, 'go_test', excluding_delegation_id='gd_'+first)
    db.execute("UPDATE tasks SET status='running' WHERE id=?", (second,))
    assert wf.has_in_flight_delegation(db, 'go_test', excluding_delegation_id='gd_'+first)
