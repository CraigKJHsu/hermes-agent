from __future__ import annotations

from proactive.ai_bizweek_asset_policy import (
    ai_bizweek_asset_guidance,
    infer_requested_asset_families,
)


def test_infers_audio_brief_without_page_hero_mix():
    families = infer_requested_asset_families(
        "請 review AI BizWeek Audio Brief 1:1 podcast 封面圖"
    )

    assert families == ["audio_brief"]


def test_infers_page_hero_from_facebook_business_image():
    families = infer_requested_asset_families(
        "請生成 Facebook Page 貼文用橫式商業洞察主圖，16:9"
    )

    assert families == ["page_hero"]


def test_page_hero_guidance_uses_horizontal_business_insight_spec():
    guidance = ai_bizweek_asset_guidance(
        [{"policy_id": "ai-bizweek-page-hero"}],
        task_body="Facebook Page 貼文用橫式商業洞察主圖",
    )

    assert guidance is not None
    page_hero = guidance["asset_families"]["page_hero"]
    assert "16:9 horizontal" in page_hero["output_spec"]["aspect_ratio"]
    assert "core solo-business insight" in page_hero["required_variables"]


def test_audio_brief_social_post_main_image_does_not_infer_page_hero():
    families = infer_requested_asset_families(
        "AI BizWeek Audio Brief 封面，Facebook、Podcast 單集宣傳與社群貼文主圖"
    )

    assert families == ["audio_brief"]


def test_asset_guidance_filters_to_requested_family():
    guidance = ai_bizweek_asset_guidance(
        [
            {"policy_id": "ai-bizweek-page-hero"},
            {"policy_id": "ai-bizweek-audio-brief-cover"},
            {"policy_id": "ai-bizweek-asset-production"},
        ],
        task_body="本任務只處理 Audio Brief 單集封面",
    )

    assert guidance is not None
    assert guidance["requested_asset_families"] == ["audio_brief"]
    assert list(guidance["asset_families"]) == ["audio_brief"]
    assert "at least three ai_leverage items" in guidance["asset_families"][
        "audio_brief"
    ]["required_variables"]


def test_content_guidance_preserves_page_order_and_source_body():
    from proactive.ai_bizweek_asset_policy import ai_bizweek_content_guidance

    guidance = ai_bizweek_content_guidance(
        [{"policy_id": "ai-bizweek-facebook-page-source-fidelity"}]
    )

    assert guidance is not None
    fidelity = guidance["facebook_page_source_fidelity"]
    assert "preserved in full" in fidelity["default_rule"]
    assert "hashtags as the final paragraph" in fidelity["required_page_order"]
    assert "confirmation that hashtags are the final paragraph" in fidelity[
        "review_evidence_required"
    ]
