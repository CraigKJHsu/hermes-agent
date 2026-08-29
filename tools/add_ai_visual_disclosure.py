"""Add the AI-assisted visual disclosure without involving an image model."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


DEFAULT_TEXT = "AI-assisted visual"
FONT_PATHS = (
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/System/Library/Fonts/Helvetica.ttc"),
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def add_disclosure(source: Path, destination: Path, *, text: str = DEFAULT_TEXT) -> dict:
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    width, height = image.size
    font = _font(max(18, round(height * 0.022)))
    draw = ImageDraw.Draw(image)
    left = max(8, round(width * 0.008))
    bottom = max(8, round(height * 0.008))
    box = draw.textbbox((0, 0), text, font=font)
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]
    pad_x = max(8, round(width * 0.005))
    pad_y = max(5, round(height * 0.006))
    panel_width = max(text_width + pad_x * 2, round(width * 0.15))
    panel_height = text_height + pad_y * 2
    top = height - bottom - panel_height
    draw.rectangle(
        (left, top, left + panel_width, top + panel_height),
        fill=(255, 248, 234, 255),
        outline=(0, 23, 43, 255),
        width=max(1, round(width * 0.0012)),
    )
    draw.text(
        (left + pad_x, top + pad_y - box[1]),
        text,
        font=font,
        fill=(0, 23, 43, 255),
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.disclosure-tmp.png")
    image.save(temporary, format="PNG", optimize=True)
    temporary.replace(destination)
    payload = destination.read_bytes()
    return {
        "path": str(destination.resolve()),
        "dimensions": f"{width}x{height}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "disclosure": text,
        "placement": "bottom-left high-contrast panel",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(add_disclosure(args.source, args.destination), ensure_ascii=False))


if __name__ == "__main__":
    main()
