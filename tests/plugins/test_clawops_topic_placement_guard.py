from __future__ import annotations

import json


def test_delegate_rejects_high_confidence_topic_mismatch_before_task_creation(
    tmp_path,
    monkeypatch,
):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "version: 1\ncontexts:\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2120'\n"
        "    topic_name: KJ Profile\n    project: kj_profile\n"
        "    topic_hints: [LinkedIn, 履歷, 工作經歷]\n"
        "  - platform: telegram\n    chat_id: chat-1\n    thread_id: '2680'\n"
        "    topic_name: 失智患者的照護\n    project: dementia_care\n"
        "    topic_hints: [失智, 媽媽照護, 照護事件, 事件紀錄, 回診]\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_THREAD_CONTEXT_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(
        "plugins.openclaw_bridge.clawops_delegate.get_session_env",
        lambda key, default="": {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "chat-1",
            "HERMES_SESSION_THREAD_ID": "2120",
            "HERMES_SESSION_USER_ID": "user-1",
            "HERMES_SESSION_KEY": "agent:main:telegram:group:chat-1:2120",
            "HERMES_SESSION_ID": "grace-session-wrong-topic",
            "HERMES_SESSION_MESSAGE_ID": "msg-wrong-topic",
            "HERMES_SESSION_MESSAGE_TEXT": "請整理媽媽照護事件紀錄",
            "HERMES_SESSION_INTERNAL": "false",
        }.get(key, default),
    )
    args = {
        "original_request": "請整理媽媽照護事件紀錄，回診時提供給我。",
        "grace_interpretation": "建立媽媽照護事件紀錄",
        "goal": {"objective": "整理照護事件作為回診資料"},
    }

    from plugins.openclaw_bridge.clawops_delegate import handle_clawops_delegate

    result = json.loads(handle_clawops_delegate(args))

    assert result["status"] == "rejected"
    assert result["reason"] == "topic_mismatch"
    assert result["task_created"] is False
    assert result["suggested_topic"]["thread_id"] == "2680"
    assert not db_path.exists()
