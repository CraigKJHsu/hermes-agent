from __future__ import annotations

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from plugins.openclaw_bridge.tools import pre_gateway_dispatch


def _event(text: str, *, platform=Platform.TELEGRAM, thread_id="2680"):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=platform,
            chat_id="-1003938559457",
            chat_type="group",
            thread_id=thread_id,
            user_id="kj",
        ),
    )


def test_unambiguous_natural_language_topic_rename_uses_native_title_command():
    result = pre_gateway_dispatch(
        event=_event("請幫我更改Topic名稱為失智患者的照護")
    )

    assert result == {
        "action": "rewrite",
        "text": "/title 失智患者的照護",
    }


def test_quoted_topic_rename_accepts_natural_polite_wording():
    result = pre_gateway_dispatch(
        event=_event("可以把這個 Topic 改成「失智患者的照護」嗎？")
    )

    assert result == {
        "action": "rewrite",
        "text": "/title 失智患者的照護",
    }


def test_quoted_topic_rename_preserves_internal_whitespace():
    result = pre_gateway_dispatch(
        event=_event("把這個 Topic 改成「Project  X」")
    )

    assert result == {
        "action": "rewrite",
        "text": "/title Project  X",
    }


def test_topic_rename_explanation_stays_with_grace():
    assert (
        pre_gateway_dispatch(
            event=_event("為什麼無法更改 Topic 名稱為失智患者的照護？")
        )
        is None
    )


def test_compound_topic_request_stays_with_grace_without_dropping_intent():
    assert (
        pre_gateway_dispatch(
            event=_event("把 Topic 改成照護筆記，然後刪除舊資料")
        )
        is None
    )


def test_quoted_compound_topic_request_stays_with_grace():
    assert (
        pre_gateway_dispatch(
            event=_event("把 Topic 改成「照護筆記」，然後列出待辦")
        )
        is None
    )


def test_multiline_topic_request_stays_with_grace():
    assert pre_gateway_dispatch(event=_event("把 Topic 改成照護筆記\n再列出待辦")) is None


def test_topic_questions_in_chinese_or_english_stay_with_grace():
    assert pre_gateway_dispatch(event=_event("這個 Topic 為什麼要改成「工作」？")) is None
    assert pre_gateway_dispatch(event=_event("修改這個主題為什麼會失敗？")) is None
    assert pre_gateway_dispatch(event=_event("How do I rename this Topic to Work?")) is None


def test_request_for_another_topic_stays_with_grace():
    assert pre_gateway_dispatch(event=_event("把另一個 Topic 改成照護筆記")) is None


def test_capability_question_and_named_topic_request_stay_with_grace():
    assert pre_gateway_dispatch(event=_event("Can I rename the Topic to Travel?")) is None
    assert (
        pre_gateway_dispatch(
            event=_event("Rename the Translator topic to English Practice")
        )
        is None
    )


def test_unrelated_quoted_reply_is_not_taken_as_the_title():
    assert (
        pre_gateway_dispatch(event=_event("把 Topic 改成 Foo，回覆「完成」"))
        is None
    )


def test_unpunctuated_compound_clause_is_not_taken_as_part_of_the_title():
    assert (
        pre_gateway_dispatch(event=_event("把這個主題改成照護並告訴我天氣"))
        is None
    )


def test_sentence_punctuation_and_mismatched_quotes_fail_closed():
    assert (
        pre_gateway_dispatch(event=_event("把這個 Topic 改成新名稱。刪除舊訊息"))
        is None
    )
    assert (
        pre_gateway_dispatch(
            event=_event('把這個 Topic 改成「新名，並刪除舊訊息"')
        )
        is None
    )
    assert pre_gateway_dispatch(event=_event("把這個主題改成「Roadmap』")) is None
    assert pre_gateway_dispatch(event=_event("把這個主題改成「Alpha”」")) is None
    assert (
        pre_gateway_dispatch(event=_event("把這個主題改成 Alpha．刪除舊筆記"))
        is None
    )
    assert (
        pre_gateway_dispatch(
            event=_event("rename this topic to Care and summarize this chat")
        )
        is None
    )


def test_residual_clause_and_boundary_newline_fail_closed():
    assert (
        pre_gateway_dispatch(
            event=_event("Rename this topic to Roadmap while you summarize the thread")
        )
        is None
    )
    assert pre_gateway_dispatch(event=_event("\nRename this topic to Roadmap")) is None
    assert (
        pre_gateway_dispatch(
            event=_event("把這個 Topic 改成 Project\u2028請告訴我目前狀態")
        )
        is None
    )


def test_native_topic_rename_does_not_apply_outside_telegram_topic_lane():
    assert (
        pre_gateway_dispatch(
            event=_event(
                "把 Topic 改成照護筆記",
                platform=Platform.DISCORD,
            )
        )
        is None
    )
