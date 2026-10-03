import hashlib
from pathlib import Path

import pytest
from PIL import Image

from tools import deterministic_image_render_broker as broker
from tools.deterministic_image_render_broker import (
    LEGACY_AUDIO_BRIEF_SHA256,
    _rebuild_legacy_footer,
    handle,
)


def test_renders_disclosure_above_reserved_footer(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    root = tmp_path / "media" / "tool-image-generation"
    root.mkdir(parents=True)
    source = root / "candidate.png"
    Image.new("RGB", (1000, 1000), "#91a6b8").save(source)
    result = handle(
        {
            "args": {
                "source_image": str(source),
                "output_filename": "Audio Brief.png",
                "placement": "audio_brief_above_footer",
            },
            "scope": {
                "task_id": "t_example",
                "contract_fingerprint": "abc123",
                "allowed_asset_filenames": ["Page Hero.png", "Audio Brief.png"],
                "authorized_source_path": str(source),
                "authorized_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
        }
    )
    output = Path(result["path"])
    assert output.is_file()
    assert result["dimensions"] == "1000x1000"
    assert result["disclosure_bounds"][3] < result["footer_reserved_from_y"]
    assert root in output.parents


def test_rejects_filename_outside_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    root = tmp_path / "media" / "tool-image-generation"
    root.mkdir(parents=True)
    source = root / "candidate.png"
    Image.new("RGB", (600, 600), "white").save(source)
    try:
        handle(
            {
                "args": {
                    "source_image": str(source),
                    "output_filename": "other.png",
                    "placement": "audio_brief_above_footer",
                },
                "scope": {
                    "task_id": "t_example",
                    "contract_fingerprint": "abc123",
                    "allowed_asset_filenames": ["Audio Brief.png"],
                    "authorized_source_path": str(source),
                    "authorized_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
            }
        )
    except ValueError as exc:
        assert "contract-authorized" in str(exc)
    else:
        raise AssertionError("unauthorized output filename was accepted")


def test_rebuilds_authorized_legacy_footer_and_removes_baked_in_label():
    image = Image.new("RGBA", (1254, 1254), "#91a6b8")
    footer_sample = (1200, 1235)
    image.putpixel(footer_sample, (230, 174, 43, 255))

    assert _rebuild_legacy_footer(image, LEGACY_AUDIO_BRIEF_SHA256) is True

    assert image.getpixel(footer_sample) == (2, 27, 50, 255)
    assert image.getpixel((20, 1161)) == (230, 174, 43, 255)


def test_does_not_rebuild_footer_for_other_sources():
    image = Image.new("RGBA", (1254, 1254), "#91a6b8")

    assert _rebuild_legacy_footer(image, "0" * 64) is False
    assert image.getpixel((885, 1192)) == (145, 166, 184, 255)


def test_rejects_unapproved_footer_font(monkeypatch):
    image = Image.new("RGBA", (1254, 1254), "#91a6b8")
    monkeypatch.setattr(broker, "PINGFANG_SHA256", "0" * 64)

    with pytest.raises(ValueError, match="font digest is not approved"):
        _rebuild_legacy_footer(image, LEGACY_AUDIO_BRIEF_SHA256)


def _payload(source: Path, *, authorized_path: Path, authorized_sha256: str):
    return {
        "args": {
            "source_image": str(source),
            "output_filename": "Audio Brief.png",
            "placement": "audio_brief_above_footer",
        },
        "scope": {
            "task_id": "t_authenticated",
            "contract_fingerprint": "authenticated-v1",
            "allowed_asset_filenames": ["Audio Brief.png"],
            "authorized_source_path": str(authorized_path),
            "authorized_source_sha256": authorized_sha256,
        },
    }


def test_rejects_mismatched_authorized_source_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    root = tmp_path / "media" / "tool-image-generation"
    root.mkdir(parents=True)
    source = root / "candidate.png"
    other = root / "other.png"
    Image.new("RGB", (600, 600), "white").save(source)
    Image.new("RGB", (600, 600), "black").save(other)

    with pytest.raises(ValueError, match="not authorized"):
        handle(
            _payload(
                source,
                authorized_path=other,
                authorized_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        )


def test_rejects_source_changed_after_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    root = tmp_path / "media" / "tool-image-generation"
    root.mkdir(parents=True)
    source = root / "candidate.png"
    Image.new("RGB", (600, 600), "white").save(source)
    receipt_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    Image.new("RGB", (600, 600), "black").save(source)

    with pytest.raises(ValueError, match="do not match"):
        handle(
            _payload(
                source,
                authorized_path=source,
                authorized_sha256=receipt_sha256,
            )
        )


def test_authenticated_legacy_repair_preserves_body_and_rebuilds_footer(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    root = tmp_path / "media" / "tool-image-generation"
    root.mkdir(parents=True)
    source = root / "legacy.png"
    image = Image.new("RGB", (1254, 1254), "#91a6b8")
    body_marker = (17, 29, 43)
    image.putpixel((40, 500), body_marker)
    image.save(source)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(broker, "LEGACY_AUDIO_BRIEF_SHA256", source_sha256)

    result = handle(
        _payload(
            source,
            authorized_path=source,
            authorized_sha256=source_sha256,
        )
    )

    rendered = Image.open(result["path"]).convert("RGB")
    assert result["legacy_footer_rebuilt"] is True
    assert result["disclosure_bounds"][3] < result["footer_reserved_from_y"]
    assert rendered.getpixel((40, 500)) == body_marker
    assert rendered.getpixel((20, 1161)) == (230, 174, 43)
    assert rendered.getpixel((1200, 1235)) == (2, 27, 50)
    footer = rendered.crop((0, 1164, 1254, 1254))
    assert sum(
        1 for pixel in footer.get_flattened_data() if pixel == (255, 255, 255)
    ) > 100


@pytest.mark.parametrize("height", [1000, 800])
def test_repairs_bound_candidate_footer_without_changing_content_area(tmp_path, monkeypatch, height):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "openclaw"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    root = tmp_path / "hermes" / "kanban" / "artifacts"
    root.mkdir(parents=True)
    source = root / "candidate.png"
    image = Image.new("RGB", (1000, height), "#91a6b8")
    image.putpixel((500, height - 50), (255, 0, 0))
    image.save(source)
    payload = {"args": {"source_image": str(source), "output_filename": "EP11_Best_Buy_audio_brief_1x1.png", "placement": "audio_brief_above_footer"}, "scope": {"task_id": "t_example", "contract_fingerprint": "abc123", "allowed_asset_filenames": ["EP11_Best_Buy_audio_brief_1x1.png"], "authorized_source_path": str(source), "authorized_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "authorized_source_kind": "controller_candidate", "authorized_footer_text": "6–10 min｜Fixed footer"}}
    if height != 1000:
        with pytest.raises(ValueError, match="exact square"):
            handle(payload)
        return
    result = handle(payload)
    output = Image.open(result["path"]).convert("RGB")
    assert result["footer_rebuilt"] is True
    assert output.size == image.size
    assert output.crop((0, 0, 1000, 780)).tobytes() == image.crop((0, 0, 1000, 780)).tobytes()
    assert output.getpixel((500, 950)) != (255, 0, 0)
    assert output.crop((0, 880, 90, 1000)).tobytes() == image.crop((0, 880, 90, 1000)).tobytes()
    assert result["disclosure_bounds"][3] < result["footer_reserved_from_y"]


def test_render_preserves_previous_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    root = tmp_path / "media" / "tool-image-generation"
    root.mkdir(parents=True)
    source = root / "source.png"
    Image.new("RGB", (600, 600), "white").save(source)
    first = handle(_payload(source, authorized_path=source,
                            authorized_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    Image.new("RGB", (600, 600), "black").save(source)
    second = handle(_payload(source, authorized_path=source,
                             authorized_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
    assert first["path"] != second["path"]
    assert hashlib.sha256(Path(first["path"]).read_bytes()).hexdigest() == first["sha256"]
