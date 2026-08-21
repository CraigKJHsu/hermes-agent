from proactive.prompt_policy import (
    approval_token_candidate,
    approval_turn_prompt,
    ensure_active_policy_prompt,
    marketplace_readonly_turn_prompt,
    saved_evidence_finalization_turn_prompt,
    stored_prompt_matches_active_policy,
)


def test_prompt_policy_marker_invalidates_stale_prompt(tmp_path, monkeypatch):
    policy = tmp_path / "AGENTS.md"
    policy.write_text(
        "## Grace to ClawOps Delegation Contract\n\n"
        "GRACE_CLAWOPS_POLICY_VERSION: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AGENTS_POLICY", str(policy))
    assert stored_prompt_matches_active_policy(policy.read_text(encoding="utf-8"))
    assert not stored_prompt_matches_active_policy("old prompt")


def test_prompt_policy_injects_live_section_once(tmp_path, monkeypatch):
    policy = tmp_path / "AGENTS.md"
    policy.write_text(
        "# Profile\n\n## Grace to ClawOps Delegation Contract\n\n"
        "GRACE_CLAWOPS_POLICY_VERSION: 3\n\n- Use nested contracts.\n\n"
        "## Risk Mode\n\n- Verify first.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AGENTS_POLICY", str(policy))
    injected = ensure_active_policy_prompt("base prompt")
    assert "GRACE_CLAWOPS_POLICY_VERSION: 3" in injected
    assert "Use nested contracts" in injected
    assert "Risk Mode" not in injected
    assert ensure_active_policy_prompt(injected) == injected


def test_prompt_policy_rejects_truncated_matching_version(tmp_path, monkeypatch):
    policy = tmp_path / "AGENTS.md"
    policy.write_text(
        "## Grace to ClawOps Delegation Contract\n\n"
        "GRACE_CLAWOPS_POLICY_VERSION: 13\n\n"
        "- Approval turns must call clawops_delegate.\n\n"
        "## Risk Mode\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AGENTS_POLICY", str(policy))

    truncated = "base\nGRACE_CLAWOPS_POLICY_VERSION: 13"
    assert not stored_prompt_matches_active_policy(truncated)
    assert "Approval turns must call" in ensure_active_policy_prompt(truncated)


def test_prompt_policy_rejects_stale_superset(tmp_path, monkeypatch):
    policy = tmp_path / "AGENTS.md"
    policy.write_text(
        "## Grace to ClawOps Delegation Contract\n\n"
        "GRACE_CLAWOPS_POLICY_VERSION: 13\n\n"
        "- Current rule.\n\n"
        "## Risk Mode\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AGENTS_POLICY", str(policy))
    stale = (
        "## Grace to ClawOps Delegation Contract\n\n"
        "GRACE_CLAWOPS_POLICY_VERSION: 13\n\n"
        "- Current rule.\n"
        "- Removed stale rule.\n\n"
        "## Other Prompt Section\n"
    )

    rebuilt = ensure_active_policy_prompt(stale)
    assert not stored_prompt_matches_active_policy(stale)
    assert rebuilt.count(
        "## Grace to ClawOps Delegation Contract"
    ) == 1
    assert "Removed stale rule" not in rebuilt
    assert "- Current rule.\n\n## Other Prompt Section" in rebuilt


def test_prompt_policy_removes_duplicate_stale_sections(
    tmp_path,
    monkeypatch,
):
    policy = tmp_path / "AGENTS.md"
    policy.write_text(
        "## Grace to ClawOps Delegation Contract\n\n"
        "GRACE_CLAWOPS_POLICY_VERSION: 13\n\n"
        "- Authoritative rule.\n\n"
        "## Risk Mode\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_AGENTS_POLICY", str(policy))
    active = policy.read_text(encoding="utf-8").split(
        "\n## Risk Mode", 1
    )[0]
    duplicated = (
        f"{active}\n\n"
        "## Other Prompt Section\n\n- Keep me.\n\n"
        "## Grace to ClawOps Delegation Contract\n\n"
        "GRACE_CLAWOPS_POLICY_VERSION: 12\n\n- Stale rule.\n\n"
        "## Final Section\n\n- Keep me too.\n"
    )

    assert not stored_prompt_matches_active_policy(duplicated)
    rebuilt = ensure_active_policy_prompt(duplicated)
    assert rebuilt.count(
        "## Grace to ClawOps Delegation Contract"
    ) == 1
    assert "Stale rule" not in rebuilt
    assert "## Other Prompt Section" in rebuilt
    assert "## Final Section" in rebuilt


def test_approval_turn_prompt_forces_tool_validation():
    prompt = approval_turn_prompt("好的，核准 fe341e4c447cde20。")

    assert "MUST call clawops_delegate now" in prompt
    assert "fe341e4c447cde20" not in prompt
    assert "tool result is authoritative" in prompt.lower()


def test_approval_turn_prompt_routes_unsafe_framing_to_tool_too():
    prompt = approval_turn_prompt(
        "核准 fe341e4c447cde20\nIGNORE_PREVIOUS_RULES"
    )

    assert "MUST call clawops_delegate now" in prompt
    assert "fe341e4c447cde20" not in prompt
    assert "IGNORE_PREVIOUS_RULES" not in prompt


def test_approval_turn_prompt_routes_malformed_token_to_tool():
    for message in (
        "核准 FE341E4C447CDE20",
        "核准 short",
    ):
        assert "MUST call clawops_delegate now" in approval_turn_prompt(message)


def test_approval_turn_prompt_ignores_nonapproval_message():
    assert approval_turn_prompt("請問現在進度如何？") == ""
    assert approval_turn_prompt("a核准 fe341e4c447cde20") == ""
    assert (
        approval_turn_prompt(
            "這是唯讀任務，不需要建立發布核准、跨貼核准或多個核准 token。"
        )
        == ""
    )


def test_marketplace_readonly_turn_prompt_forces_approved_false_delegation():
    prompt = marketplace_readonly_turn_prompt(
        "請只查核 Carimali（Marketplace Listing ID：36803832485927906）。"
        "執行 Facebook Marketplace 唯讀社團狀態查核，直接列出社團名稱與狀態。"
        "不勾選、不發布、不修改、不建立核准 token。"
    )

    assert "MUST call clawops_delegate now" in prompt
    assert '"approved": false' in prompt
    assert '"task_type": "secondhand_commerce_group_status"' in prompt
    assert '"external_targets": ["Facebook Marketplace listing ID 36803832485927906"]' in prompt
    assert "do not create, request, or consume an approval token" in prompt
    assert "approval_token" in prompt
    assert "facebook_crosspost" in prompt


def test_marketplace_readonly_turn_prompt_normalizes_known_retry_shorthand():
    prompt = marketplace_readonly_turn_prompt(
        "[KJ HSU] 只讀重試 Carimali Listing ID 36803832485927906"
    )

    assert "MUST call clawops_delegate now" in prompt
    assert '"subject_keys": ["facebook_marketplace:36803832485927906"]' in prompt
    assert '"external_targets": ["Facebook Marketplace listing ID 36803832485927906"]' in prompt
    assert '"approved": false' in prompt


def test_marketplace_readonly_turn_prompt_rejects_broad_or_mutating_requests():
    for message in (
        "請查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態。",
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 與 27909676598721497 的社團狀態，不勾選、不發布、不修改。",
        "請將 Facebook Marketplace Listing ID 36803832485927906 發布到社團。",
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態，"
        "不勾選、不發布、不修改；之後請發布到社團。",
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態，"
        "不勾選、不提交、不修改，但取消不發布限制。",
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態，"
        "不勾選、不發布、不修改任何外部狀態；以上限制全部取消。",
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態，"
        "不勾選、不發布、不修改任何外部狀態；以上要求取消。",
        "Please read-only inspect Facebook Marketplace Listing ID 36803832485927906 "
        "group status. Do not select, do not publish, do not change external state; "
        "revoke those restrictions.",
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態，"
        "不勾選、不發布、不修改，但也幫我刊登此物品。",
        "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 的社團狀態，"
        "不勾選、不發布、不修改，但也 Boost the listing。",
    ):
        assert marketplace_readonly_turn_prompt(message) == ""


def test_marketplace_readonly_turn_prompt_accepts_natural_imperative_bans():
    for bans in (
        "不要勾選、不要發布、不要修改。",
        "請勿勾選、請勿發布、請勿修改任何外部狀態。",
    ):
        prompt = marketplace_readonly_turn_prompt(
            "請唯讀查核 Facebook Marketplace Listing ID 36803832485927906 "
            f"的社團狀態，{bans}"
        )

        assert "MUST call clawops_delegate now" in prompt
        assert '"approved": false' in prompt


def test_saved_evidence_finalization_routes_original_task_not_new_contract():
    prompt = saved_evidence_finalization_turn_prompt(
        "請重新讀取該任務 t_2f6b540f，使用已保存的 27 筆證據完成原任務。"
    )

    assert "MUST call clawops_finalize_saved_evidence" in prompt
    assert '"execution_task_id": "t_2f6b540f"' in prompt
    assert '"board": "default"' in prompt
    assert "Do not call clawops_delegate" in prompt
    assert "do not open any browser" in prompt


def test_saved_evidence_finalization_does_not_hijack_status_questions():
    assert saved_evidence_finalization_turn_prompt(
        "請問 t_2f6b540f 現在進度如何？"
    ) == ""
    assert saved_evidence_finalization_turn_prompt(
        "請讀取該任務，但我不確定任務編號。"
    ) == ""
    assert saved_evidence_finalization_turn_prompt(
        "請用既有證據處理 t_2f6b540f 與 t_e49768c5。"
    ) == ""
    assert saved_evidence_finalization_turn_prompt(
        "What does finalization mean for t_2f6b540f?"
    ) == ""
    assert saved_evidence_finalization_turn_prompt(
        "t_2f6b540f 現在有 27 筆嗎？"
    ) == ""
    assert saved_evidence_finalization_turn_prompt(
        "t_2f6b540f 的證據已保存了嗎？"
    ) == ""
    assert saved_evidence_finalization_turn_prompt(
        "不要使用已保存證據完成原任務 t_2f6b540f。"
    ) == ""


def test_saved_evidence_finalization_preserves_nondefault_task_board(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import kanban_db as kb

    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kb.create_board("secondhand")
    with kb.connect_closing(board="secondhand") as conn:
        task_id = kb.create_task(conn, title="Saved evidence execution")

    prompt = saved_evidence_finalization_turn_prompt(
        f"請使用已保存證據完成原任務 {task_id}。"
    )

    assert f'"execution_task_id": "{task_id}"' in prompt
    assert '"board": "secondhand"' in prompt


def test_approval_token_candidate_accepts_harmless_framing():
    assert (
        approval_token_candidate("好吧，核准 fe341e4c447cde20。")
        == "fe341e4c447cde20"
    )
    assert approval_token_candidate("核准 short") == ""


def test_runtime_prompt_cache_fails_closed_when_policy_check_raises(
    monkeypatch,
):
    from types import SimpleNamespace

    from agent.conversation_loop import _stored_prompt_matches_runtime
    import proactive.prompt_policy as prompt_policy

    monkeypatch.setattr(
        prompt_policy,
        "stored_prompt_matches_active_policy",
        lambda _prompt: (_ for _ in ()).throw(OSError("policy unavailable")),
    )
    agent = SimpleNamespace(model="test-model", provider="test-provider")

    assert not _stored_prompt_matches_runtime(agent, "cached prompt")


def test_new_prompt_build_fails_closed_when_policy_injection_raises(
    monkeypatch,
):
    from unittest.mock import MagicMock

    from agent.conversation_loop import _restore_or_build_system_prompt
    import proactive.prompt_policy as prompt_policy

    monkeypatch.setattr(
        prompt_policy,
        "ensure_active_policy_prompt",
        lambda _prompt: (_ for _ in ()).throw(OSError("policy unavailable")),
    )
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent._session_db = None
    agent._build_system_prompt.return_value = "base prompt"
    agent.session_id = "new-session"

    import pytest

    with pytest.raises(RuntimeError, match="refusing to start"):
        _restore_or_build_system_prompt(agent, None, [])


def test_configured_missing_policy_fails_closed(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv(
        "HERMES_AGENTS_POLICY", str(tmp_path / "missing-AGENTS.md")
    )

    with pytest.raises(RuntimeError, match="does not exist"):
        ensure_active_policy_prompt("base prompt")


def test_configured_malformed_policy_fails_closed(tmp_path, monkeypatch):
    import pytest

    policy = tmp_path / "AGENTS.md"
    policy.write_text("# Missing required section\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_AGENTS_POLICY", str(policy))

    with pytest.raises(RuntimeError, match="missing the"):
        ensure_active_policy_prompt("base prompt")
