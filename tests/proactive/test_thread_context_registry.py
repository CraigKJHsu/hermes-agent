from __future__ import annotations

import yaml
import pytest

from proactive.thread_context_registry import (
    ThreadContextError,
    ensure_thread_context,
    is_generated_topic_placeholder,
    resolve_thread_context,
    seed_thread_context_topic_name,
    update_thread_context_topic_name,
)


def _use_registry(tmp_path, monkeypatch, content="version: 1\ncontexts: []\n"):
    registry = tmp_path / "thread_context_registry.yaml"
    registry.write_text(content, encoding="utf-8")
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    return registry


def test_new_telegram_topic_materializes_one_lightweight_main_project(
    tmp_path,
    monkeypatch,
):
    registry = _use_registry(tmp_path, monkeypatch)

    first = ensure_thread_context(
        platform="telegram",
        chat_id="-100123",
        thread_id="2680",
    )
    second = ensure_thread_context(
        platform="telegram",
        chat_id="-100123",
        thread_id="2680",
    )

    assert first == second
    assert first["project"].startswith("telegram_100123_2680_")
    assert first["project_kind"] == "lightweight_main"
    assert first["subprojects"] == []
    assert first["memory_namespace"].endswith(f"/{first['project']}")
    persisted = yaml.safe_load(registry.read_text(encoding="utf-8"))
    assert len(persisted["contexts"]) == 1


def test_resolve_can_auto_create_unknown_telegram_topic(tmp_path, monkeypatch):
    _use_registry(tmp_path, monkeypatch)

    resolved = resolve_thread_context(
        platform="telegram",
        chat_id="chat-1",
        thread_id="999",
        auto_create=True,
        work_hint="整理後續工作",
    )

    assert resolved["project"].startswith("telegram_chat_1_999_")
    assert resolved["auto_created"] is True


def test_generated_projects_preserve_the_full_chat_lane_identity(tmp_path, monkeypatch):
    _use_registry(tmp_path, monkeypatch)

    positive = ensure_thread_context(
        platform="telegram", chat_id="123", thread_id="7"
    )
    negative = ensure_thread_context(
        platform="telegram", chat_id="-123", thread_id="7"
    )

    assert positive["project"] != negative["project"]
    assert positive["memory_namespace"] != negative["memory_namespace"]


def test_multiple_active_subprojects_require_a_decision_only_when_unmatched(
    tmp_path,
    monkeypatch,
):
    _use_registry(
        tmp_path,
        monkeypatch,
        content=(
            "version: 1\ncontexts:\n"
            "  - platform: telegram\n"
            "    chat_id: chat-1\n"
            "    thread_id: '42'\n"
            "    topic_name: 失智患者的照護\n"
            "    project: dementia_care\n"
            "    memory_namespace: topic:42/dementia_care\n"
            "    subprojects:\n"
            "      - project: family_handbook\n"
            "        name: 家屬手冊\n"
            "        aliases: [家屬手冊, handbook]\n"
            "      - project: caregiver_training\n"
            "        name: 照護者訓練\n"
            "        aliases: [照護者訓練, training]\n"
        ),
    )

    selected = resolve_thread_context(
        platform="telegram",
        chat_id="chat-1",
        thread_id="42",
        work_hint="請繼續整理家屬手冊的第三章",
    )
    assert selected["project"] == "family_handbook"
    assert selected["selected_subproject"] == "family_handbook"

    with pytest.raises(ThreadContextError, match="ambiguous project placement"):
        resolve_thread_context(
            platform="telegram",
            chat_id="chat-1",
            thread_id="42",
            work_hint="請整理新的訪談資料",
        )


def test_short_ascii_alias_does_not_match_inside_an_unrelated_word(
    tmp_path,
    monkeypatch,
):
    _use_registry(
        tmp_path,
        monkeypatch,
        content=(
            "version: 1\ncontexts:\n"
            "  - platform: telegram\n"
            "    chat_id: chat-1\n"
            "    thread_id: '42'\n"
            "    project: main\n"
            "    subprojects:\n"
            "      - {project: app, name: App, aliases: [app]}\n"
            "      - {project: docs, name: Docs, aliases: [docs, red fox]}\n"
        ),
    )

    with pytest.raises(ThreadContextError, match="ambiguous project placement"):
        resolve_thread_context(
            platform="telegram",
            chat_id="chat-1",
            thread_id="42",
            work_hint="approval routing",
        )

    with pytest.raises(ThreadContextError, match="ambiguous project placement"):
        resolve_thread_context(
            platform="telegram",
            chat_id="chat-1",
            thread_id="42",
            work_hint="app-v2 deployment",
        )

    with pytest.raises(ThreadContextError, match="ambiguous project placement"):
        resolve_thread_context(
            platform="telegram",
            chat_id="chat-1",
            thread_id="42",
            work_hint="red foxtrot report",
        )


def test_topic_rename_updates_main_project_label_without_changing_project_id(
    tmp_path,
    monkeypatch,
):
    _use_registry(tmp_path, monkeypatch)
    original = ensure_thread_context(
        platform="telegram",
        chat_id="chat-1",
        thread_id="7",
    )

    updated = update_thread_context_topic_name(
        platform="telegram",
        chat_id="chat-1",
        thread_id="7",
        topic_name="失智患者的照護",
    )

    assert updated["project"] == original["project"]
    assert updated["topic_name"] == "失智患者的照護"
    assert updated["project_name"] == "失智患者的照護"
    assert "失智患者的照護" in updated["aliases"]


def test_source_topic_name_only_seeds_a_placeholder_and_cannot_undo_rename(
    tmp_path,
    monkeypatch,
):
    _use_registry(tmp_path, monkeypatch)

    seeded = seed_thread_context_topic_name(
        platform="telegram",
        chat_id="chat-1",
        thread_id="7",
        topic_name="舊設定名稱",
    )
    assert seeded["topic_name"] == "舊設定名稱"

    update_thread_context_topic_name(
        platform="telegram",
        chat_id="chat-1",
        thread_id="7",
        topic_name="新名稱",
    )
    preserved = seed_thread_context_topic_name(
        platform="telegram",
        chat_id="chat-1",
        thread_id="7",
        topic_name="舊設定名稱",
    )

    assert preserved["topic_name"] == "新名稱"


def test_topic_name_seeding_preserves_internal_whitespace(tmp_path, monkeypatch):
    _use_registry(tmp_path, monkeypatch)

    seeded = seed_thread_context_topic_name(
        platform="telegram",
        chat_id="chat-1",
        thread_id="7",
        topic_name="Release  Plan",
    )

    assert seeded["topic_name"] == "Release  Plan"


def test_authoritative_topic_name_seed_is_a_byte_preserving_noop(
    tmp_path,
    monkeypatch,
):
    original = (
        "# preserve this operator comment\n"
        "version: 1\n"
        "contexts:\n"
        "  - platform: telegram\n"
        "    chat_id: chat-1\n"
        "    thread_id: '7'\n"
        "    topic_name: Current Name\n"
        "    project: current_project\n"
    )
    registry = _use_registry(tmp_path, monkeypatch, content=original)

    result = seed_thread_context_topic_name(
        platform="telegram",
        chat_id="chat-1",
        thread_id="7",
        topic_name="Stale Source Name",
    )

    assert result["topic_name"] == "Current Name"
    assert registry.read_text(encoding="utf-8") == original


def test_only_the_exact_generated_name_is_a_placeholder():
    assert is_generated_topic_placeholder("Topic 2680", "2680") is True
    assert is_generated_topic_placeholder("Topic Ideas", "2680") is False
    assert is_generated_topic_placeholder("topic 2680", "2680") is False
    assert is_generated_topic_placeholder("Topic   2680", "2680") is False
