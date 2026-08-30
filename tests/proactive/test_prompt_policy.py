from proactive.prompt_policy import (
    approval_attempt_candidate,
    approval_token_candidate,
    approval_turn_prompt,
    evidence_first_answering_prompt,
    ensure_active_policy_prompt,
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
        "a核准 fe341e4c447cde20",
    ):
        assert "MUST call clawops_delegate now" in approval_turn_prompt(message)


def test_approval_turn_prompt_ignores_nonapproval_message():
    assert approval_turn_prompt("請問現在進度如何？") == ""


def test_evidence_first_answering_prompt_prioritizes_authoritative_sources():
    prompt = evidence_first_answering_prompt()

    assert "Trusted evidence-first answering gate" in prompt
    assert "do not answer from Prompt Memory, Mem0, QMD" in prompt
    assert "task_runs, task_events, task_external_effects" in prompt
    assert "user_facing_report" in prompt
    assert "managed policy snapshots" in prompt
    assert "Prompt Memory, USER.md, Mem0, QMD, and session_search" in prompt
    assert "never prove absence by themselves" in prompt
    assert "historical verified" in prompt
    assert "current live verified" in prompt
    assert "verified/not verified" in prompt
    assert "must not convert a missing current read into 'never existed'" in prompt


def test_approval_token_candidate_accepts_harmless_framing():
    assert (
        approval_token_candidate("好吧，核准 fe341e4c447cde20。")
        == "fe341e4c447cde20"
    )
    assert approval_token_candidate("核准 short") == ""


def test_long_work_instruction_discussing_approval_is_not_protocol_turn():
    message = """請建立全新的預發布流程。

只有在我另行明確核准精確 message SHA-256、image SHA-256 與 Page URL 後，
才建立發布任務。來源 SHA-256：
6bfffca4227fe64ddfb966d4f50e5c6beeda95e2e23fd194ebcfe02efa0eda48
"""

    assert approval_attempt_candidate(message) == ""
    assert approval_token_candidate(message) == ""
    assert approval_turn_prompt(message) == ""


def test_standalone_malformed_approval_remains_fail_closed():
    assert approval_attempt_candidate("核准 short") == "short"
    assert "MUST call clawops_delegate now" in approval_turn_prompt("核准 short")


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
