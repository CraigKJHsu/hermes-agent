from agent.delegation_revalidation import (
    build_fresh_delegation_revalidation_nudge,
)


USER_REQUEST = (
    "Grace，請建立全新的 Carimali Facebook 社團發布流程，"
    "使用 facebook_crosspost.group_names。"
)
STALE_RESPONSE = (
    "發布合約缺少具名社團綁定能力。這份請求與剛才已驗證被拒的合約相同；"
    "name-bound facebook_crosspost contains an unrecognized external target。"
)


def _nudge(messages, *, user_message=USER_REQUEST, attempts=0):
    return build_fresh_delegation_revalidation_nudge(
        user_message=user_message,
        final_response=STALE_RESPONSE,
        messages=messages,
        current_turn_user_idx=1,
        valid_tool_names=["clawops_delegate"],
        attempts=attempts,
    )


def test_fresh_execution_cannot_repeat_prior_rejection_without_current_tool_call():
    messages = [
        {"role": "tool", "name": "clawops_delegate", "content": "old reject"},
        {"role": "user", "content": USER_REQUEST},
    ]

    nudge = _nudge(messages)

    assert nudge is not None
    assert "Call clawops_delegate now" in nudge


def test_current_turn_clawops_call_satisfies_revalidation_guard():
    messages = [
        {"role": "assistant", "content": "old response"},
        {"role": "user", "content": USER_REQUEST},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "clawops_delegate",
                        "arguments": "{}",
                    },
                },
            ],
        },
    ]

    assert _nudge(messages) is None


def test_revalidation_guard_is_bounded_and_ignores_callbacks():
    messages = [{"role": "assistant"}, {"role": "user"}]

    assert _nudge(messages, attempts=1) is None
    assert _nudge(
        messages,
        user_message=(
            "[SYSTEM: Grace Loop callback] Carimali Facebook 發布"
        ),
    ) is None
