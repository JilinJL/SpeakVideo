"""Speaker-driven alpha glow tracks, using the same clock as the audio delivery."""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

from .processing import ffmpeg

GLOW_FADE_SECONDS = 0.12


def speaking_intervals(timeline: dict, layer: dict) -> list[tuple[float, float]]:
    if not layer.get("speaker_slot") or not layer.get("speaking_glow", True):
        return []
    intervals = []
    for row in timeline.get("lines", []):
        if row["slot"] != layer["speaker_slot"]:
            continue
        start = max(row["start"], layer["start"])
        end = min(row["end"], layer["end"] if layer["end"] is not None else row["end"])
        if end > start:
            intervals.append((start, end))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def glow_opacity(second: float, intervals: list[tuple[float, float]]) -> float:
    for start, end in intervals:
        if start <= second < end:
            fade = min(GLOW_FADE_SECONDS, (end - start) / 2)
            return max(0, min(1, (second - start) / fade, (end - second) / fade))
    return 0


def glow_image(sticker: Image.Image, color: str, strength: float):
    """Keep the glow behind the silhouette, including partially transparent edges."""
    size = min(sticker.size)
    radius = max(2, min(24, round(size * 0.04)))
    pad = radius * 4
    mask = Image.new("L", (sticker.width + 2 * pad, sticker.height + 2 * pad))
    mask.paste(sticker.getchannel("A"), (pad, pad))
    outline = mask.filter(ImageFilter.MaxFilter(2 * max(1, radius // 2) + 1))
    soft = outline.filter(ImageFilter.GaussianBlur(radius))
    rim = outline.filter(ImageFilter.GaussianBlur(max(1, radius / 3)))
    alpha = ImageChops.subtract(ImageChops.lighter(soft, rim), mask)
    alpha = alpha.point(lambda value: round(value * strength))
    glow = Image.new("RGBA", mask.size, color)
    glow.putalpha(alpha)
    return glow, pad


def glow_track(
    image: Image.Image,
    intervals: list[tuple[float, float]],
    work: Path,
    prefix: str,
    fps: int,
    duration: float,
    checkpoint,
) -> Path:
    """Encode run-length alpha states once rather than evaluate expressions per pixel."""
    frames = math.ceil(duration * fps)
    runs = []
    cursor = 0
    for frame in range(frames):
        if frame % fps == 0:
            checkpoint()
        second = frame / fps
        while cursor < len(intervals) and second >= intervals[cursor][1]:
            cursor += 1
        amount = glow_opacity(second, intervals[cursor : cursor + 1])
        level = round(amount * 16)
        if runs and runs[-1][0] == level:
            runs[-1] = (level, runs[-1][1] + 1)
        else:
            runs.append((level, 1))
    entries = []
    for level in {level for level, _ in runs} | {0}:
        variant = image.copy()
        variant.putalpha(image.getchannel("A").point(lambda alpha: round(alpha * level / 16)))
        variant.save(work / f"{prefix}-{level:02}.png")
    for level, count in runs:
        entries.extend(
            [
                f"file '{prefix}-{level:02}.png'",
                f"option framerate {fps}",
                f"duration {count / fps:.9f}",
            ]
        )
    entries.extend([f"file '{prefix}-00.png'", f"option framerate {fps}"])
    listing = work / f"{prefix}.txt"
    listing.write_text("\n".join(entries) + "\n")
    path = work / f"{prefix}.mov"
    ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            listing.name,
            "-vf",
            f"fps={fps},format=argb",
            "-t",
            duration,
            "-c:v",
            "qtrle",
            path,
        ],
        cwd=work,
    )
    return path
