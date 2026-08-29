from pathlib import Path

from PIL import Image

from tools.add_ai_visual_disclosure import add_disclosure


def test_add_disclosure_preserves_dimensions_and_uses_bottom_left(tmp_path: Path):
    source = tmp_path / "source.png"
    destination = tmp_path / "final.png"
    Image.new("RGB", (1600, 900), "#fff8ea").save(source)

    result = add_disclosure(source, destination)

    with Image.open(destination) as image:
        assert image.size == (1600, 900)
        dark_pixels = [
            (x, y)
            for y in range(850, 900)
            for x in range(0, 400)
            if max(image.getpixel((x, y))[:3]) < 120
        ]
        assert dark_pixels
        assert not any(
            max(image.getpixel((x, y))[:3]) < 120
            for y in range(700, 900)
            for x in range(900, 1600)
        )
        assert image.getpixel((15, 875))[:3] == (255, 248, 234)
    assert result["dimensions"] == "1600x900"
    assert len(result["sha256"]) == 64
    assert result["placement"] == "bottom-left high-contrast panel"
