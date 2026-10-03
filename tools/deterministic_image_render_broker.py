"""Contract-bound deterministic image rendering for the OpenClaw bridge."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image, ImageDraw, ImageFont


DISCLOSURE = "AI-assisted visual"
PLACEMENT = "audio_brief_above_footer"
LEGACY_AUDIO_BRIEF_SHA256 = (
    "53419efacf7825bb539a4ae680eb14b87af9b9ef9d3fabc44f9e694a981220ab"
)
LEGACY_AUDIO_BRIEF_SIZE = (1254, 1254)
LEGACY_FOOTER_TEXT = "約 6–10 分鐘  │  用聽的，理解一個值得學習的一人公司"
PINGFANG_FONT = Path("/System/Library/Fonts/PingFang.ttc")
PINGFANG_SHA256 = "6464159410c45e73c2ef78041c8b5b17689cea1730a6e269056a8313ff410c84"


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return candidate != root
    except ValueError:
        return False


def _media_root() -> Path:
    state = Path(os.environ.get("OPENCLAW_STATE_DIR", "~/.openclaw")).expanduser()
    return (state / "media" / "tool-image-generation").resolve()


MAX_ENCODED_BYTES = 20 * 1024 * 1024


def _rebuild_legacy_footer(image: Image.Image, source_sha256: str) -> bool:
    """Replace the one authorized source's baked-in legacy disclosure footer."""
    if source_sha256 != LEGACY_AUDIO_BRIEF_SHA256:
        return False
    if image.size != LEGACY_AUDIO_BRIEF_SIZE:
        raise ValueError("Authorized legacy Audio Brief has unexpected dimensions.")
    try:
        font_sha256 = hashlib.sha256(PINGFANG_FONT.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(
            "Required deterministic Traditional Chinese font is unavailable."
        ) from exc
    if font_sha256 != PINGFANG_SHA256:
        raise ValueError("Deterministic Traditional Chinese font digest is not approved.")

    width, height = image.size
    footer_top = round(height * 0.925)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, footer_top, width, height), fill=(2, 27, 50, 255))
    draw.rectangle((0, footer_top, width, footer_top + 3), fill=(230, 174, 43, 255))
    font = ImageFont.truetype(str(PINGFANG_FONT), size=34)
    left, top, right, bottom = draw.textbbox((0, 0), LEGACY_FOOTER_TEXT, font=font)
    text_y = footer_top + ((height - footer_top) - (bottom - top)) // 2 - top
    draw.text((52, text_y), LEGACY_FOOTER_TEXT, font=font, fill=(255, 255, 255, 255))
    return True


def _rebuild_bound_footer(image: Image.Image, text: str) -> None:
    """Rebuild only the reserved footer using the broker's sealed policy text."""
    if not isinstance(text, str) or not text.strip() or len(text) > 160 or any(ord(c) < 32 for c in text):
        raise ValueError("Invalid contract-bound footer text.")
    if hashlib.sha256(PINGFANG_FONT.read_bytes()).hexdigest() != PINGFANG_SHA256:
        raise ValueError("Deterministic Traditional Chinese font digest is not approved.")
    width, height = image.size
    footer_top = round(height * 0.88)
    draw = ImageDraw.Draw(image, "RGBA")
    background = image.getpixel((0, height - 1))
    text_left = round(width * 0.09)  # Keep the existing headphone icon and its left margin.
    text_right = round(width * 0.98)
    draw.rectangle((text_left, footer_top, width, height), fill=background)
    size = max(16, round(width * 0.027))
    while size >= 12:
        font = ImageFont.truetype(str(PINGFANG_FONT), size=size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= (text_right - text_left) * 0.98 and bottom - top <= (height - footer_top) * 0.8:
            break
        size -= 1
    else:
        raise ValueError("Contract footer does not fit the reserved information strip.")
    luminance = sum(background[:3]) / 3
    ink = (20, 20, 20, 255) if luminance > 128 else (255, 255, 255, 255)
    draw.text((text_left + ((text_right - text_left) - (right - left)) / 2 - left, footer_top + ((height - footer_top) - (bottom - top)) / 2 - top), text, font=font, fill=ink)


def _render(source: BinaryIO, output: Path, *, source_sha256: str, footer_text: str | None = None) -> dict[str, Any]:
    with Image.open(source) as opened:
        width, height = opened.size
        if width < 512 or height < 512:
            raise ValueError("Source image must be at least 512x512.")
        if width > 4096 or height > 4096 or width * height > 16_000_000:
            raise ValueError("Source image exceeds the deterministic renderer limit.")
        image = opened.convert("RGBA")

    legacy_footer_rebuilt = False
    if footer_text is not None:
        if width != height:
            raise ValueError("Audio Brief footer repair requires an exact square image.")
        _rebuild_bound_footer(image, footer_text)
    else:
        legacy_footer_rebuilt = _rebuild_legacy_footer(image, source_sha256)
    draw = ImageDraw.Draw(image, "RGBA")
    font_size = max(16, round(min(width, height) * 0.022))
    # Pillow 12.2 is pinned by Hermes. Its bundled default font avoids mutable
    # host font discovery while honoring the requested point size.
    font = ImageFont.load_default(size=font_size)
    left, top, right, bottom = draw.textbbox((0, 0), DISCLOSURE, font=font)
    text_width, text_height = right - left, bottom - top
    pad_x = max(14, round(width * 0.016))
    pad_y = max(8, round(height * 0.009))
    panel_width = text_width + pad_x * 2
    panel_height = text_height + pad_y * 2
    panel_left = max(0, (width - panel_width) // 2)
    # The bottom 12% is reserved for the Audio Brief footer. Keep a further 3%
    # gap so the disclosure is visibly separate from the footer.
    panel_bottom = round(height * 0.85)
    panel_top = panel_bottom - panel_height
    radius = max(8, round(panel_height * 0.22))
    draw.rounded_rectangle(
        (panel_left, panel_top, panel_left + panel_width, panel_bottom),
        radius=radius,
        fill=(255, 255, 255, 232),
        outline=(30, 30, 30, 210),
        width=max(1, round(width * 0.0015)),
    )
    draw.text(
        (panel_left + pad_x, panel_top + pad_y - top),
        DISCLOSURE,
        font=font,
        fill=(20, 20, 20, 255),
    )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_fd = os.open(
        output.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    temporary_name = f".{output.name}.{os.getpid()}.tmp"
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temporary_fd, "wb") as temporary:
            image.save(temporary, format="PNG", optimize=True)
        os.replace(
            temporary_name,
            output.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        rendered_fd = os.open(
            output.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        with os.fdopen(rendered_fd, "rb") as rendered:
            digest = hashlib.file_digest(rendered, "sha256").hexdigest()
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return {
        "success": True,
        "path": str(output),
        "sha256": digest,
        "dimensions": f"{width}x{height}",
        "placement": PLACEMENT,
        "disclosure": DISCLOSURE,
        "footer_reserved_from_y": round(height * 0.88),
        "disclosure_bounds": [panel_left, panel_top, panel_left + panel_width, panel_bottom],
        "legacy_footer_rebuilt": legacy_footer_rebuilt,
        "footer_rebuilt": footer_text is not None,
        "footer_text_sha256": hashlib.sha256(footer_text.encode()).hexdigest() if footer_text is not None else None,
    }


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    if args.get("placement") != PLACEMENT:
        raise ValueError("Only audio_brief_above_footer placement is supported.")
    source = Path(str(args.get("source_image") or "")).expanduser().resolve()
    output_filename = str(args.get("output_filename") or "").strip()
    allowed = scope.get("allowed_asset_filenames")
    if (
        not isinstance(allowed, list)
        or output_filename not in allowed
        or Path(output_filename).name != output_filename
        or not re.search(r"(?:^|[-_ ])audio[-_ ]brief(?:[-_ ]1x1)?\.png$", output_filename, re.I)
    ):
        raise ValueError("Output filename is not the contract-authorized Audio Brief asset.")
    root = _media_root()
    footer_text = scope.get("authorized_footer_text") if scope.get("authorized_source_kind") == "controller_candidate" else None
    if scope.get("authorized_source_kind") == "controller_candidate" and (not isinstance(footer_text, str) or not footer_text.strip()):
        raise ValueError("Candidate footer repair requires sealed footer text.")
    artifact_root = (Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "kanban" / "artifacts").resolve()
    candidate_source = scope.get("authorized_source_kind") == "controller_candidate" and _inside(artifact_root, source)
    if (not _inside(root, source) and not candidate_source) or source.suffix.lower() != ".png" or not source.is_file():
        raise ValueError("Source must be an existing generated PNG inside OpenClaw media.")
    task_id = str(scope.get("task_id") or "").strip()
    contract_fingerprint = str(scope.get("contract_fingerprint") or "").strip()
    authorized_source = Path(str(scope.get("authorized_source_path") or "")).expanduser().resolve()
    authorized_sha256 = str(scope.get("authorized_source_sha256") or "").strip().lower()
    if not task_id or not contract_fingerprint:
        raise ValueError("Renderer scope requires a task and contract identity.")
    if source != authorized_source or len(authorized_sha256) != 64:
        raise ValueError("Source is not authorized for this contract session.")
    binding = hashlib.sha256(
        json.dumps([task_id, contract_fingerprint, authorized_sha256, footer_text],
                   ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    output_dir = (root / "deterministic-render" / binding).resolve()
    if not _inside(root, output_dir):
        raise ValueError("Resolved output escaped the generated-media root.")
    output = output_dir / output_filename
    with source.open("rb") as source_stream:
        encoded_size = os.fstat(source_stream.fileno()).st_size
        if encoded_size > MAX_ENCODED_BYTES:
            raise ValueError("Encoded source image exceeds the renderer size limit.")
        source_bytes = source_stream.read(MAX_ENCODED_BYTES + 1)
        if len(source_bytes) != encoded_size:
            raise ValueError("Source image changed while it was being authenticated.")
        if hashlib.sha256(source_bytes).hexdigest() != authorized_sha256:
            raise ValueError("Source bytes do not match the authorized task receipt.")
        return _render(
            io.BytesIO(source_bytes),
            output,
            source_sha256=authorized_sha256,
            footer_text=footer_text,
        )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Broker payload must be an object.")
        print(json.dumps(handle(payload), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
