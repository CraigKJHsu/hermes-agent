"""Version comparison for live Grace operating instructions."""

from __future__ import annotations

import os
import re
import json
from pathlib import Path


_PATTERN = re.compile(r"GRACE_CLAWOPS_POLICY_VERSION:\s*([A-Za-z0-9._-]+)")
_SECTION = re.compile(
    r"(?ms)^## Grace to ClawOps Delegation Contract\s*$.*?(?=^##\s|\Z)"
)
_HORIZONTAL_SPACE = r"[ \t\u3000]"
_OPTIONAL_SPACE = _HORIZONTAL_SPACE + "*"
_APPROVAL_ATTEMPT = re.compile(
    r"^(?:(?:好吧|好的?|可以|沒問題|收到)"
    + _OPTIONAL_SPACE
    + r"(?:[，,、:：]"
    + _OPTIONAL_SPACE
    + r")?)?核准"
    + _HORIZONTAL_SPACE
    + r"+([^ \t\u3000，,。.!！？?、:：;；]+)"
    + r"(?:"
    + _OPTIONAL_SPACE
    + r"(?:[，,、]"
    + _OPTIONAL_SPACE
    + r")?(?:謝謝|麻煩了))?"
    + _OPTIONAL_SPACE
    + r"[。.!！]?$"
)
_VALID_APPROVAL_TOKEN = re.compile(r"^[0-9a-f]{16}$")
_KANBAN_TASK_ID = re.compile(r"(?<![A-Za-z0-9_])(t_[0-9a-f]{8})(?![A-Za-z0-9_])")
_SAVED_EVIDENCE_HINTS = (
    "已保存", "保存紀錄", "既有證據", "耐久證據", "durable evidence",
    "saved evidence",
)
_SAVED_EVIDENCE_FINALIZATION_ACTIONS = (
    "接續原任務", "完成原任務", "接續結案", "完成結案",
    "resume finalization", "continue finalization",
    "complete finalization",
)
_SAVED_EVIDENCE_FINALIZATION_REJECTIONS = (
    "不要", "不用", "不需", "取消", "停止", "是否", "嗎", "?", "？",
    "do not", "don't", "cancel", "stop", "whether",
)


def _saved_evidence_task_board(task_id: str) -> str | None:
    """Resolve the sole board containing an existing task, if discoverable."""
    from hermes_cli import kanban_db as kb

    matches: list[str] = []
    try:
        boards = kb.list_boards(include_archived=True)
    except Exception:
        boards = []
    for metadata in boards:
        slug = str(metadata.get("slug") or kb.DEFAULT_BOARD)
        try:
            with kb.connect_closing(board=slug) as conn:
                if conn.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
                ).fetchone() is not None:
                    matches.append(slug)
        except Exception:
            continue
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    return os.getenv("HERMES_KANBAN_BOARD", "").strip() or kb.DEFAULT_BOARD


def _policy_blocks_from_text(text: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in _SECTION.finditer(str(text or ""))
    ]


def active_policy_version() -> str:
    path = Path(os.getenv("HERMES_AGENTS_POLICY", "~/.hermes/AGENTS.md")).expanduser()
    if not path.exists():
        return ""
    match = _PATTERN.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def stored_prompt_matches_active_policy(prompt: str) -> bool:
    current = str(prompt or "")
    block = active_policy_prompt_block()
    if block:
        return _policy_blocks_from_text(current) == [block]
    version = active_policy_version()
    return not version or f"GRACE_CLAWOPS_POLICY_VERSION: {version}" in current


def active_policy_prompt_block() -> str:
    """Return the live Grace/ClawOps section that must survive prompt assembly."""
    configured_path = os.getenv("HERMES_AGENTS_POLICY", "").strip()
    path = Path(configured_path or "~/.hermes/AGENTS.md").expanduser()
    if not path.exists():
        if configured_path:
            raise RuntimeError(
                f"Configured Grace policy file does not exist: {path}"
            )
        return ""
    match = _SECTION.search(path.read_text(encoding="utf-8"))
    if match is None:
        if configured_path:
            raise RuntimeError(
                "Configured Grace policy file is missing the "
                "'Grace to ClawOps Delegation Contract' section."
            )
        return ""
    return match.group(0).strip()


def ensure_active_policy_prompt(prompt: str) -> str:
    """Append the active policy when Hermes' cwd-based prompt builder omitted it."""
    current = str(prompt or "")
    block = active_policy_prompt_block()
    existing_blocks = _policy_blocks_from_text(current)
    if not block or existing_blocks == [block]:
        return current
    if existing_blocks:
        replacements = iter([f"{block}\n\n"] + [""] * (len(existing_blocks) - 1))
        return _SECTION.sub(lambda _match: next(replacements), current)
    return f"{current.rstrip()}\n\n# Live Grace Operating Policy\n\n{block}\n"


def approval_turn_prompt(message_text: str) -> str:
    """Force token-shaped approval turns through the authoritative tool."""
    match = _APPROVAL_ATTEMPT.fullmatch(str(message_text or "").strip())
    if match is None:
        return ""
    return (
        "[Trusted approval-turn routing]\n"
        "The current authenticated user message contains an approval-token attempt. "
        "Do not judge its wording, validity, expiry, action, platform, or scope "
        "from conversation history and do not answer with approval instructions "
        "from memory. You MUST call clawops_delegate now with the unchanged "
        "contract and the approval_token copied from the user message. The tool "
        "result is authoritative. "
        "If it reports an expired or invalid challenge, state that exact reason; "
        "then retry the same unchanged contract without approval_token only when "
        "a fresh replacement challenge is still required. Never claim punctuation "
        "or polite wording is invalid unless the tool actually returns that error."
    )


def approval_token_candidate(message_text: str) -> str:
    """Return a syntactically valid challenge token from an approval turn."""
    match = _APPROVAL_ATTEMPT.fullmatch(str(message_text or "").strip())
    if match is None:
        return ""
    candidate = match.group(1)
    return candidate if _VALID_APPROVAL_TOKEN.fullmatch(candidate) else ""


def approval_attempt_candidate(message_text: str) -> str:
    """Return a candidate only when the whole message is an approval turn.

    Natural requests may discuss approval or tokens. They must not be routed
    as approval replies unless the entire authenticated message has the narrow
    approval-command shape.
    """
    match = _APPROVAL_ATTEMPT.fullmatch(str(message_text or "").strip())
    return match.group(1) if match is not None else ""


def marketplace_readonly_turn_prompt(message_text: str) -> str:
    """Force one exact Marketplace group-status audit through the safe route."""
    from proactive.loop_contract import (
        canonical_marketplace_readonly_delegate_args,
        marketplace_readonly_user_request_listing_id,
    )

    listing_id = marketplace_readonly_user_request_listing_id(message_text)
    if listing_id is None:
        return ""
    fixed_args = canonical_marketplace_readonly_delegate_args(listing_id)
    return (
        "[Trusted exact Marketplace read-only routing]\n"
        "The current authenticated message is one exact read-only inspection, "
        "not an approval turn and not a publishing request. Do not answer from "
        "conversation history and do not create, request, or consume an approval "
        "token. You MUST call clawops_delegate now. Set original_request to the "
        "current authenticated user message verbatim, omit approval_token and "
        "facebook_crosspost, and use these exact remaining arguments:\n"
        + json.dumps(fixed_args, ensure_ascii=False, sort_keys=True)
        + "\nThe clawops_delegate result is authoritative."
    )


def saved_evidence_finalization_turn_prompt(message_text: str) -> str:
    """Route an exact saved-evidence continuation to its original task.

    The task id plus an explicit evidence/finalization hint are both required,
    so ordinary progress questions mentioning a Kanban id stay read-only.
    """
    current = str(message_text or "")
    task_ids = {match.group(1) for match in _KANBAN_TASK_ID.finditer(current)}
    normalized = current.casefold()
    if (
        len(task_ids) != 1
        or any(marker in normalized for marker in _SAVED_EVIDENCE_FINALIZATION_REJECTIONS)
        or not any(
            hint.casefold() in normalized
            for hint in _SAVED_EVIDENCE_HINTS
        )
        or not any(
            action.casefold() in normalized
            for action in _SAVED_EVIDENCE_FINALIZATION_ACTIONS
        )
    ):
        return ""
    task_id = next(iter(task_ids))
    board = _saved_evidence_task_board(task_id)
    if board is None:
        return ""
    return (
        "[Trusted saved-evidence finalization routing]\n"
        "The authenticated user is referring to one existing blocked commerce "
        "execution and its already-saved evidence. This is not a new Facebook "
        "audit and not a callback continuation. You MUST call "
        "clawops_finalize_saved_evidence now with exactly "
        f'{json.dumps({"execution_task_id": task_id, "board": board}, sort_keys=True)}. '
        "Do not call clawops_delegate, do not construct a new Loop Contract, "
        "and do not open any browser. The tool result is authoritative."
    )
