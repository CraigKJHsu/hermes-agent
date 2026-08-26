import sqlite3

from agent.memory_adjudication import MemoryAdjudicator


def _snapshot():
    return {
        "observed_at": "2026-08-09T00:00:00+00:00",
        "mem0": {
            "health": "healthy",
            "llm_provider": "openai",
            "llm_model": "gpt-5.6-luna",
            "embedder_model": "text-embedding-3-large",
            "collection": "memories_openai_v1",
        },
        "qmd": {"status": "node_abi_mismatch"},
    }


def _config(tmp_path, **overrides):
    config = {
        "enabled": True,
        "mode": "enforce",
        "runtime_sensitive_only": True,
        "audit_db": str(tmp_path / "adjudications.db"),
        "enqueue_revisions": True,
    }
    config.update(overrides)
    return config


def test_static_memory_uses_deterministic_fast_path(tmp_path):
    called = False

    def transport(_payload):
        nonlocal called
        called = True
        raise AssertionError("static memory must not call the model")

    gate = MemoryAdjudicator(
        _config(tmp_path),
        hermes_home=tmp_path,
        llm_transport=transport,
        environment_probe=_snapshot,
    )
    source = "KJ 要求所有回覆使用繁體中文。"

    assert gate.adjudicate_entries([source], source="prompt_memory") == [source]
    assert called is False


def test_dynamic_memory_can_be_reconstructed_without_source_mutation(tmp_path):
    source = "Mem0 目前使用 Gemini，集合為 memories。"

    def transport(_payload):
        return {
            "results": [
                {
                    "index": 0,
                    "decision": "RECONSTRUCT",
                    "effective_text": (
                        "Mem0 目前使用 OpenAI gpt-5.6-luna；"
                        "集合為 memories_openai_v1。Gemini 是歷史設定。"
                    ),
                    "reason": "Runtime snapshot supersedes the old provider state.",
                    "evidence": ["runtime_snapshot.mem0"],
                    "confidence": 0.99,
                }
            ]
        }

    gate = MemoryAdjudicator(
        _config(tmp_path),
        hermes_home=tmp_path,
        llm_transport=transport,
        environment_probe=_snapshot,
    )
    effective = gate.adjudicate_entries([source], source="provider:mem0")

    assert effective == [
        "Mem0 目前使用 OpenAI gpt-5.6-luna；集合為 memories_openai_v1。Gemini 是歷史設定。"
    ]
    assert source == "Mem0 目前使用 Gemini，集合為 memories。"

    with sqlite3.connect(tmp_path / "adjudications.db") as db:
        row = db.execute(
            "SELECT decision, original_text, effective_text FROM memory_adjudications"
        ).fetchone()
        pending = db.execute(
            "SELECT state FROM memory_revision_outbox"
        ).fetchone()
    assert row[0] == "RECONSTRUCT"
    assert row[1] == source
    assert "memories_openai_v1" in row[2]
    assert pending == ("pending",)


def test_reject_and_defer_are_not_injected_in_enforce_mode(tmp_path):
    def transport(_payload):
        return {
            "results": [
                {
                    "index": 0,
                    "decision": "REJECT",
                    "effective_text": "",
                    "reason": "Conflicts with current state.",
                    "evidence": ["runtime_snapshot"],
                    "confidence": 1.0,
                },
                {
                    "index": 1,
                    "decision": "DEFER",
                    "effective_text": "",
                    "reason": "Required live evidence is absent.",
                    "evidence": [],
                    "confidence": 0.8,
                },
            ]
        }

    gate = MemoryAdjudicator(
        _config(tmp_path),
        hermes_home=tmp_path,
        llm_transport=transport,
        environment_probe=_snapshot,
    )
    entries = [
        "目前正式環境已完成部署。",
        "目前法律審查已通過，可以發布。",
    ]
    assert gate.adjudicate_entries(entries, source="provider:mem0") == []


def test_shadow_mode_audits_but_preserves_original(tmp_path):
    def transport(_payload):
        return {
            "results": [
                {
                    "index": 0,
                    "decision": "REJECT",
                    "effective_text": "",
                    "reason": "Stale.",
                    "evidence": ["runtime_snapshot"],
                    "confidence": 1.0,
                }
            ]
        }

    gate = MemoryAdjudicator(
        _config(tmp_path, mode="shadow"),
        hermes_home=tmp_path,
        llm_transport=transport,
        environment_probe=_snapshot,
    )
    source = "目前服務仍然離線。"
    assert gate.adjudicate_entries([source], source="prompt_memory") == [source]


def test_model_failure_defers_dynamic_memory_fail_closed(tmp_path):
    def transport(_payload):
        raise RuntimeError("provider unavailable")

    gate = MemoryAdjudicator(
        _config(tmp_path),
        hermes_home=tmp_path,
        llm_transport=transport,
        environment_probe=_snapshot,
    )
    source = "目前正式環境可以直接部署並刪除舊資料。"
    assert gate.adjudicate_entries([source], source="provider:mem0") == []


def test_bullet_block_preserves_header_and_only_effective_entries(tmp_path):
    def transport(_payload):
        return {
            "results": [
                {
                    "index": 0,
                    "decision": "RECONSTRUCT",
                    "effective_text": "Mem0 目前使用 OpenAI。",
                    "reason": "Updated provider.",
                    "evidence": ["runtime_snapshot.mem0"],
                    "confidence": 1.0,
                }
            ]
        }

    gate = MemoryAdjudicator(
        _config(tmp_path),
        hermes_home=tmp_path,
        llm_transport=transport,
        environment_probe=_snapshot,
    )
    block = "## Mem0 Memory\n- Mem0 目前使用 Gemini。\n- KJ 偏好繁體中文。"
    effective = gate.adjudicate_block(block, source="provider:mem0")

    assert effective == "## Mem0 Memory\n- Mem0 目前使用 OpenAI。\n- KJ 偏好繁體中文。"

