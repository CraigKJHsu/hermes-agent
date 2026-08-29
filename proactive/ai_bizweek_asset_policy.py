"""AI BizWeek visual asset policy helpers."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


AI_BIZWEEK_ASSET_POLICY_IDS = frozenset(
    {
        "ai-bizweek-page-hero",
        "ai-bizweek-audio-brief-cover",
        "ai-bizweek-asset-production",
    }
)
AI_BIZWEEK_PAGE_FIDELITY_POLICY_ID = "ai-bizweek-facebook-page-source-fidelity"

_FAMILY_ALIASES = {
    "page_hero": (
        "page_hero",
        "page hero",
        "facebook page hero",
        "facebook page 貼文",
        "facebook 主圖",
        "page 主圖",
        "hero 主圖",
        "資訊圖主圖",
        "橫式商業洞察主圖",
    ),
    "audio_brief": (
        "audio_brief",
        "audio brief",
        "podcast cover",
        "podcast 封面",
        "單集封面",
        "封面圖",
        "音訊封面",
        "節目封面",
    ),
}


ASSET_FAMILY_GUIDANCE: dict[str, dict[str, Any]] = {
    "page_hero": {
        "asset_family": "page_hero",
        "policy_id": "ai-bizweek-page-hero",
        "output_spec": {
            "aspect_ratio": "exact 16:9 horizontal Facebook Page business-insight image unless a task-pinned policy version explicitly says otherwise",
            "style": "cream-paper editorial business insight infographic with bold black type, saturated blue, bright orange brush marks, and hand-drawn annotations",
        },
        "required_variables": [
            "core solo-business insight",
            "business-model innovation",
            "business-model operating flow",
            "one to three emphasized keywords",
            "demand or signal source cards",
            "case facts available in task-scoped source",
            "risk reminder",
            "today action",
        ],
        "review_evidence_required": [
            "asset_family=page_hero",
            "actual image absolute path",
            "pixel dimensions and aspect-ratio check",
            "SHA-256",
            "AI disclosure",
            "policy receipt",
            "visual readback against the Page Hero checklist",
        ],
        "production_rule": (
            "Compile a Page Hero-only prompt from the complete policy and current task "
            "source. Do not borrow Audio Brief layout, EP box, podcast branding, or "
            "bottom podcast info bar."
        ),
    },
    "audio_brief": {
        "asset_family": "audio_brief",
        "policy_id": "ai-bizweek-audio-brief-cover",
        "output_spec": {
            "aspect_ratio": "1:1",
            "recommended_size": "1248x1248 or 2048x2048",
            "layout": "fixed eight-zone AI BizWeek Audio Brief cover",
        },
        "required_variables": [
            "episode_number",
            "country",
            "flag",
            "person_or_brand",
            "solo_business_type",
            "core_result_lead_in",
            "core_metric",
            "at least three ai_leverage items",
            "representative_scene",
            "summary_line_1",
            "summary_line_2",
        ],
        "review_evidence_required": [
            "asset_family=audio_brief",
            "actual image absolute path",
            "1:1 pixel dimensions check",
            "SHA-256",
            "AI disclosure",
            "policy receipt",
            "eight-zone visual readback",
            "Traditional Chinese text readability check",
        ],
        "production_rule": (
            "Keep content generation, scene generation, and final cover rendering separate. "
            "For the fixed brand text, EP box, flag, core metric, labels, summary lines, "
            "and bottom program bar, prefer deterministic HTML/Canvas/Pillow overlay after "
            "image generation instead of relying on the image model to typeset every word."
        ),
    },
}


def infer_requested_asset_families(text: str) -> list[str]:
    """Infer the AI BizWeek asset families named in task text."""
    normalized = str(text or "").casefold()
    found: list[str] = []
    for family, aliases in _FAMILY_ALIASES.items():
        if any(alias.casefold() in normalized for alias in aliases):
            found.append(family)
    if re.search(r"\b1\s*:\s*1\b|1248\s*[x×]\s*1248|2048\s*[x×]\s*2048", normalized):
        if "audio_brief" not in found:
            found.append("audio_brief")
    if re.search(
        r"facebook.+(?:資訊圖|橫式|商業洞察)|(?:資訊圖|橫式商業洞察).+facebook",
        normalized,
    ):
        if "page_hero" not in found:
            found.append("page_hero")
    return found


def ai_bizweek_asset_guidance(
    policies: Sequence[Mapping[str, Any]],
    *,
    task_body: str = "",
) -> dict[str, Any] | None:
    """Return deterministic guidance derived from loaded AI BizWeek policies."""
    policy_ids = {
        str(policy.get("policy_id") or "").strip()
        for policy in policies
        if isinstance(policy, Mapping)
    }
    available = sorted(policy_ids & AI_BIZWEEK_ASSET_POLICY_IDS)
    if not available:
        return None

    requested = infer_requested_asset_families(task_body)
    if not requested:
        requested = [
            family
            for family, guidance in ASSET_FAMILY_GUIDANCE.items()
            if guidance["policy_id"] in policy_ids
        ]
    families = {
        family: ASSET_FAMILY_GUIDANCE[family]
        for family in requested
        if family in ASSET_FAMILY_GUIDANCE
        and ASSET_FAMILY_GUIDANCE[family]["policy_id"] in policy_ids
    }
    return {
        "brand": "AI BizWeek",
        "available_policy_ids": available,
        "requested_asset_families": list(families),
        "asset_families": families,
        "global_rules": [
            "Topic memory, Mem0, prompt memory, or chat recollection are only indexes; they do not replace the complete loaded policies.",
            "Each image must declare exactly one asset_family.",
            "page_hero and audio_brief are separate asset families and must not share layouts.",
            "Review must inspect the actual generated image and file evidence, not only the prompt or worker summary.",
            "Fail closed when required variables, dimensions, SHA-256, AI disclosure, policy receipts, or visual checklist evidence are missing.",
        ],
    }


def ai_bizweek_content_guidance(
    policies: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Return deterministic non-image content guidance for AI BizWeek policies."""
    policy_ids = {
        str(policy.get("policy_id") or "").strip()
        for policy in policies
        if isinstance(policy, Mapping)
    }
    if AI_BIZWEEK_PAGE_FIDELITY_POLICY_ID not in policy_ids:
        return None
    return {
        "brand": "AI BizWeek",
        "facebook_page_source_fidelity": {
            "policy_id": AI_BIZWEEK_PAGE_FIDELITY_POLICY_ID,
            "default_rule": (
                "KJ-provided Facebook Page copy is the highest-priority source of "
                "truth and must be preserved in full unless KJ and Grace explicitly "
                "discussed and approved specific changes."
            ),
            "required_page_order": (
                "case body -> Page to Group CTA when needed -> case-customized "
                "hashtags as the final paragraph; nothing may follow the hashtags."
            ),
            "forbidden_without_explicit_discussion": [
                "summarize",
                "shorten",
                "rewrite",
                "remove paragraphs",
                "restructure",
                "replace with a template",
                "deliver a work summary instead of the full Page copy",
            ],
            "review_evidence_required": [
                "source-vs-output diff",
                "explicit change authorization when any Page copy changed",
                "confirmation that unmodified paragraphs were preserved",
                "confirmation that CTA or hashtags did not replace the Page body",
                "confirmation that hashtags are the final paragraph",
            ],
            "fail_closed_rule": (
                "Reject when Page copy was shortened or rewritten without explicit "
                "KJ/Grace discussion, or when the review cannot prove the source "
                "body was preserved."
            ),
        },
    }
