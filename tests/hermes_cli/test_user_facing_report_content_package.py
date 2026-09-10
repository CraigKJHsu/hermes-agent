from __future__ import annotations

import hashlib
import json
import time

import pytest
from PIL import Image

from hermes_cli import kanban_db as kb
from proactive import model_routing, policy_registry
from hermes_cli.user_facing_report import (
    delivery_contract_from_report,
    normalize_user_facing_report,
    render_user_facing_report_chunks,
    report_satisfies_user_facing_delivery,
)


def _inline_text_report() -> dict:
    return {
        "kind": "content_package",
        "delivery": "inline_only",
        "complete": True,
        "title": "Tasker 提案",
        "body": "完整繁體中文提案正文",
        "body_field": "finalPasteReadyDraft",
        "observed_at": int(time.time()),
        "assets": [],
    }


def _full_publication_report() -> dict:
    sections = {
        "facebook_page_post": "完整 Facebook Page 內文",
        "facebook_group_post": "完整 Facebook Group 討論附文",
        "gemini_notebook_prompt": "完整 Gemini Notebook Prompt",
        "podcast_title": "完整 Podcast 標題",
        "podcast_description": "完整 Podcast 說明",
    }
    return {
        "kind": "content_package",
        "package_kind": "full_publication_package",
        "delivery": "inline_with_attachment",
        "complete": False,
        "title": "D Squared EP06 完整發布包",
        "body": "\n\n".join(sections.values()),
        "observed_at": int(time.time()),
        "sections": sections,
        "assets": [
            {
                "filename": "page.png",
                "label": "Page Hero",
                "path": "/tmp/page.png",
                "sha256": "1" * 64,
                "asset_family": "page_hero",
                "dimensions": "1600x900",
            },
            {
                "filename": "audio.png",
                "label": "Audio Brief",
                "path": "/tmp/audio.png",
                "sha256": "2" * 64,
                "asset_family": "audio_brief",
                "width": 1200,
                "height": 1200,
            },
        ],
        "policy_receipts": [{
            "role": "execution",
            "policy_id": "ai-bizweek-brand-channel",
            "version": "1",
            "sha256": "3" * 64,
            "loaded": True,
        }],
        "external_effects": [],
    }


def test_inline_text_content_package_round_trips_delivery_contract():
    report = normalize_user_facing_report(_inline_text_report())
    contract = delivery_contract_from_report(report)

    assert contract == {
        "required": True,
        "kind": "content_package",
        "delivery": "inline_only",
        "body_field": "finalPasteReadyDraft",
    }
    assert report_satisfies_user_facing_delivery(report, contract)
    assert "完整繁體中文提案正文" in "".join(
        render_user_facing_report_chunks(report)
    )


def test_inline_text_content_package_rejects_assets():
    report = _inline_text_report()
    report["assets"] = [{
        "filename": "unexpected.png",
        "label": "unexpected",
        "path": "/tmp/unexpected.png",
        "sha256": "0" * 64,
    }]

    with pytest.raises(ValueError, match="assets must be empty"):
        normalize_user_facing_report(report)


def test_incomplete_content_is_valid_delivery_but_cannot_close_outcome():
    from hermes_cli.user_facing_report import (
        report_matches_user_facing_delivery, report_allows_closed_outcome,
    )
    raw = {**_inline_text_report(), "complete": False}
    report = normalize_user_facing_report(raw)
    contract = delivery_contract_from_report(report)
    assert report["complete"] is False
    assert report_matches_user_facing_delivery(report, contract)
    assert not report_satisfies_user_facing_delivery(report, contract)
    assert not report_allows_closed_outcome(report)
    with pytest.raises(ValueError):
        normalize_user_facing_report({**raw, "body": ""})


@pytest.mark.parametrize("complete", [None, 0, 1, "false", "true"])
def test_boolean_flag(complete):
    with pytest.raises(ValueError, match="boolean"):
        normalize_user_facing_report({**_inline_text_report(), "complete": complete})


def test_full_publication_package_preserves_structured_manifest():
    report = normalize_user_facing_report(_full_publication_report())

    assert report["package_kind"] == "full_publication_package"
    assert set(report["sections"]) == {
        "facebook_page_post", "facebook_group_post", "gemini_notebook_prompt",
        "podcast_title", "podcast_description",
    }
    assert {(asset["asset_family"], asset["dimensions"]) for asset in report["assets"]} == {
        ("page_hero", "1600x900"), ("audio_brief", "1200x1200"),
    }
    assert report["complete"] is False
    assert report["external_effects"] == []


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda report: report["sections"].pop("facebook_group_post"), "facebook_group_post"),
        (lambda report: report["assets"].pop(), "page_hero and one audio_brief"),
        (lambda report: report.update(external_effects=[{"platform": "facebook"}]), "external_effects"),
    ],
)
def test_full_publication_package_fails_closed_when_manifest_is_incomplete(mutation, match):
    report = _full_publication_report()
    mutation(report)

    with pytest.raises(ValueError, match=match):
        normalize_user_facing_report(report)


@pytest.mark.parametrize("versioned,empty_policy", [(False, False), (True, False), (True, True)])
def test_accepted_full_publication_selector_rejects_audio_only_patch(tmp_path, monkeypatch, versioned, empty_policy):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    page = tmp_path / "page.png"
    audio = tmp_path / "audio.png"
    Image.new("RGB", (1600, 900)).save(page)
    Image.new("RGB", (1200, 1200)).save(audio)
    report = _full_publication_report()
    report["assets"][0].update(path=str(page), sha256=hashlib.sha256(page.read_bytes()).hexdigest())
    report["assets"][1].update(path=str(audio), sha256=hashlib.sha256(audio.read_bytes()).hexdigest())
    execution_receipts = report["policy_receipts"]
    review_receipts = [{
        **execution_receipts[0],
        "role": "review",
        "latest_active_verified": True,
    }]
    identity = {
        "platform": "telegram",
        "chat_id": "chat-1",
        "thread_id": "4641",
        "project": "d-squared",
    }
    behavior = {}
    if versioned:
        from proactive.behavior_profiles import registry as br
        policy_registry.create_policy_version("full-package-test", "v1", "Pinned policy", owner_scope="topic", owner_id="fixture", activate=True)
        policy_registry.bind_topic_policies("telegram:chat-1:4641/d-squared", [] if empty_policy else [{"policy_id": "full-package-test", "resolution": "latest_active"}])
        with kb.connect_closing() as conn:
            br.set_selection(conn, platform="telegram", chat_id="chat-1", thread_id="4641", project="d-squared", profile_id="ai_bizweek", version="v1", expected_revision=0, reason="Package test")
            kb.create_grace_objective(conn, objective_id="go_package", platform="telegram", chat_id="chat-1", thread_id="4641", session_key="test", title="Package", objective="Package", original_request_sha256="a" * 64, required_stage_keys=["prepare"], terminal_stage_key="prepare", acceptance_criteria=["verified"], behavior_project="d-squared")
            behavior = {"memory": {"namespace": "telegram:chat-1:4641/d-squared"}, "objective_ref": {"objective_id": "go_package"}, "behavior_pin": br.get_pin(conn, "go_package")}
        review_receipts[0].pop("latest_active_verified")
        review_receipts[0]["pinned_version_verified"] = True
    if empty_policy:
        report["policy_receipts"] = []
        execution_receipts = []
        review_receipts = []
    claim_sources = {}
    policy_validation_roles = []
    monkeypatch.setattr(
        kb,
        "_workflow_review_source",
        lambda _conn, task_id: claim_sources.get(task_id),
    )
    monkeypatch.setattr(
        policy_registry,
        "policy_refs_from_task_body",
        lambda _body: [{"policy_id": "ai-bizweek-brand-channel"}],
    )
    def verify_policies(body, metadata, *, role):
        policy_validation_roles.append(role)
        if empty_policy:
            br.validate_task_policy_completion(body, metadata, role)
    monkeypatch.setattr(policy_registry, "validate_policy_completion", verify_policies)
    monkeypatch.setattr(
        model_routing,
        "execution_receipt_from_env",
        lambda _value: {"controller_attested": True},
    )
    monkeypatch.setattr(
        model_routing,
        "validate_grace_acceptance_receipt",
        lambda _receipt, **_kwargs: None,
    )

    def contract_body(stage):
        contract = {"identity": identity, "external_effect_budget": 0, **behavior}
        return (
            f"GRACE_LOOP_CONTRACT_STAGE: {stage}\n"
            + ("GRACE_BEHAVIOR_PIN: " + json.dumps(contract) + "\n" if versioned else "")
            + "HERMES_LOOP_CONTRACT:\n```json\n"
            f"{json.dumps(contract)}\n```"
        )

    def create_pair(
        conn,
        suffix,
        source_report,
        *,
        bind_review=True,
        review_effects=None,
        zero_effect_attested=True,
    ):
        execution_id = kb.create_task(
            conn,
            title=f"execution {suffix}",
            body=contract_body("execution"),
            project_namespace="d-squared",
        )
        assert kb.claim_task(conn, execution_id)
        assert kb.complete_task(conn, execution_id, metadata={
            "loop_contract": {"identity": identity, "external_effect_budget": 0, **behavior},
            "user_facing_report": source_report,
            "policy_receipts": execution_receipts,
            "external_effects": [],
            "external_effect_budget": 0,
            **(
                {"read_only_zero_external_effects": True}
                if zero_effect_attested
                else {}
            ),
        })
        review_id = kb.create_task(
            conn,
            title=f"review {suffix}",
            body=contract_body("grace_review"),
            parents=(execution_id,),
        )
        source_run = kb.latest_run(conn, execution_id)
        assert source_run is not None
        review_source = {
            "parent_execution_task_id": execution_id,
            "parent_execution_run_id": source_run.id,
            "parent_execution_evidence_sha256": (
                kb.workflow_review_evidence_hash(source_run)
            ),
            "parent_task_body_sha256": hashlib.sha256(
                contract_body("execution").encode("utf-8")
            ).hexdigest(),
            "review_task_body_sha256": hashlib.sha256(
                contract_body("grace_review").encode("utf-8")
            ).hexdigest(),
            **kb._review_runtime_receipt(conn, review_id),
        }
        if bind_review:
            claim_sources[review_id] = review_source
        assert kb.claim_task(conn, review_id)
        review_metadata = {
            "review_outcome": "accepted",
            "evidence": {"manifest": "verified"},
            "policy_receipts": review_receipts,
            "external_effects": review_effects if review_effects is not None else [],
            # A completion-only source must not gain controller authority.
            "workflow_review_source": review_source,
        }
        assert kb.complete_task(conn, review_id, metadata=review_metadata)
        conn.execute(
            "INSERT INTO grace_delegations (delegation_id, contract_fingerprint, "
            "request_instance_id, platform, chat_id, thread_id, session_key, session_id, "
            "resolved_route, approval_required, state, execution_task_id, review_task_id, "
            "created_at, updated_at) VALUES (?, ?, ?, 'telegram', 'chat-1', '4641', "
            "'session', 'session', '{}', 0, 'queued', ?, ?, ?, ?)",
            (f"gd-{suffix}", suffix * 64, suffix, execution_id, review_id, int(time.time()), int(time.time())),
        )
        return execution_id, review_id

    with kb.connect_closing() as conn:
        full_execution, full_review = create_pair(conn, "a", {**report, "complete": True})
        audio_report = {
            key: value for key, value in report.items()
            if key not in {"package_kind", "sections", "policy_receipts", "external_effects"}
        }
        audio_report["title"] = "Audio-only semantic fix"
        audio_report["body"] = "Audio-only semantic fix"
        audio_report["assets"] = [audio_report["assets"][1]]
        audio_execution, audio_review = create_pair(conn, "b", audio_report)
        incomplete_execution, incomplete_review = create_pair(conn, "c", report)
        unbound_execution, unbound_review = create_pair(
            conn, "e", {**report, "complete": True}, bind_review=False,
        )

        wrong_size = tmp_path / "wrong-size.png"
        Image.new("RGB", (800, 800)).save(wrong_size)
        wrong_size_report = _full_publication_report()
        wrong_size_report["complete"] = True
        wrong_size_report["policy_receipts"] = execution_receipts
        wrong_size_report["assets"][0].update(
            path=str(wrong_size),
            sha256=hashlib.sha256(wrong_size.read_bytes()).hexdigest(),
        )
        wrong_size_report["assets"][1].update(
            path=str(audio),
            sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
        )
        wrong_size_execution, wrong_size_review = create_pair(
            conn, "d", wrong_size_report,
        )
        effect_execution, effect_review = create_pair(
            conn, "f", {**report, "complete": True},
        )
        conn.execute(
            "INSERT INTO task_external_effects ("
            "task_id, platform, effect_key, state, external_id, created_at, updated_at"
            ") VALUES (?, 'facebook', 'create', 'existing', 'post-1', ?, ?)",
            (effect_execution, int(time.time()), int(time.time())),
        )
        review_effect_execution, review_effect_review = create_pair(
            conn,
            "g",
            {**report, "complete": True},
            review_effects=[{
                "platform": "facebook",
                "state": "existing",
                "external_id": "post-2",
            }],
        )
        unattested_execution, unattested_review = create_pair(
            conn,
            "h",
            {**report, "complete": True},
            zero_effect_attested=False,
        )
        manifest_execution, manifest_review = create_pair(
            conn, "i", {**report, "complete": True},
        )
        contract_drift_execution, contract_drift_review = create_pair(
            conn, "j", {**report, "complete": True},
        )
        conn.execute(
            "UPDATE tasks SET body=body || '\npost-review drift' WHERE id=?",
            (contract_drift_review,),
        )
        added = tmp_path / "added-after-review.txt"
        added.write_text("changed bundle", encoding="utf-8")
        kb.add_attachment(
            conn,
            manifest_execution,
            filename=added.name,
            stored_path=str(added),
            size=added.stat().st_size,
        )

        if versioned:
            # Installing another profile changes the process source inventory,
            # while this accepted package retains its own immutable provenance.
            changed_process = hashlib.sha256(b"another profile installed").digest()
            monkeypatch.setattr(kb, "_REVIEW_RUNTIME_SHA256", changed_process)
            monkeypatch.setattr(kb, "_review_runtime_digest", lambda: changed_process)
        selected = kb.accepted_full_publication_package(
            conn,
            execution_task_id=full_execution,
            review_task_id=full_review,
            identity=identity,
        )
        assert selected["sections"] == report["sections"]
        assert selected["external_effects"] == []
        assert policy_validation_roles == ["execution", "review"]
        for incomplete_identity in (None, {}, {"project": "d-squared"}):
            with pytest.raises(ValueError, match="another Topic or project"):
                kb.accepted_full_publication_package(
                    conn,
                    execution_task_id=full_execution,
                    review_task_id=full_review,
                    identity=incomplete_identity,
                )
        with pytest.raises(ValueError, match="not a full publication package"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=audio_execution,
                review_task_id=audio_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="not a full publication package"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=incomplete_execution,
                review_task_id=incomplete_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="dimensions do not match"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=wrong_size_execution,
                review_task_id=wrong_size_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="does not bind exact execution evidence"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=unbound_execution,
                review_task_id=unbound_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="zero external effects"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=effect_execution,
                review_task_id=effect_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="zero-effect admission"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=unattested_execution,
                review_task_id=unattested_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="attachment manifest changed"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=manifest_execution,
                review_task_id=manifest_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="task contract changed"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=contract_drift_execution,
                review_task_id=contract_drift_review,
                identity=identity,
            )
        with pytest.raises(ValueError, match="zero external effects"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=review_effect_execution,
                review_task_id=review_effect_review,
                identity=identity,
            )

        def reject_review_policy(_body, _metadata, *, role):
            if role == "review":
                raise policy_registry.PolicyRegistryError("stale review policy")

        monkeypatch.setattr(
            policy_registry,
            "validate_policy_completion",
            reject_review_policy,
        )
        with pytest.raises(ValueError, match="not registry-verified"):
            kb.accepted_full_publication_package(
                conn,
                execution_task_id=full_execution,
                review_task_id=full_review,
                identity=identity,
            )
