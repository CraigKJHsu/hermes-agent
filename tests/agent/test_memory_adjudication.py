import stat

from agent.memory_adjudication import MemoryAdjudicator, MemoryDecision


def test_mixed_bullet_block_adjudicates_substantive_non_bullet_lines(
    tmp_path,
    monkeypatch,
):
    adjudicator = MemoryAdjudicator(
        {"enabled": True, "mode": "enforce"},
        hermes_home=tmp_path,
    )
    captured = []

    def fake_adjudicate(entries, **_kwargs):
        captured.extend(entries)
        return [entry for entry in entries if "production model" not in entry]

    monkeypatch.setattr(adjudicator, "adjudicate_entries", fake_adjudicate)

    result = adjudicator.adjudicate_block(
        "Memory:\nCurrent production model is X\n- preferred style",
        source="test",
    )

    assert captured == ["Current production model is X", "preferred style"]
    assert result == "Memory:\n- preferred style"


def test_cache_prunes_expired_entries_and_enforces_size_limit(tmp_path):
    adjudicator = MemoryAdjudicator(
        {
            "enabled": True,
            "cache_ttl_seconds": 0.001,
            "max_cache_entries": 2,
        },
        hermes_home=tmp_path,
    )
    decision = MemoryDecision(
        decision="ACCEPT",
        effective_text="value",
        reason="test",
        evidence=[],
        confidence=1.0,
        risk_level="low",
    )
    adjudicator._cache_put("one", decision)
    adjudicator._cache_put("two", decision)
    adjudicator._cache_put("three", decision)

    assert len(adjudicator._cache) == 2
    assert "one" not in adjudicator._cache


def test_existing_custom_audit_parent_permissions_are_preserved(tmp_path):
    audit_parent = tmp_path / "shared-audit"
    audit_parent.mkdir(mode=0o755)
    audit_db = audit_parent / "adjudications.db"
    adjudicator = MemoryAdjudicator(
        {"enabled": True, "audit_db": str(audit_db)},
        hermes_home=tmp_path,
    )
    decision = MemoryDecision(
        decision="REJECT",
        effective_text="",
        reason="test",
        evidence=[],
        confidence=1.0,
        risk_level="high",
    )

    adjudicator._record_decision(
        source="test",
        original_text="volatile value",
        query_hash="query",
        environment_hash="environment",
        session_id="session",
        decision=decision,
    )

    assert stat.S_IMODE(audit_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(audit_db.stat().st_mode) == 0o600
