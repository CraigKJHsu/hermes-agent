from gateway.response_filters import (
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
    should_suppress_successful_internal_response,
)


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


def test_blank_and_prose_mentions_are_not_silence():
    assert not is_intentional_silence_response("")
    assert not is_intentional_silence_response("Use NO_REPLY when no answer is needed.")
    assert not is_intentional_silence_response("The reply was [SILENT], intentionally.")


def test_failed_agent_result_never_counts_as_intentional_silence():
    assert is_intentional_silence_agent_result({"failed": False}, "NO_REPLY")
    assert not is_intentional_silence_agent_result({"failed": True}, "NO_REPLY")


def test_structural_internal_suppression_only_hides_successful_internal_turns():
    context = {"suppress_successful_response": True}

    assert should_suppress_successful_internal_response(
        internal=True,
        internal_context=context,
        agent_result={"failed": False},
    )
    assert not should_suppress_successful_internal_response(
        internal=False,
        internal_context=context,
        agent_result={"failed": False},
    )
    assert not should_suppress_successful_internal_response(
        internal=True,
        internal_context=context,
        agent_result={"failed": True},
    )
