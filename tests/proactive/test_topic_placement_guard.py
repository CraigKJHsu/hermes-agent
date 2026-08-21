from __future__ import annotations

import yaml
from types import SimpleNamespace

from proactive.topic_placement_guard import (
    TopicMismatch,
    _clear_guard_state_for_tests,
    detect_topic_mismatch,
    evaluate_inbound_topic_placement,
    register_pending_topic_override,
    topic_override_confirmed,
)


def _registry(tmp_path, monkeypatch):
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "contexts": [
                    {
                        "platform": "telegram",
                        "chat_id": "chat-1",
                        "thread_id": "2120",
                        "topic_name": "KJ Profile",
                        "project": "kj_profile",
                        "aliases": ["LinkedIn 個人資料維護"],
                        "topic_hints": [
                            "LinkedIn",
                            "work history",
                            "履歷",
                            "工作經歷",
                        ],
                    },
                    {
                        "platform": "telegram",
                        "chat_id": "chat-1",
                        "thread_id": "2680",
                        "topic_name": "失智患者的照護",
                        "project": "dementia_care",
                        "aliases": ["失智患者的溝通策略"],
                        "topic_hints": [
                            "失智",
                            "媽媽照護",
                            "照護事件",
                            "事件紀錄",
                            "回診",
                        ],
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(path))
    _clear_guard_state_for_tests()


def test_care_request_in_profile_topic_suggests_dementia_topic(
    tmp_path,
    monkeypatch,
):
    _registry(tmp_path, monkeypatch)

    mismatch = detect_topic_mismatch(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        text="請把媽媽照護事件整理成事件紀錄，回診時提供給我。",
    )

    assert mismatch is not None
    assert mismatch.current_topic_name == "KJ Profile"
    assert mismatch.suggested_thread_id == "2680"
    assert mismatch.suggested_topic_name == "失智患者的照護"


def test_one_weak_cross_topic_word_does_not_warn(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)

    assert (
        detect_topic_mismatch(
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            text="MissionCrew 的照護產業定位要放進 LinkedIn 個人介紹。",
        )
        is None
    )


def test_ascii_hint_preserves_word_boundaries(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)

    mismatch = detect_topic_mismatch(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2680",
        text="Please update my LinkedIn profile and work history.",
    )

    assert mismatch is not None
    assert mismatch.suggested_thread_id == "2120"


def test_named_current_topic_wins_over_sibling_hints(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)

    assert (
        detect_topic_mismatch(
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            text="這次刻意留在 LinkedIn 個人資料維護，補充失智與回診經驗。",
        )
        is None
    )


def test_warning_pauses_then_exact_override_replays_original_message(
    tmp_path,
    monkeypatch,
):
    _registry(tmp_path, monkeypatch)
    original = "請整理媽媽照護事件紀錄，回診時給我。"
    original_payload = SimpleNamespace(text=original)

    warning = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-1",
        text=original,
        original_payload=original_payload,
    )
    resumed = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-2",
        text="留在這裡",
    )

    assert warning.action == "warn"
    assert "我先不建立任務" in warning.message
    assert resumed.action == "allow"
    assert resumed.replacement_text == original
    assert resumed.replacement_payload is original_payload
    assert (
        topic_override_confirmed(
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="user-1",
            message_id="msg-2",
        )
        is True
    )


def test_cancel_discards_pending_message(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-1",
        text="請整理媽媽照護事件紀錄，回診時給我。",
    )

    cancelled = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-2",
        text="取消",
    )

    assert cancelled.action == "cancel"
    assert "沒有建立任務" in cancelled.message


def test_other_sender_cannot_consume_pending_override(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    original = "請整理媽媽照護事件紀錄，回診時給我。"
    evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-1",
        text=original,
    )

    other = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-2",
        message_id="msg-2",
        text="留在這裡",
    )
    owner = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-3",
        text="留在這裡",
    )

    assert other.action == "allow"
    assert other.replacement_text == ""
    assert owner.replacement_text == original


def test_unrelated_message_does_not_discard_pending_request(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    original = "請整理媽媽照護事件紀錄，回診時給我。"
    evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-1",
        text=original,
    )

    held = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-2",
        text="還有另一件事",
    )
    resumed = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-3",
        text="留在這裡",
    )

    assert held.action == "warn"
    assert "仍在等待" in held.message
    assert resumed.replacement_text == original


def test_delegate_only_warning_registers_resumable_pending(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    mismatch = TopicMismatch(
        current_thread_id="2120",
        current_topic_name="KJ Profile",
        suggested_thread_id="2680",
        suggested_topic_name="失智患者的照護",
        matched_hints=("媽媽照護", "回診"),
    )
    original = "請整理這件事。"
    original_payload = SimpleNamespace(text=original)

    allowed = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-1",
        text=original,
        original_payload=original_payload,
    )
    assert allowed.action == "allow"

    assert register_pending_topic_override(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-1",
        original_text="",
        mismatch=mismatch,
    )
    resumed = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-2",
        text="留在這裡",
    )

    assert resumed.replacement_text == original
    assert resumed.replacement_payload is original_payload


def test_senderless_warning_does_not_offer_unavailable_override(
    tmp_path,
    monkeypatch,
):
    _registry(tmp_path, monkeypatch)

    warning = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="",
        message_id="msg-1",
        text="請整理媽媽照護事件紀錄，回診時給我。",
    )

    assert warning.action == "warn"
    assert "留在這裡" not in warning.message


def test_second_delegate_registration_cannot_replace_existing_pending(
    tmp_path,
    monkeypatch,
):
    _registry(tmp_path, monkeypatch)
    mismatch = TopicMismatch(
        current_thread_id="2120",
        current_topic_name="KJ Profile",
        suggested_thread_id="2680",
        suggested_topic_name="失智患者的照護",
        matched_hints=("媽媽照護", "回診"),
    )
    first_payload = SimpleNamespace(text="第一件事")
    second_payload = SimpleNamespace(text="第二件事")
    for message_id, payload in (("msg-1", first_payload), ("msg-2", second_payload)):
        allowed = evaluate_inbound_topic_placement(
            platform="telegram",
            chat_id="chat-1",
            thread_id="2120",
            user_id="user-1",
            message_id=message_id,
            text=payload.text,
            original_payload=payload,
        )
        assert allowed.action == "allow"

    assert register_pending_topic_override(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-1",
        original_text="第一件事",
        mismatch=mismatch,
    )
    assert not register_pending_topic_override(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-2",
        original_text="第二件事",
        mismatch=mismatch,
    )
    resumed = evaluate_inbound_topic_placement(
        platform="telegram",
        chat_id="chat-1",
        thread_id="2120",
        user_id="user-1",
        message_id="msg-3",
        text="留在這裡",
    )

    assert resumed.replacement_payload is first_payload
