import pytest

from proactive import grace_task_compiler as compiler


def test_id_bound_crosspost_guidance_keeps_common_join_group_safeguards():
    guidance = compiler._render_facebook_crosspost_guidance({
        "facebook_crosspost": {
            "marketplace_listing_id": "1666446304587399",
            "group_ids": ["123456"],
        },
    })

    combined = " ".join(guidance)
    assert "already-selected row must stop" in combined
    assert "do not join under this cross-post approval" in combined
    assert "metadata.approval_needed" in combined


def test_allowlisted_snapshot_rejects_additional_allowed_scope(monkeypatch):
    contract = {
        "identity": {"project": "test", "topic_name": "test"},
        "scope": {
            "allowed": ["https://example.com/", "Inspect another page"],
        },
        "external_targets": ["https://example.com/"],
    }
    monkeypatch.setattr(
        compiler,
        "validate_loop_contract",
        lambda value: value,
    )
    monkeypatch.setattr(
        compiler,
        "assert_contract_matches_context",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="complete allowed scope"):
        compiler.compile_and_delegate(
            contract,
            context={},
            task_type="browser_readonly",
            risk_level="low",
            approved=False,
            delegation_id="delegation",
            delegation_build_owner="owner",
            platform="telegram",
            chat_id="chat",
            thread_id="thread",
        )
