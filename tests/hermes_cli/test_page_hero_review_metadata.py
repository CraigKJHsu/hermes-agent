"""Preserve controller asset identity when equivalent wrappers are combined."""
import pytest
from hermes_cli.grace_review_metadata import page_hero_review_evidence

@pytest.mark.parametrize("conflict", [False, True])
def test_complementary_asset_identity(conflict):
    metadata = {
        "asset_family": "page_hero",
        "asset_declarations": {"page_hero": {"dimensions": "1600x900"}},
        "visual_review": {"all_required_text_readable": True, "text_occlusion_free": True,
                          "disclosure_non_obstructive": True, "defects_found": []},
        "evidence": {"asset_declarations": {"page_hero": {
            "width": 1600, "height": 900, "filename": "hero.png", "sha256": "a" * 64, "bytes": 123,
        }}},
    }
    if conflict:
        metadata["asset_declarations"]["page_hero"]["filename"] = "other.png"
        with pytest.raises(ValueError, match="conflicting filename"):
            page_hero_review_evidence(metadata)
    else:
        declaration = page_hero_review_evidence(metadata)["declaration"]
        assert (declaration["filename"], declaration["sha256"], declaration["bytes"]) == ("hero.png", "a" * 64, 123)
