from __future__ import annotations

import pytest

from proactive.domain_memory import (
    DomainMemoryError,
    attach_domain_memory_contract,
    normalize_domain_memory_contract,
    normalize_memory_deltas,
    validate_delta_external_effect_refs,
)


def _contract(*, task_type: str = "content_draft") -> dict:
    return {
        "identity": {
            "project": "SoloBizAi",
            "topic_name": "AI BizWeek",
        },
        "goal": {"objective": "整理目前案例狀態"},
        "routing": {"task_type": task_type},
    }


def _solobiz_delta() -> dict:
    return {
        "operation": "upsert",
        "entity_id": "carters-junk-away-ep04",
        "label": "Carter's Junk Away",
        "status": "published",
        "attributes": {"episode_number": "EP04"},
        "artifacts": [
            {
                "artifact_type": "facebook_page_post",
                "platform": "facebook",
                "status": "published",
                "external_id": "123_456",
                "public_url": "https://www.facebook.com/123/posts/456",
                "verified_at": "2026-08-29T12:00:00+08:00",
                "evidence_ref": "task_external_effect:facebook:create",
            },
            {
                "artifact_type": "podcast_episode",
                "platform": "podcast",
                "artifact_key": "podcast_episode:ep04",
                "status": "not_published",
            },
            {
                "artifact_type": "audio_brief",
                "platform": "internal",
                "artifact_key": "audio_brief:ep04",
                "status": "not_published",
            },
        ],
        "evidence_refs": ["task_external_effect:facebook:create"],
    }


def test_known_solobiz_topic_is_inferred_without_user_reminder():
    normalized = attach_domain_memory_contract(_contract())

    assert normalized["domain_memory"]["schema_id"] == "solobizai.case.v1"
    assert normalized["domain_memory"]["mode"] == "query"
    assert normalized["domain_memory"]["require_delta_on_acceptance"] is False


def test_ai_bizweek_project_key_is_inferred_without_topic_label():
    contract = _contract()
    contract["identity"] = {"project": "ai_bizweek"}

    normalized = attach_domain_memory_contract(contract)

    assert normalized["domain_memory"]["schema_id"] == "solobizai.case.v1"


def test_known_publish_task_requires_memory_delta():
    normalized = attach_domain_memory_contract(
        _contract(task_type="facebook_page_api_publish")
    )

    assert normalized["domain_memory"]["mode"] == "mutate"
    assert normalized["domain_memory"]["require_delta_on_acceptance"] is True
    with pytest.raises(DomainMemoryError, match="requires at least one"):
        normalize_memory_deltas([], normalized["domain_memory"])


def test_query_contract_cannot_smuggle_registry_write():
    spec = attach_domain_memory_contract(_contract())["domain_memory"]

    with pytest.raises(DomainMemoryError, match="query-mode"):
        normalize_memory_deltas([_solobiz_delta()], spec)


def test_mutation_delta_is_canonical_and_cites_external_effect():
    spec = attach_domain_memory_contract(
        _contract(task_type="facebook_page_api_publish")
    )["domain_memory"]
    deltas = normalize_memory_deltas([_solobiz_delta()], spec)

    validate_delta_external_effect_refs(
        deltas,
        [{"platform": "facebook", "effect_key": "create"}],
    )
    assert deltas[0]["artifacts"][0]["artifact_key"].startswith("facebook_page_post:")


def test_mutation_delta_rejects_unlinked_external_artifact():
    spec = attach_domain_memory_contract(
        _contract(task_type="facebook_page_api_publish")
    )["domain_memory"]
    delta = _solobiz_delta()
    delta["evidence_refs"] = ["task_external_effect:facebook:other"]
    delta["artifacts"][0]["evidence_ref"] = "task_external_effect:facebook:other"
    deltas = normalize_memory_deltas([delta], spec)

    with pytest.raises(DomainMemoryError, match="no exact completion"):
        validate_delta_external_effect_refs(
            deltas,
            [{"platform": "facebook", "effect_key": "create"}],
        )


def test_mutation_delta_requires_every_artifact_slot():
    spec = attach_domain_memory_contract(
        _contract(task_type="facebook_page_api_publish")
    )["domain_memory"]
    raw = _solobiz_delta()
    raw["artifacts"] = raw["artifacts"][:1]
    with pytest.raises(
        DomainMemoryError,
        match="missing: audio_brief, podcast_episode",
    ):
        normalize_memory_deltas([raw], spec)


def test_materialized_artifact_requires_exact_evidence_ref():
    spec = attach_domain_memory_contract(
        _contract(task_type="facebook_page_api_publish")
    )["domain_memory"]
    raw = _solobiz_delta()
    raw["artifacts"][0].pop("evidence_ref")
    with pytest.raises(DomainMemoryError, match="evidence_ref is required"):
        normalize_memory_deltas([raw], spec)


def test_mutation_delta_rejects_top_level_artifact_shape():
    spec = attach_domain_memory_contract(
        _contract(task_type="facebook_page_api_publish")
    )["domain_memory"]
    raw = {
        "operation": "upsert",
        "entity_id": "carters-junk-away-ep04",
        "label": "Carter's Junk Away",
        "status": "published",
        "artifact_type": "facebook_page_post",
        "artifact_id": "facebook:page:123_456",
        "platform": "facebook",
        "public_url": "https://www.facebook.com/123/posts/456",
    }

    with pytest.raises(DomainMemoryError, match="artifact state inside artifacts"):
        normalize_memory_deltas([raw], spec)


def test_builtin_schema_cannot_be_redefined_under_same_id():
    with pytest.raises(DomainMemoryError, match="conflicts with built-in"):
        normalize_domain_memory_contract({
            "schema_id": "solobizai.case.v1",
            "mode": "query",
            "entity_type": "WrongType",
        })
