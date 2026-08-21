"""Prevent a fresh Grace execution request from reusing an old route error."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_FACEBOOK_EXECUTION_MARKERS = (
    "facebook_crosspost",
    "facebook_page_post",
    "marketplace",
    "carimali",
    "刊登",
    "發布",
    "社團發布流程",
)
_STALE_ROUTE_RESPONSE_MARKERS = (
    "name-bound facebook_crosspost contains an unrecognized external target",
    "發布合約缺少具名社團綁定能力",
    "不會重複嘗試相同路由",
    "與剛才已驗證被拒的合約相同",
)


def _called_clawops_delegate(
    messages: Sequence[Mapping[str, Any]],
    *,
    current_turn_user_idx: int,
) -> bool:
    for message in messages[current_turn_user_idx + 1:]:
        if str(message.get("role") or "") == "tool" and str(
            message.get("name") or message.get("tool_name") or ""
        ) == "clawops_delegate":
            return True
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if isinstance(function, Mapping) and str(
                function.get("name") or ""
            ) == "clawops_delegate":
                return True
    return False


def build_fresh_delegation_revalidation_nudge(
    *,
    user_message: str,
    final_response: str,
    messages: Sequence[Mapping[str, Any]],
    current_turn_user_idx: int,
    valid_tool_names: Sequence[str],
    attempts: int = 0,
) -> str | None:
    """Require current-turn tool evidence before repeating a prior rejection."""
    if attempts >= 1 or "clawops_delegate" not in set(valid_tool_names):
        return None
    user_text = str(user_message or "").casefold()
    if "[system: grace loop callback]" in user_text:
        return None
    if not (
        "facebook" in user_text
        and any(marker.casefold() in user_text for marker in _FACEBOOK_EXECUTION_MARKERS)
    ):
        return None
    response_text = str(final_response or "").casefold()
    if not any(
        marker.casefold() in response_text
        for marker in _STALE_ROUTE_RESPONSE_MARKERS
    ):
        return None
    if _called_clawops_delegate(
        messages,
        current_turn_user_idx=current_turn_user_idx,
    ):
        return None
    return (
        "[System: This is a fresh authenticated Facebook execution request. "
        "A validation result from an earlier user message is historical "
        "evidence, not the current route result; runtime code or schema may "
        "have changed. Call clawops_delegate now with the complete contract "
        "and the user's exact current listing and destination values. Do not "
        "claim that the current route rejected the contract unless this turn's "
        "tool result says so. This revalidation creates no Facebook action "
        "before an approval token is separately confirmed.]"
    )


__all__ = ["build_fresh_delegation_revalidation_nudge"]
