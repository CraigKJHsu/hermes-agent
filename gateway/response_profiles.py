"""Trusted Topic response-profile normalization and fast-lane dispatch.

Topic configuration selects a named profile.  The platform adapter resolves
that name from operator-controlled configuration, while the gateway executes
the normalized strategy before the ordinary conversational model is started.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from gateway.fast_translation import (
    build_detail_lane_prompt,
    build_failed_lane_prompt,
    clean_fast_translation_output,
    eligible_fast_translation_text,
    format_fast_translation,
    normalize_fast_translation_config,
    prepare_direct_translation_input,
    run_fast_translation,
)


FastLaneRunner = Callable[[str, Dict[str, Any]], Awaitable[str]]


class NormalizedResponseProfile(dict):
    """Process-local profile type that serialized configuration cannot forge."""


class ResponseProfileConfigurationError(RuntimeError):
    """Raised when a Topic explicitly binds an unavailable response profile."""


_FAST_LANE_HANDLERS: Dict[str, Dict[str, Any]] = {
    "translation": {
        "normalize": normalize_fast_translation_config,
        "eligible": eligible_fast_translation_text,
        "prepare": prepare_direct_translation_input,
        "run": run_fast_translation,
        "clean": clean_fast_translation_output,
        "format": format_fast_translation,
        "detail_prompt": build_detail_lane_prompt,
        "failed_prompt": build_failed_lane_prompt,
    },
}

_DETAIL_OUTPUT_CONTRACT_PROMPTS = {
    "translator_mastery": (
        "[Trusted Translator Topic output contract: Classify the current user "
        "input into exactly one of these three types: (1) primarily Chinese "
        "text, (2) an English single word or non-sentential phrase, or (3) an "
        "English sentence or passage. Then use the matching template below. "
        "The detailed response is invalid if any required numbered heading or "
        "required field for that type is omitted. Use Traditional Chinese for "
        "all teaching explanations.\n"
        "Before answering, inspect the available conversation history and "
        "memory for the same or a highly related word, phrase, or sentence. "
        "Only when verifiable prior evidence exists, begin with "
        "`### 🔔 學習紀錄提醒` and include `**複習時間與情境**` plus "
        "`**溫馨提醒**`. Never fabricate a prior occurrence, date, or context. "
        "This history check belongs only to the detailed lane and must not "
        "delay the already separate fast reply.\n"
        "TYPE 1 — primarily Chinese input. Use exactly these headings in order:\n"
        "### 1. 自然道地英文翻譯\n"
        "Provide 2-3 materially different English expressions, such as daily, "
        "formal/business, or idiomatic variants when applicable.\n"
        "### 2. 句型結構與語法解析\n"
        "Include `**核心句型**` and `**文法與用詞特點**`.\n"
        "### 3. 精選核心單字\n"
        "Select 1-2 words. For each, include the word, part of speech, Chinese "
        "meaning, `**字根拆解**`, and `**記憶口訣／聯想**`. Do not invent "
        "morphemes; use a reliable etymology or state that no useful modern "
        "decomposition exists. Put each field's substantive content on the "
        "same line, or indent any continuation line beneath that field.\n"
        "### 4. 實用英文例句\n"
        "Provide at least one natural English example with a Chinese translation.\n"
        "TYPE 2 — English single word or non-sentential phrase. Use exactly "
        "these headings in order:\n"
        "### 1. 基本資訊與翻譯\n"
        "Include `**單字／詞彙**`, `**音標**` using IPA or K.K., and "
        "`**詞性與繁體中文解釋**`.\n"
        "### 2. 構詞拆解（字根、字首、字尾）\n"
        "Include `**字首 (Prefix)**`, `**字根 (Root)**`, and "
        "`**字尾 (Suffix)**`. If no meaningful affix or root analysis exists, "
        "explicitly explain the reliable etymology or word-formation logic; "
        "never invent a decomposition.\n"
        "### 3. 記憶法與聯想助手\n"
        "Include `**邏輯組合**` and `**記憶小撇步**`.\n"
        "### 4. 實用例句\n"
        "Provide exactly two natural English examples, each with a Traditional "
        "Chinese translation.\n"
        "### 5. 延伸學習\n"
        "Include `**同／反義詞**` and `**常用搭配詞 (Collocation)**` with "
        "2-3 common collocations.\n"
        "TYPE 3 — English sentence or passage. Use exactly these headings in order:\n"
        "### 1. 整句翻譯\n"
        "Include `**繁體中文翻譯**` with a natural, complete translation.\n"
        "### 2. 句型結構與文法解析\n"
        "Include `**核心句型**`, `**句子成分拆解**` using S/V/O/C plus "
        "modifiers or clauses as applicable, and `**關鍵文法焦點**`.\n"
        "### 3. 核心單字字根拆解\n"
        "Select 1-2 key words and include part of speech, Chinese meaning, "
        "`**字根拆解**`, and `**記憶提示**`; never invent morphology.\n"
        "Put each field's substantive content on the same line, or indent any "
        "continuation line beneath that field.\n"
        "### 4. 句型延伸與仿寫造句\n"
        "Include `**句型套用範例**` and its Traditional Chinese translation.\n"
        "The first fast reply may already contain a direct translation or K.K. "
        "line. Nevertheless, this detailed teaching response must be "
        "self-contained and must include every translation and pronunciation "
        "field required by the selected template. Do not omit a required field "
        "merely to avoid duplication. Keep the tone natural, light, and "
        "consistent with Grace's persona.]"
    ),
    "vocabulary_full": (
        "[Trusted response-profile output contract: When the current input is "
        "a single English word, the detailed response is invalid unless it "
        "contains all five Markdown headings below, in this exact order, with "
        "substantive Traditional Chinese content under every heading:\n"
        "**詞性與核心用法**\n"
        "**語意辨析**\n"
        "**字根／字首／字尾**\n"
        "**常見搭配**\n"
        "**英中例句**\n"
        "Never omit a heading. The morphology section must explicitly state "
        "when there is no productive modern-English prefix or suffix, and "
        "then give reliable etymology when known; never invent a decomposition. "
        "The semantic distinction must compare the most easily confused terms. "
        "The collocation section needs at least two items. The example section "
        "needs at least one natural English sentence and its Traditional "
        "Chinese translation. Do not repeat the already delivered standalone "
        "translation or K.K. line. For non-single-word input, follow the "
        "selected detail skill without forcing this five-section template.]"
    ),
}

# Keep the original self-contained contract as a compatibility alias, while
# exposing two outcome-specific contracts for new Topic configurations.
_DETAIL_OUTPUT_CONTRACT_PROMPTS["translator_mastery_self_contained"] = (
    _DETAIL_OUTPUT_CONTRACT_PROMPTS["translator_mastery"]
)
_DETAIL_OUTPUT_CONTRACT_PROMPTS["translator_mastery_after_fast"] = (
    _DETAIL_OUTPUT_CONTRACT_PROMPTS["translator_mastery"]
    .replace(
        "Include `**繁體中文翻譯**` with a natural, complete translation.",
        "Do not repeat the full translation. Include exactly this short handoff "
        "line: `已於上一則快速翻譯提供，這裡不重複全文。` and then continue "
        "with the teaching sections.",
    )
    .replace(
        "The first fast reply may already contain a direct translation or K.K. "
        "line. Nevertheless, this detailed teaching response must be "
        "self-contained and must include every translation and pronunciation "
        "field required by the selected template. Do not omit a required field "
        "merely to avoid duplication. Keep the tone natural, light, and "
        "consistent with Grace's persona.",
        "A confirmed fast reply has already delivered the directly usable "
        "translation and any required K.K. line. Continue the lesson without "
        "repeating a full sentence or passage translation. For TYPE 1, provide "
        "materially different alternative expressions instead of copying the "
        "fast reply. For TYPE 2, concise word meaning and pronunciation fields "
        "remain required because they anchor the teaching format. For TYPE 3, "
        "the first numbered section must contain only the required handoff line. "
        "Keep the tone natural, light, and consistent with Grace's persona.",
    )
)

_LEARNING_HISTORY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "from",
    "has",
    "have",
    "in",
    "is",
    "it",
    "of",
    "on",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}


def _normalize_learning_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    # Persisted Telegram user messages are prefixed with the display name.
    text = re.sub(r"^\[[^\]\n]{1,80}\]\s*", "", text)
    return re.sub(r"[^\w\u3400-\u9fff']+", " ", text).strip()


def _learning_text_related(current: str, previous: str) -> bool:
    if not current or not previous:
        return False
    if current == previous:
        return True

    current_english = {
        token
        for token in re.findall(r"[a-z]+(?:'[a-z]+)?", current)
        if len(token) >= 4 and token not in _LEARNING_HISTORY_STOPWORDS
    }
    previous_english = {
        token
        for token in re.findall(r"[a-z]+(?:'[a-z]+)?", previous)
        if len(token) >= 4 and token not in _LEARNING_HISTORY_STOPWORDS
    }
    shared_english = current_english & previous_english
    if shared_english and (
        current_english <= previous_english
        or previous_english <= current_english
        or len(shared_english) / min(
            len(current_english),
            len(previous_english),
        ) >= 0.5
    ):
        return True

    current_cjk = "".join(re.findall(r"[\u3400-\u9fff]", current))
    previous_cjk = "".join(re.findall(r"[\u3400-\u9fff]", previous))
    if min(len(current_cjk), len(previous_cjk)) >= 2 and (
        current_cjk in previous_cjk or previous_cjk in current_cjk
    ):
        return True
    return False


def learning_history_evidence(
    current_text: Any,
    history: Any,
    *,
    limit: int = 3,
) -> list[Dict[str, Any]]:
    """Return recent, verifiable prior user turns related to the current text."""
    current = _normalize_learning_text(current_text)
    if not current or not isinstance(history, list):
        return []

    evidence: list[Dict[str, Any]] = []
    for message in reversed(history):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        previous_raw = str(message.get("content") or "").strip()
        previous = _normalize_learning_text(previous_raw)
        if not _learning_text_related(current, previous):
            continue
        timestamp = message.get("timestamp")
        local_time = None
        try:
            local_time = datetime.fromtimestamp(
                float(timestamp),
            ).astimezone().isoformat(timespec="minutes")
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        evidence.append({
            "time": local_time,
            "match": "exact" if current == previous else "related",
        })
        if len(evidence) >= max(1, min(int(limit), 5)):
            break
    return evidence


def build_learning_history_note(current_text: Any, history: Any) -> str:
    """Build a trusted, injection-safe reminder decision for the detail lane."""
    evidence = learning_history_evidence(current_text, history)
    if not evidence:
        return (
            "[Trusted Translator learning-history precheck: result=no_match. "
            "No verifiable related prior user turn was found. Do not include "
            "the `### 🔔 學習紀錄提醒` heading in this response.]"
        )
    serialized = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    return (
        "[Trusted Translator learning-history precheck: result=verified_match. "
        "The detailed response MUST begin with "
        "`### 🔔 學習紀錄提醒`, followed by `**複習時間與情境**` and "
        "`**溫馨提醒**`, before the numbered teaching template. Use only the "
        "following program-derived evidence to describe prior learning. It "
        "contains only local timestamps and exact/related match types; no "
        "historical user text has been promoted into this trusted prompt. "
        f"evidence={serialized}]"
    )


_TRANSLATOR_CONTRACTS = frozenset({
    "translator_mastery",
    "translator_mastery_after_fast",
    "translator_mastery_self_contained",
})

_TRANSLATOR_TEMPLATE_REQUIREMENTS = {
    "chinese": (
        "### 1. 自然道地英文翻譯",
        "### 2. 句型結構與語法解析",
        "**核心句型**",
        "**文法與用詞特點**",
        "### 3. 精選核心單字",
        "**字根拆解**",
        "**記憶口訣／聯想**",
        "### 4. 實用英文例句",
    ),
    "english_term": (
        "### 1. 基本資訊與翻譯",
        "**單字／詞彙**",
        "**音標**",
        "**詞性與繁體中文解釋**",
        "### 2. 構詞拆解（字根、字首、字尾）",
        "**字首 (Prefix)**",
        "**字根 (Root)**",
        "**字尾 (Suffix)**",
        "### 3. 記憶法與聯想助手",
        "**邏輯組合**",
        "**記憶小撇步**",
        "### 4. 實用例句",
        "### 5. 延伸學習",
        "**同／反義詞**",
        "**常用搭配詞 (Collocation)**",
    ),
    "english_sentence": (
        "### 1. 整句翻譯",
        "### 2. 句型結構與文法解析",
        "**核心句型**",
        "**句子成分拆解**",
        "**關鍵文法焦點**",
        "### 3. 核心單字字根拆解",
        "**字根拆解**",
        "**記憶提示**",
        "### 4. 句型延伸與仿寫造句",
        "**句型套用範例**",
    ),
}

_TRANSLATOR_REPEATABLE_FIELD_PAIRS = {
    "chinese": (
        ("**字根拆解**", "**記憶口訣／聯想**"),
    ),
    "english_sentence": (
        ("**字根拆解**", "**記憶提示**"),
    ),
}

_TRANSLATOR_REPEATABLE_FIELD_SECTIONS = {
    "chinese": (
        "### 3. 精選核心單字",
        "### 4. 實用英文例句",
    ),
    "english_sentence": (
        "### 3. 核心單字字根拆解",
        "### 4. 句型延伸與仿寫造句",
    ),
}


def _normalize_translator_template_markers(value: str) -> str:
    """Canonicalize harmless Markdown variants before contract validation."""
    normalized = str(value or "")
    markers = {
        marker
        for template in _TRANSLATOR_TEMPLATE_REQUIREMENTS.values()
        for marker in template
    } | {
        "### 🔔 學習紀錄提醒",
        "**複習時間與情境**",
        "**溫馨提醒**",
        "**繁體中文翻譯**",
    }
    for marker in markers:
        if marker.startswith("### "):
            title = marker[4:]
            normalized = re.sub(
                rf"(?m)^\s*#{{1,6}}\s+{re.escape(title)}\s*$",
                marker,
                normalized,
            )
        elif marker.startswith("**") and marker.endswith("**"):
            label = marker[2:-2]
            normalized = re.sub(
                rf"\*\*{re.escape(label)}\s*([:：])\s*\*\*",
                rf"{marker}\1",
                normalized,
            )
    return normalized


def classify_translator_input(value: Any) -> str:
    """Classify Translator Topic input for deterministic format validation."""
    text = str(value or "").strip()
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)
    if cjk_count and cjk_count >= len(latin_words):
        return "chinese"
    if len(latin_words) <= 1:
        return "english_term"

    lowered_words = [word.lower() for word in latin_words]
    short_sentence_starters = {
        "i", "you", "he", "she", "it", "we", "they",
        "this", "that", "these", "those",
    }
    finite_or_auxiliary_verbs = {
        "am", "is", "are", "was", "were",
        "have", "has", "had",
        "do", "does", "did",
        "can", "could", "will", "would", "shall", "should",
        "may", "might", "must",
    }
    common_imperative_starters = {
        "ask", "bring", "call", "check", "close", "come", "continue",
        "explain", "find", "give", "go", "help", "keep", "let", "listen",
        "look", "make", "open", "read", "remember", "show", "stop",
        "take", "tell", "translate", "turn", "use", "wait", "write",
    }
    looks_like_short_sentence = bool(
        len(lowered_words) >= 2
        and (
            lowered_words[0] in short_sentence_starters
            or lowered_words[0] in common_imperative_starters
            or any(
                word in finite_or_auxiliary_verbs
                for word in lowered_words[1:]
            )
        )
    )
    if (
        "\n" in text
        or bool(re.search(r"[.!?][\"')\]]?\s*$", text))
        or len(latin_words) >= 4
        or looks_like_short_sentence
    ):
        return "english_sentence"
    return "english_term"


def translator_detail_validation_errors(
    contract_name: Any,
    current_text: Any,
    response_text: Any,
    learning_history_note: Any = "",
    *,
    fast_output: Any = "",
) -> list[str]:
    """Return human-readable contract violations for a Translator detail draft."""
    contract = str(contract_name or "").strip().lower()
    if contract not in _TRANSLATOR_CONTRACTS:
        return []

    response = _normalize_translator_template_markers(
        str(response_text or "").strip()
    )
    if not response:
        return ["詳細教學回覆是空白。"]

    errors: list[str] = []
    verified_history = "result=verified_match" in str(learning_history_note or "")
    reminder_heading = "### 🔔 學習紀錄提醒"
    if verified_history:
        if not response.startswith(reminder_heading):
            errors.append("有可驗證的歷史紀錄，回覆必須以學習紀錄提醒開頭。")
        for field in ("**複習時間與情境**", "**溫馨提醒**"):
            if field not in response:
                errors.append(f"學習紀錄提醒缺少必要欄位：{field}")
    elif reminder_heading in response:
        errors.append("沒有可驗證的歷史紀錄，不得加入學習紀錄提醒。")

    input_type = classify_translator_input(current_text)
    # Two- and three-word English inputs are inherently ambiguous without
    # semantic analysis ("school curriculum" vs. "Dogs bark"). The trusted
    # contract already asks the model to classify them, so validate whichever
    # one complete English template the draft selected instead of overriding
    # that semantic choice with an incomplete verb dictionary.
    latin_word_count = len(
        re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", str(current_text or ""))
    )
    raw_current_text = str(current_text or "").strip()
    lowered_current_words = [
        word.lower()
        for word in re.findall(
            r"[A-Za-z]+(?:'[A-Za-z]+)?",
            raw_current_text,
        )
    ]
    clearly_sentential = bool(
        "\n" in raw_current_text
        or re.search(r"[.!?][\"')\]]?\s*$", raw_current_text)
        or (
            lowered_current_words
            and (
                lowered_current_words[0] in {
                    "i", "you", "he", "she", "it", "we", "they",
                    "this", "that", "these", "those",
                    "ask", "bring", "call", "check", "close", "come",
                    "continue", "explain", "find", "give", "go", "help",
                    "keep", "let", "listen", "look", "make", "open",
                    "read", "remember", "show", "stop", "take", "tell",
                    "translate", "turn", "use", "wait", "write",
                }
                or any(
                    word in {
                        "am", "is", "are", "was", "were",
                        "have", "has", "had", "do", "does", "did",
                        "can", "could", "will", "would", "shall", "should",
                        "may", "might", "must",
                    }
                    for word in lowered_current_words[1:]
                )
            )
        )
    )
    phrase_function_words = {
        "a", "an", "the", "of", "to", "in", "on", "for",
        "with", "by", "at", "from",
    }
    looks_phrase_ambiguous = bool(
        2 <= latin_word_count <= 3
        or (
            latin_word_count >= 4
            and sum(
                word in phrase_function_words
                for word in lowered_current_words
            ) >= 2
            and raw_current_text == raw_current_text.lower()
        )
    )
    if (
        looks_phrase_ambiguous
        and input_type != "chinese"
        and not clearly_sentential
    ):
        has_term_template = "### 1. 基本資訊與翻譯" in response
        has_sentence_template = "### 1. 整句翻譯" in response
        if has_term_template != has_sentence_template:
            input_type = (
                "english_term"
                if has_term_template
                else "english_sentence"
            )
    requirements = _TRANSLATOR_TEMPLATE_REQUIREMENTS[input_type]
    repeatable_field_pairs = _TRANSLATOR_REPEATABLE_FIELD_PAIRS.get(
        input_type,
        (),
    )
    repeatable_fields = {
        field
        for pair in repeatable_field_pairs
        for field in pair
    }
    repeatable_section = _TRANSLATOR_REPEATABLE_FIELD_SECTIONS.get(
        input_type
    )
    repeatable_section_start = -1
    repeatable_section_end = -1
    repeatable_section_text = ""
    if repeatable_section:
        section_heading, next_heading = repeatable_section
        repeatable_section_start = response.find(section_heading)
        if repeatable_section_start >= 0:
            repeatable_section_end = response.find(
                next_heading,
                repeatable_section_start + len(section_heading),
            )
            if repeatable_section_end < 0:
                repeatable_section_end = len(response)
            repeatable_section_text = response[
                repeatable_section_start + len(section_heading):
                repeatable_section_end
            ]
    positions: list[int] = []
    for field in requirements:
        search_text = (
            repeatable_section_text
            if field in repeatable_fields
            else response
        )
        relative_position = search_text.find(field)
        position = relative_position
        if field in repeatable_fields and relative_position >= 0:
            position += repeatable_section_start
        if position < 0:
            errors.append(f"缺少必要標題或欄位：{field}")
        else:
            positions.append(position)
            field_count = search_text.count(field)
            if field in repeatable_fields:
                if not 1 <= field_count <= 2:
                    errors.append(
                        f"核心單字子欄位必須出現一至兩次：{field}"
                    )
            elif field_count != 1:
                errors.append(f"必要標題或欄位必須且只能出現一次：{field}")
    if len(positions) == len(requirements) and positions != sorted(positions):
        errors.append("必要標題或欄位的順序不正確。")

    for first_field, second_field in repeatable_field_pairs:
        first_positions = [
            match.start()
            for match in re.finditer(
                re.escape(first_field),
                repeatable_section_text,
            )
        ]
        second_positions = [
            match.start()
            for match in re.finditer(
                re.escape(second_field),
                repeatable_section_text,
            )
        ]
        if len(first_positions) != len(second_positions):
            errors.append(
                "每個核心單字都必須同時包含"
                f"{first_field}與{second_field}。"
            )
            continue
        paired_positions = [
            position
            for pair in zip(first_positions, second_positions)
            for position in pair
        ]
        if paired_positions != sorted(paired_positions):
            errors.append(
                "核心單字子欄位必須依序成對排列："
                f"{first_field}後接{second_field}。"
            )

    first_numbered_heading = requirements[0]
    if not verified_history and not response.startswith(first_numbered_heading):
        errors.append("回覆開頭不得在第一個必要標題前加入額外內容。")

    all_markers = sorted(
        {
            marker
            for template in _TRANSLATOR_TEMPLATE_REQUIREMENTS.values()
            for marker in template
        }
        | {
            reminder_heading,
            "**複習時間與情境**",
            "**溫馨提醒**",
            "**繁體中文翻譯**",
        },
        key=len,
        reverse=True,
    )

    def _content_after(marker: str) -> str:
        start = response.find(marker)
        if start < 0:
            return ""
        start += len(marker)
        end = len(response)
        for boundary in all_markers:
            position = response.find(boundary, start)
            if position >= 0:
                end = min(end, position)
        return response[start:end].strip()

    def _has_substance(value: str) -> bool:
        cleaned = re.sub(r"[\s#>*_`~:：\-–—•]+", "", value or "")
        return bool(re.search(r"[A-Za-z0-9\u3400-\u9fff]", cleaned))

    if repeatable_section:
        def _field_block_has_substance(
            match: re.Match[str],
            boundary: int,
        ) -> bool:
            """Accept inline content or an immediately nested Markdown block."""
            line_start = repeatable_section_text.rfind(
                "\n", 0, match.start()
            ) + 1
            marker_line = repeatable_section_text[
                line_start:match.start()
            ]
            marker_indent = len(marker_line) - len(marker_line.lstrip())
            line_end = repeatable_section_text.find("\n", match.end())
            if line_end < 0 or line_end > boundary:
                line_end = boundary
            if _has_substance(
                repeatable_section_text[match.end():line_end]
            ):
                return True

            cursor = line_end + 1
            while cursor < boundary:
                next_end = repeatable_section_text.find("\n", cursor)
                if next_end < 0 or next_end > boundary:
                    next_end = boundary
                line = repeatable_section_text[cursor:next_end]
                if line.strip():
                    indentation = len(line) - len(line.lstrip())
                    if indentation <= marker_indent:
                        return False
                    if _has_substance(line):
                        return True
                cursor = next_end + 1
            return False

        incomplete_repeatable_content = False
        for first_field, second_field in repeatable_field_pairs:
            section_text = repeatable_section_text
            first_matches = list(
                re.finditer(re.escape(first_field), section_text)
            )
            second_matches = list(
                re.finditer(re.escape(second_field), section_text)
            )
            if len(first_matches) != len(second_matches):
                continue
            for index, (first_match, second_match) in enumerate(
                zip(first_matches, second_matches)
            ):
                if first_match.start() >= second_match.start():
                    continue
                second_end = (
                    first_matches[index + 1].start()
                    if index + 1 < len(first_matches)
                    else len(section_text)
                )
                if not (
                    _field_block_has_substance(
                        first_match,
                        second_match.start(),
                    )
                    and _field_block_has_substance(
                        second_match,
                        second_end,
                    )
                ):
                    incomplete_repeatable_content = True
                    break
            if incomplete_repeatable_content:
                break
        if incomplete_repeatable_content:
            errors.append(
                "每個核心單字的字根與記憶子欄位"
                "都必須各自提供實質內容。"
            )

    content_fields = [
        marker
        for marker in requirements
        if (
            not marker.startswith("### ")
            and marker not in repeatable_fields
        )
    ]
    if verified_history:
        content_fields.extend(("**複習時間與情境**", "**溫馨提醒**"))
        for field in (
            reminder_heading,
            "**複習時間與情境**",
            "**溫馨提醒**",
        ):
            if response.count(field) != 1:
                errors.append(f"學習紀錄標題或欄位必須且只能出現一次：{field}")
    if (
        input_type == "english_sentence"
        and contract != "translator_mastery_after_fast"
    ):
        content_fields.append("**繁體中文翻譯**")
    for field in dict.fromkeys(content_fields):
        if field not in response:
            continue
        field_contents = [_content_after(field)]
        if any(not _has_substance(content) for content in field_contents):
            errors.append(f"必要欄位沒有實質內容：{field}")

    def _section(first_heading: str, next_heading: Optional[str]) -> str:
        start = response.find(first_heading)
        if start < 0:
            return ""
        start += len(first_heading)
        end = (
            response.find(next_heading, start)
            if next_heading
            else len(response)
        )
        if end < 0:
            end = len(response)
        return response[start:end].strip()

    if input_type == "chinese":
        translation_section = _section(
            "### 1. 自然道地英文翻譯",
            "### 2. 句型結構與語法解析",
        )
        expression_lines = re.findall(
            r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S.+$",
            translation_section,
        )
        if not 2 <= len(expression_lines) <= 3:
            errors.append("自然道地英文翻譯必須提供兩至三種實質不同的表達。")
        normalized_expressions = {
            re.sub(
                r"[\W_]+",
                "",
                re.sub(
                    r"^\s*(?:[-*]|\d+[.)])\s+",
                    "",
                    line,
                ).lower(),
            )
            for line in expression_lines
        }
        if len(normalized_expressions) != len(expression_lines):
            errors.append("自然道地英文翻譯的各種表達不得重複。")
        example_section = _section("### 4. 實用英文例句", None)
        if not (
            re.search(r"[A-Za-z]", example_section)
            and re.search(r"[\u3400-\u9fff]", example_section)
        ):
            errors.append("實用英文例句區缺少英文例句與繁體中文翻譯。")
    elif input_type == "english_term":
        example_section = _section(
            "### 4. 實用例句",
            "### 5. 延伸學習",
        )
        example_markers = list(
            re.finditer(
                r"(?m)^\s*(\d+)[.)]\s+\S.*$",
                example_section,
            )
        )
        example_numbers = [
            marker.group(1)
            for marker in example_markers
        ]
        if example_numbers != ["1", "2"]:
            errors.append("實用例句必須包含編號 1、2 的兩個英中例句。")
        example_blocks = [
            example_section[
                marker.start():(
                    example_markers[index + 1].start()
                    if index + 1 < len(example_markers)
                    else len(example_section)
                )
            ]
            for index, marker in enumerate(example_markers)
        ]
        if any(
            not (
                re.search(r"[A-Za-z]", block)
                and re.search(r"[\u3400-\u9fff]", block)
            )
            for block in example_blocks
        ):
            errors.append("每個實用例句都必須同時包含英文與繁體中文翻譯。")
        collocations = _content_after("**常用搭配詞 (Collocation)**")
        collocation_items = re.findall(
            r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S.+$",
            collocations,
        )
        inline_items = [
            item.strip()
            for item in re.split(r"[,，、;；]", collocations)
            if _has_substance(item)
        ]
        collocation_count = (
            len(collocation_items)
            if collocation_items
            else len(inline_items)
        )
        if not 2 <= collocation_count <= 3:
            errors.append("常用搭配詞必須提供兩至三個項目。")

    if input_type == "english_sentence":
        if contract == "translator_mastery_after_fast":
            handoff = "已於上一則快速翻譯提供，這裡不重複全文。"
            if handoff not in response:
                errors.append("快速翻譯成功後，整句翻譯區必須使用不重複全文的銜接句。")
            first_heading = "### 1. 整句翻譯"
            second_heading = "### 2. 句型結構與文法解析"
            first_start = response.find(first_heading)
            second_start = response.find(second_heading)
            if 0 <= first_start < second_start:
                first_section = response[
                    first_start + len(first_heading):second_start
                ].strip()
                if first_section != handoff:
                    errors.append("快速翻譯成功後，整句翻譯區只能包含指定銜接句。")
            if "**繁體中文翻譯**" in response:
                errors.append("快速翻譯成功後，詳細回覆不得再次提供完整繁體中文翻譯。")
        else:
            if "**繁體中文翻譯**" not in response:
                errors.append("自足詳細回覆缺少完整的繁體中文翻譯欄位。")
            elif response.count("**繁體中文翻譯**") != 1:
                errors.append("繁體中文翻譯欄位必須且只能出現一次。")
            translation_content = _content_after("**繁體中文翻譯**")
            if translation_content and not re.search(
                r"[\u3400-\u9fff]",
                translation_content,
            ):
                errors.append("自足繁體中文翻譯欄位必須包含中文譯文。")
            if "已於上一則快速翻譯提供，這裡不重複全文。" in (
                translation_content
            ):
                errors.append("自足詳細回覆不得用快速翻譯銜接句取代完整翻譯。")
        extension_section = _section(
            "### 4. 句型延伸與仿寫造句",
            None,
        )
        if not (
            re.search(r"[A-Za-z]", extension_section)
            and re.search(r"[\u3400-\u9fff]", extension_section)
        ):
            errors.append("句型延伸範例必須同時包含英文與繁體中文翻譯。")

    # TYPE 1 must offer materially different alternatives, and TYPE 3 must
    # continue with teaching rather than copy the confirmed first reply.
    # TYPE 2 intentionally repeats the concise meaning/K.K. anchors required
    # by its vocabulary-teaching template, so it is exempt from this exact
    # delivered-output comparison.
    if (
        contract == "translator_mastery_after_fast"
        and input_type != "english_term"
    ):
        normalized_fast = re.sub(
            r"[\W_]+",
            "",
            str(fast_output or "").lower(),
        )
        repeated_fast_output = False
        if normalized_fast and input_type == "chinese":
            repeated_fast_output = normalized_fast in normalized_expressions
        elif normalized_fast:
            normalized_response = re.sub(
                r"[\W_]+",
                "",
                response.lower(),
            )
            if (
                re.search(r"[\u3400-\u9fff]", str(fast_output or ""))
                or len(normalized_fast) >= 4
            ):
                repeated_fast_output = normalized_fast in normalized_response
            else:
                repeated_fast_output = any(
                    re.sub(r"[\W_]+", "", line.lower())
                    == normalized_fast
                    for line in response.splitlines()
                )
        if repeated_fast_output:
            errors.append("詳細回覆不得在任何位置原樣重貼快速翻譯全文。")
    return errors


def build_translator_detail_repair_prompt(errors: Any) -> str:
    """Build the trusted one-shot correction nudge for an invalid detail draft."""
    safe_errors = [
        str(error).strip()
        for error in (errors if isinstance(errors, list) else [])
        if str(error).strip()
    ]
    serialized = json.dumps(safe_errors, ensure_ascii=False)
    return (
        "[Trusted Translator response validation: The previous draft is not "
        "deliverable. Rewrite the entire detailed teaching response now. "
        "Return only the replacement response, use Traditional Chinese, follow "
        "the already supplied Translator output contract exactly, and do not "
        "call tools or discuss this validation message. Correct these "
        f"program-detected violations: {serialized}]"
    )


def normalize_response_profile(value: Any) -> Optional[Dict[str, Any]]:
    """Return a safe response profile, or ``None`` for invalid configuration."""
    if not isinstance(value, dict):
        return None
    if isinstance(value, NormalizedResponseProfile):
        return NormalizedResponseProfile(value)

    strategy = str(value.get("strategy") or "default").strip().lower()
    detail_lane_raw = value.get("detail_lane")
    if detail_lane_raw is not None and not isinstance(detail_lane_raw, dict):
        return None
    detail_lane = detail_lane_raw if isinstance(detail_lane_raw, dict) else {}
    legacy_skill = str(detail_lane.get("skill") or "").strip() or None
    legacy_contract = (
        str(detail_lane.get("output_contract") or "").strip().lower()
    )

    def _detail_route(
        key: str,
        fallback_skill: Optional[str],
        fallback_contract: str = "",
        *,
        allow_output_contract: bool = True,
    ) -> Optional[Dict[str, Any]]:
        raw_route = detail_lane.get(key)
        if key in detail_lane and not isinstance(raw_route, dict):
            return None
        route = raw_route if isinstance(raw_route, dict) else {}
        skill = str(route.get("skill") or "").strip() or fallback_skill
        output_contract = (
            str(route.get("output_contract") or "").strip().lower()
            or fallback_contract
        )
        if output_contract and not allow_output_contract:
            return None
        if (
            output_contract
            and output_contract not in _DETAIL_OUTPUT_CONTRACT_PROMPTS
        ):
            return None
        return {
            "model": "default",
            "skill": skill,
            "output_contract": output_contract or None,
        }

    if strategy == "default":
        # Default profiles never execute an outcome detail route, so accepting
        # route-specific configuration here would silently ignore an operator
        # typo. A default profile intentionally uses the ordinary model and
        # prompt path without a response-profile detail override.
        if "fast_lane" in value or legacy_skill or legacy_contract or any(
            key in detail_lane
            for key in (
                "on_fast_success",
                "on_fast_ambiguous",
                "on_fast_failure",
            )
        ):
            return None
        return NormalizedResponseProfile({
            "strategy": "default",
            "detail_lane": {
                "model": "default",
                "skill": None,
            },
        })
    if strategy != "fast_then_default":
        return None

    fast_lane_raw = value.get("fast_lane")
    if not isinstance(fast_lane_raw, dict):
        return None
    handler_name = str(fast_lane_raw.get("handler") or "").strip().lower()
    handler = _FAST_LANE_HANDLERS.get(handler_name)
    if handler is None:
        return None
    fast_lane = handler["normalize"](fast_lane_raw)
    if fast_lane is None:
        return None
    fast_lane["handler"] = handler_name
    success_route = _detail_route(
        "on_fast_success",
        legacy_skill,
        legacy_contract,
    )
    if success_route is None:
        return None
    if success_route["output_contract"] in {
        "translator_mastery",
        "translator_mastery_self_contained",
    }:
        success_route["output_contract"] = "translator_mastery_after_fast"
    ambiguous_fallback_contract = str(
        success_route["output_contract"] or "",
    )
    if (
        "on_fast_ambiguous" not in detail_lane
        and ambiguous_fallback_contract in {
            "translator_mastery_after_fast",
            "vocabulary_full",
        }
    ):
        ambiguous_fallback_contract = "translator_mastery_self_contained"
    ambiguous_route = _detail_route(
        "on_fast_ambiguous",
        success_route["skill"],
        ambiguous_fallback_contract,
    )
    if (
        ambiguous_route is not None
        and ambiguous_route["output_contract"] in {
            "translator_mastery",
            "translator_mastery_after_fast",
            "vocabulary_full",
        }
    ):
        ambiguous_route["output_contract"] = (
            "translator_mastery_self_contained"
        )
    failure_route = _detail_route(
        "on_fast_failure",
        legacy_skill,
    )
    if failure_route is not None:
        failure_contract = failure_route["output_contract"]
        if failure_contract == "translator_mastery":
            failure_route["output_contract"] = (
                "translator_mastery_self_contained"
            )
        elif failure_contract not in {
            None,
            "translator_mastery_self_contained",
        }:
            # A failed fast lane cannot satisfy contracts that assume a
            # confirmed first reply (or other success-only handoff formats).
            return None
    if ambiguous_route is None or failure_route is None:
        return None

    return NormalizedResponseProfile({
        "strategy": "fast_then_default",
        "fast_lane": fast_lane,
        "detail_lane": {
            "on_fast_success": success_route,
            "on_fast_ambiguous": ambiguous_route,
            "on_fast_failure": failure_route,
        },
    })


def resolve_response_profile(
    profiles: Any,
    profile_name: Any,
    *,
    explicitly_bound: bool = False,
) -> Optional[Dict[str, Any]]:
    """Resolve one named operator profile without accepting inline user data."""
    if profile_name is None and not explicitly_bound:
        return None
    if not isinstance(profile_name, str):
        return {
            "strategy": "configuration_error",
            "error": "response_profile name must be a string",
        }
    name = profile_name.strip()
    if not name:
        return {
            "strategy": "configuration_error",
            "name": name,
            "error": "response_profile name is empty",
        }
    raw_profile = profiles.get(name) if isinstance(profiles, dict) else None
    profile = normalize_response_profile(raw_profile)
    if profile is None:
        return {
            "strategy": "configuration_error",
            "name": name,
            "error": "response_profile is missing or invalid",
        }
    profile["name"] = name
    return profile


def legacy_fast_translation_profile(value: Any) -> Optional[Dict[str, Any]]:
    """Adapt the former inline translation setting during config migration."""
    if not isinstance(value, dict):
        return None
    return normalize_response_profile(
        {
            "strategy": "fast_then_default",
            "fast_lane": {
                "handler": "translation",
                **value,
            },
            "detail_lane": {"model": "default"},
        },
    )


def _handler_for_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    fast_lane = profile["fast_lane"]
    return _FAST_LANE_HANDLERS[fast_lane["handler"]]


def eligible_fast_lane_text(
    profile: Dict[str, Any],
    text: Any,
    *,
    is_command: bool = False,
    has_media: bool = False,
) -> Optional[str]:
    handler = _handler_for_profile(profile)
    return handler["eligible"](
        text,
        profile["fast_lane"],
        is_command=is_command,
        has_media=has_media,
    )


def prepare_fast_lane(
    profile: Dict[str, Any],
    text: str,
) -> tuple[FastLaneRunner, Dict[str, Any], str]:
    """Return the handler runner, normalized lane config, and failure prompt."""
    handler = _handler_for_profile(profile)
    config = profile["fast_lane"]
    _, target_language = handler["prepare"](text)
    failed_prompt = handler["failed_prompt"](
        target_language=target_language,
        instructions=str(config["instructions"]),
    )
    return handler["run"], config, failed_prompt


def normalized_fast_lane_source(
    profile: Dict[str, Any],
    text: str,
) -> str:
    """Return the directive-free source text used by the fast-lane handler."""
    source_text, _ = _handler_for_profile(profile)["prepare"](text)
    return str(source_text or "").strip()


def clean_fast_lane_output(
    profile: Dict[str, Any],
    value: Any,
) -> Optional[str]:
    return _handler_for_profile(profile)["clean"](value)


def format_fast_lane_output(profile: Dict[str, Any], value: str) -> str:
    return _handler_for_profile(profile)["format"](value)


def detail_lane_prompt(
    profile: Dict[str, Any],
    *,
    delivery_ambiguous: bool = False,
    delivery_failed: bool = False,
) -> str:
    return _handler_for_profile(profile)["detail_prompt"](
        delivery_ambiguous=delivery_ambiguous,
        delivery_failed=delivery_failed,
    )


def _detail_lane_route(
    profile: Dict[str, Any],
    delivery_status: Optional[str],
) -> Optional[Dict[str, Any]]:
    detail_lane = profile.get("detail_lane")
    if not isinstance(detail_lane, dict):
        return None
    if delivery_status == "delivered":
        route_name = "on_fast_success"
    elif delivery_status in {"ambiguous", "delivery_failed"}:
        route_name = "on_fast_ambiguous"
    else:
        route_name = "on_fast_failure"
    route = detail_lane.get(route_name)
    return route if isinstance(route, dict) else None


def detail_lane_skill(
    profile: Dict[str, Any],
    delivery_status: Optional[str],
) -> Optional[str]:
    """Select the ephemeral detail skill for the observed fast-lane result."""
    route = _detail_lane_route(profile, delivery_status)
    if route is None:
        return None
    skill = route.get("skill")
    return skill if isinstance(skill, str) and skill else None


def detail_lane_contract_prompt(
    profile: Dict[str, Any],
    delivery_status: Optional[str],
) -> Optional[str]:
    """Return the trusted mandatory output contract for the selected route."""
    route = _detail_lane_route(profile, delivery_status)
    if route is None:
        return None
    contract_name = route.get("output_contract")
    if not isinstance(contract_name, str) or not contract_name:
        return None
    return _DETAIL_OUTPUT_CONTRACT_PROMPTS.get(contract_name)


def detail_lane_output_contract(
    profile: Dict[str, Any],
    delivery_status: Optional[str],
) -> Optional[str]:
    """Return the normalized output-contract name for the selected route."""
    route = _detail_lane_route(profile, delivery_status)
    if route is None:
        return None
    contract_name = route.get("output_contract")
    return (
        contract_name
        if isinstance(contract_name, str) and contract_name
        else None
    )
