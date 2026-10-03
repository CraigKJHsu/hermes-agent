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
    promote_full_publication_package,
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


@pytest.mark.parametrize("fault", [None, "sha256", "family", "section", "effects"])
def test_generic_report_promotes_only_from_exact_full_publication_evidence(fault):
    full = _full_publication_report()
    generic = {
        key: value
        for key, value in full.items()
        if key not in {"package_kind", "sections", "policy_receipts", "external_effects"}
    }
    generic["assets"] = [
        {
            key: asset[key]
            for key in ("filename", "label", "path", "sha256")
        }
        for asset in full["assets"]
    ]
    evidence = {
        "sections": dict(full["sections"]),
        "asset_manifest": [dict(asset) for asset in full["assets"]],
    }
    effects = []
    if fault == "sha256":
        evidence["asset_manifest"][0]["sha256"] = "0" * 64
    elif fault == "family":
        evidence["asset_manifest"][0]["asset_family"] = "audio_brief"
    elif fault == "section":
        evidence["sections"].pop("podcast_title")
    elif fault == "effects":
        effects = [{"platform": "facebook"}]

    promoted = promote_full_publication_package(
        generic,
        evidence=evidence,
        expected_asset_filenames=["page.png", "audio.png"],
        policy_receipts=full["policy_receipts"],
        external_effects=effects,
    )

    if fault is None:
        assert normalize_user_facing_report(promoted)["package_kind"] == (
            "full_publication_package"
        )
    else:
        assert "package_kind" not in promoted


def test_generic_report_promotes_from_canonical_inline_section_headings():
    full = _full_publication_report()
    headings = (
        ("facebook_page_post", "## 1. Facebook Page 貼文"),
        ("facebook_group_post", "## 2. Facebook Group 討論附文"),
        ("gemini_notebook_prompt", "## 3. Gemini Notebook Audio Generation Prompt"),
        ("podcast_title", "## 4. Podcast Title"),
        ("podcast_description", "## 5. Podcast Description"),
    )
    body = "\n\n".join(
        f"{heading}\n\n{full['sections'][field]}" for field, heading in headings
    )
    generic = {
        key: value
        for key, value in full.items()
        if key not in {"package_kind", "sections", "policy_receipts", "external_effects"}
    }
    generic["body"] = body
    generic["complete"] = False
    generic["assets"] = [
        {key: asset[key] for key in ("filename", "label", "path", "sha256")}
        for asset in full["assets"]
    ]

    promoted = promote_full_publication_package(
        generic,
        evidence={"asset_manifest": [dict(asset) for asset in full["assets"]]},
        expected_asset_filenames=["page.png", "audio.png"],
        policy_receipts=full["policy_receipts"],
        external_effects=[],
    )

    assert promoted["package_kind"] == "full_publication_package"
    assert promoted["sections"] == full["sections"]


def test_generic_report_promotes_from_worker_bracket_headings_and_named_readback():
    full = _full_publication_report()
    headings = (
        ("facebook_page_post", "【Facebook Page 正文】"),
        ("facebook_group_post", "【Facebook Group 附文】"),
        ("gemini_notebook_prompt", "【Gemini Notebook Audio Generation Prompt】"),
        ("podcast_title", "【Podcast／Spotify Title】"),
        ("podcast_description", "【Podcast／Spotify Description】"),
    )
    body = "\n\n".join(
        f"{heading}\n\n{full['sections'][field]}" for field, heading in headings
    )
    generic = {
        key: value
        for key, value in full.items()
        if key not in {"package_kind", "sections", "policy_receipts", "external_effects"}
    }
    generic.update(body=body, complete=False)
    generic["assets"] = [
        {key: asset[key] for key in ("filename", "label", "path", "sha256")}
        for asset in full["assets"]
    ]

    readback = [dict(asset) for asset in full["assets"]]
    for asset in readback:
        width, height = (
            map(int, asset["dimensions"].split("x"))
            if isinstance(asset.get("dimensions"), str)
            else (asset.pop("width"), asset.pop("height"))
        )
        asset["dimensions"] = {
            "width": width, "height": height,
        }
    promoted = promote_full_publication_package(
        generic,
        evidence={"asset_readback": readback},
        expected_asset_filenames=["page.png", "audio.png"],
        policy_receipts=full["policy_receipts"],
        external_effects=[],
    )

    assert promoted["package_kind"] == "full_publication_package"
    assert promoted["complete"] is False
    assert promoted["sections"] == full["sections"]

    conflicting = [dict(asset) for asset in readback]
    conflicting[0]["dimensions"] = {"width": 1, "height": 1}
    rejected = promote_full_publication_package(
        generic,
        evidence={"first": readback, "conflicting": conflicting},
        expected_asset_filenames=["page.png", "audio.png"],
        policy_receipts=full["policy_receipts"],
        external_effects=[],
    )
    assert "package_kind" not in rejected


@pytest.mark.parametrize(
    "fault",
    ["missing", "duplicate", "reordered", "fenced", "fake_fence_close", "malformed"],
)
def test_inline_section_heading_promotion_fails_closed(fault):
    full = _full_publication_report()
    headings = [
        ("facebook_page_post", "## Facebook Page 內文"),
        ("facebook_group_post", "## Facebook Group 討論附文"),
        ("gemini_notebook_prompt", "## Gemini Notebook Prompt"),
        ("podcast_title", "## Podcast 標題"),
        ("podcast_description", "## Podcast 說明"),
    ]
    if fault == "missing":
        headings.pop()
    elif fault == "duplicate":
        headings.insert(1, headings[0])
    elif fault == "reordered":
        headings[0], headings[1] = headings[1], headings[0]
    body = "\n\n".join(
        f"{heading}\n\n{full['sections'][field]}" for field, heading in headings
    )
    if fault == "fenced":
        body = "```markdown\n" + body + "\n```"
    elif fault == "fake_fence_close":
        body = "```markdown\n```not-a-close\n" + body + "\n```"
    elif fault == "malformed":
        body = body.replace("## ", "##")
    generic = {
        "kind": "content_package",
        "delivery": "inline_with_attachment",
        "complete": False,
        "title": "package",
        "body": body,
        "observed_at": full["observed_at"],
        "assets": [
            {key: asset[key] for key in ("filename", "label", "path", "sha256")}
            for asset in full["assets"]
        ],
    }

    promoted = promote_full_publication_package(
        generic,
        evidence={"asset_manifest": [dict(asset) for asset in full["assets"]]},
        expected_asset_filenames=["page.png", "audio.png"],
        policy_receipts=full["policy_receipts"],
        external_effects=[],
    )

    assert "package_kind" not in promoted


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


def test_accepted_full_publication_selector_rejects_audio_only_patch(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        policy_registry,
        "validate_policy_completion",
        lambda _body, _metadata, *, role: policy_validation_roles.append(role),
    )
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
        contract = {"identity": identity, "external_effect_budget": 0}
        return (
            f"GRACE_LOOP_CONTRACT_STAGE: {stage}\n"
            "HERMES_LOOP_CONTRACT:\n```json\n"
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
        execution_metadata = {
            "loop_contract": {"identity": identity, "external_effect_budget": 0},
            "user_facing_report": source_report,
            **(
                {
                    "facebook_page_post": {
                        "text": source_report["sections"]["facebook_page_post"],
                    }
                }
                if source_report.get("package_kind")
                == "full_publication_package"
                else {}
            ),
            "external_effects": [],
            "external_effect_budget": 0,
            **(
                {"read_only_zero_external_effects": True}
                if zero_effect_attested
                else {}
            ),
        }
        execution_metadata["policy_receipts"] = execution_receipts
        assert kb.complete_task(conn, execution_id, metadata=execution_metadata)
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
            "review_runtime_sha256": kb._REVIEW_RUNTIME_SHA256.hex(),
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
        rejected_id = kb.create_task(
            conn,
            title="execution missing canonical Page text",
            body=contract_body("execution"),
        )
        assert kb.claim_task(conn, rejected_id)
        with pytest.raises(
            ValueError,
            match=r"metadata\.facebook_page_post\.text",
        ):
            kb.complete_task(
                conn,
                rejected_id,
                metadata={
                    "loop_contract": {
                        "identity": identity,
                        "external_effect_budget": 0,
                    },
                    "user_facing_report": report,
                    "external_effects": [],
                    "external_effect_budget": 0,
                    "read_only_zero_external_effects": True,
                    "policy_receipts": execution_receipts,
                },
            )
        assert kb.get_task(conn, rejected_id).status == "running"
        mismatch_id = kb.create_task(
            conn,
            title="execution with mismatched canonical Page text",
            body=contract_body("execution"),
        )
        assert kb.claim_task(conn, mismatch_id)
        with pytest.raises(ValueError, match="must exactly match"):
            kb.complete_task(
                conn,
                mismatch_id,
                metadata={
                    "loop_contract": {
                        "identity": identity,
                        "external_effect_budget": 0,
                    },
                    "user_facing_report": report,
                    "facebook_page_post": {"text": "不同的 Page 內文"},
                    "external_effects": [],
                    "external_effect_budget": 0,
                    "read_only_zero_external_effects": True,
                    "policy_receipts": execution_receipts,
                },
            )
        assert kb.get_task(conn, mismatch_id).status == "running"
        for suffix, invalid_text in (
            ("whitespace", report["sections"]["facebook_page_post"] + "\n"),
            ("non-string", 123),
        ):
            invalid_id = kb.create_task(
                conn,
                title=f"execution with {suffix} canonical Page text",
                body=contract_body("execution"),
            )
            assert kb.claim_task(conn, invalid_id)
            with pytest.raises(ValueError, match="must exactly match"):
                kb.complete_task(
                    conn,
                    invalid_id,
                    metadata={
                        "loop_contract": {
                            "identity": identity,
                            "external_effect_budget": 0,
                        },
                        "user_facing_report": report,
                        "facebook_page_post": {"text": invalid_text},
                        "external_effects": [],
                        "external_effect_budget": 0,
                        "read_only_zero_external_effects": True,
                        "policy_receipts": execution_receipts,
                    },
                )
            assert kb.get_task(conn, invalid_id).status == "running"

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
        # The controller now rejects an unattested execution before review exists.
        with pytest.raises(ValueError, match="Zero-effect execution completion"):
            create_pair(conn, "h", {**report, "complete": True}, zero_effect_attested=False)
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


@pytest.mark.parametrize("fault", ["swapped", "appendix", "localized", "malformed", "nested", "bracket_nested", "bracket_wrapper_nested"])
def test_publication_binding_candidate(fault):
    full = _full_publication_report()
    labels = ["Facebook Page 貼文", "Facebook Group 討論附文",
              "Gemini Notebook Audio Generation Prompt", "Podcast Title", "Podcast Description"]
    if fault == "localized":
        labels[2:] = ["Gemini Notebook 音訊生成提示", "Podcast 單集標題", "Podcast 單集描述"]
    fields = list(full["sections"])
    if fault == "nested":
        full["sections"][fields[0]] += "\n\n### Details\nNested text must remain"
    if fault == "bracket_nested":
        full["sections"][fields[0]] += "\n\n【Details】\nPreserve bracket callout"
    if fault == "bracket_wrapper_nested":
        full["sections"][fields[0]] += "\n\n## Details\nPreserve Markdown within brackets"
    body = "\n\n".join(f"## {i+1}｜{label}\n\n{full['sections'][field]}"
                         for i, (label, field) in enumerate(zip(labels, fields)))
    if fault == "bracket_wrapper_nested":
        body = "\n\n".join(f"【{label}】\n\n{full['sections'][field]}"
                               for label, field in zip(labels, fields))
    if fault == "appendix":
        body += "\n\n## 6｜Accepted Source 原文\n\nAppendix outside description"
    generic = {key: value for key, value in full.items()
               if key not in {"package_kind", "sections", "policy_receipts", "external_effects"}}
    generic["body"] = body
    evidence = {"asset_manifest": [dict(asset) for asset in full["assets"]]}
    if fault == "malformed":
        chunks = [f"## {i+1}｜{label}\n\n{full['sections'][field]}"
                  for i, (label, field) in enumerate(zip(labels, fields))]
        chunks[0], chunks[1] = chunks[1], chunks[0]
        generic["body"] = "\n\n".join(chunks)
        evidence["sections"] = dict(zip(fields, chunks))
    if fault == "swapped":
        evidence["sections"] = dict(full["sections"])
        evidence["sections"][fields[0]], evidence["sections"][fields[1]] = (
            evidence["sections"][fields[1]], evidence["sections"][fields[0]])
    result = promote_full_publication_package(
        generic, evidence=evidence, expected_asset_filenames=["page.png", "audio.png"],
        policy_receipts=full["policy_receipts"], external_effects=[])
    if fault in {"swapped", "malformed"}:
        assert "package_kind" not in result
    else:
        assert result["sections"] == full["sections"]
