"""Compose ordered local footage with the immutable audio delivery and timed subtitles."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .audio import audio_current
from .media import font
from .processing import ffmpeg, probe
from .project import Project
from .storage import lock, read_json, sha256, write_json
from .video_glow import glow_image, glow_opacity, glow_track, speaking_intervals

SUBTITLE_COLORS = {"p1": "#70BAFF", "p2": "#FFB266"}
VIDEO_PIPELINE_VERSION = 4


class OverlayEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    x: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    y: float = Field(default=0.22, ge=0, le=1, allow_inf_nan=False)
    width: float = Field(default=0.4, ge=0.02, le=1, allow_inf_nan=False)
    opacity: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    flip: bool = False
    start: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    end: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    speaker_slot: Literal["", "p1", "p2"] = ""
    speaking_glow: bool = True
    glow_strength: float = Field(default=0.85, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("贴图结束时间必须晚于开始时间")
        return self


class CoverOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    background: Literal["frame", "image"] = "frame"
    frame_second: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=200)
    font_size: int = Field(default=88, ge=24, le=160)
    title_y: float = Field(default=0.12, ge=0, le=0.85, allow_inf_nan=False)
    color: str = Field(default="#FFFFFF", pattern=r"^#[a-fA-F0-9]{6}$")
    shade: float = Field(default=0.25, ge=0, le=0.85, allow_inf_nan=False)
    show_overlays: bool = True


class VideoOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = Field(default=1080, ge=320, le=3840, multiple_of=2)
    height: int = Field(default=1920, ge=240, le=3840, multiple_of=2)
    fps: int = Field(default=30, ge=1, le=60)
    burn_subtitles: bool = True
    font_size: int = Field(default=48, ge=16, le=120)
    image_seconds: float = Field(default=5, ge=0.5, le=60, allow_inf_nan=False)


class VideoEdit(VideoOptions):
    asset_ids: list[str] = Field(default_factory=list, max_length=100)
    overlays: list[OverlayEdit] | None = Field(default=None, max_length=12)
    cover: CoverOptions | None = None


class VideoPaused(RuntimeError):
    pass


def video_config(project: Project) -> dict:
    config = read_json(project.root / "video.json", {})
    return {
        "options": VideoOptions.model_validate(config.get("options", {})).model_dump(),
        "assets": config.get("assets", []),
        "overlays": [
            {
                **layer,
                **OverlayEdit.model_validate(
                    {key: value for key, value in layer.items() if key in OverlayEdit.model_fields}
                ).model_dump(),
            }
            for layer in config.get("overlays", [])
        ],
        "cover": {
            "options": CoverOptions.model_validate(
                config.get("cover", {}).get("options", {})
            ).model_dump(),
            "image": config.get("cover", {}).get("image"),
        },
    }


def asset_path(project: Project, asset: dict) -> Path:
    path = (project.root / asset["path"]).resolve()
    if not path.is_relative_to((project.root / "assets").resolve()) or not path.is_file():
        raise ValueError("视频素材缺失，请重新上传")
    return path


def validate_asset(path: Path, suffix: str) -> dict:
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError) as exc:
            raise ValueError("图片素材无法读取") from exc
        with Image.open(path) as image:
            width, height = ImageOps.exif_transpose(image).size
            return {
                "kind": "image",
                "duration": None,
                "width": width,
                "height": height,
            }
    try:
        info = probe(path)
        streams = [s for s in info["streams"] if s["codec_type"] == "video"]
        duration = float(info["format"].get("duration", 0))
        if not streams or not 0 < duration < float("inf"):
            raise ValueError("视频没有可用画面或时长")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise ValueError("视频素材无法读取") from exc
    return {"kind": "video", "duration": duration}


def video_signature(project: Project, config: dict | None = None) -> str:
    config = config or video_config(project)
    audio = read_json(project.root / "exports/audio.json", {})
    data = {
        "pipeline": VIDEO_PIPELINE_VERSION
        if has_speaking_glow(config)
        else 3
        if config["overlays"]
        else 2,
        "subtitle_colors": SUBTITLE_COLORS,
        "audio": audio.get("signature"),
        "audio_files": {k: v["sha256"] for k, v in audio.get("files", {}).items()},
        "options": config["options"],
        "assets": [(a["id"], sha256(asset_path(project, a))) for a in config["assets"]],
    }
    if config["overlays"]:
        data["overlays"] = [
            {**render_layer(a), "sha256": sha256(asset_path(project, a))}
            for a in config["overlays"]
        ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def has_speaking_glow(config: dict) -> bool:
    return any(
        layer.get("speaker_slot")
        and layer.get("speaking_glow", True)
        and layer.get("glow_strength", 0.85) > 0
        for layer in config["overlays"]
    )


def render_layer(layer: dict) -> dict:
    # Preserve the fingerprints of existing static stickers until glow is enabled.
    if (
        layer.get("speaker_slot")
        and layer.get("speaking_glow", True)
        and layer.get("glow_strength", 0.85) > 0
    ):
        return layer
    return {
        key: value
        for key, value in layer.items()
        if key not in {"speaker_slot", "speaking_glow", "glow_strength"}
    }


def video_timeline(project: Project) -> dict:
    if not audio_current(project):
        return {"lines": []}
    audio = read_json(project.root / "exports/audio.json", {})
    record = audio.get("files", {}).get("timeline.json")
    if not record:
        return {"lines": []}
    path = (project.root / "exports" / record["path"]).resolve()
    if not path.is_relative_to((project.root / "exports").resolve()):
        raise ValueError("音频时间线路径越界")
    return read_json(path, {"lines": []})


def video_current(project: Project) -> bool:
    meta = read_json(project.root / "exports/video.json", {})
    try:
        return bool(
            meta
            and audio_current(project)
            and meta["signature"] == video_signature(project)
            and all(
                sha256(project.root / "exports" / f["path"]) == f["sha256"]
                for f in meta["files"].values()
            )
        )
    except (OSError, ValueError, KeyError):
        return False


def edit_config(config: dict, body: VideoEdit) -> dict:
    """Resolve editor IDs against uploaded assets; never accept paths from a browser."""
    assets = {asset["id"]: asset for asset in config["assets"]}
    if len(body.asset_ids) != len(set(body.asset_ids)) or any(
        key not in assets for key in body.asset_ids
    ):
        raise ValueError("素材顺序包含重复或不存在的素材")
    config["assets"] = [assets[key] for key in body.asset_ids]
    config["options"] = body.model_dump(exclude={"asset_ids", "overlays", "cover"})
    if body.overlays is not None:
        layers = {asset["id"]: asset for asset in config["overlays"]}
        ids = [layer.id for layer in body.overlays]
        if len(ids) != len(set(ids)) or any(key not in layers for key in ids):
            raise ValueError("贴图列表包含重复或不存在的贴图")
        config["overlays"] = [{**layers[layer.id], **layer.model_dump()} for layer in body.overlays]
    if body.cover is not None:
        if body.cover.background == "image" and not config["cover"]["image"]:
            raise ValueError("请先上传封面底图")
        config["cover"]["options"] = body.cover.model_dump()
    return config


def overlay_image(project: Project, layer: dict, options: VideoOptions):
    with Image.open(asset_path(project, layer)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
    width = max(1, round(options.width * layer["width"]))
    height = max(1, round(width * image.height / image.width))
    if height > options.height:
        width = max(1, round(width * options.height / height))
        height = options.height
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    if layer.get("flip"):
        image = ImageOps.mirror(image)
    if layer["opacity"] != 1:
        image.putalpha(image.getchannel("A").point(lambda a: round(a * layer["opacity"])))
    x = min(round(layer["x"] * options.width), options.width - width)
    y = min(round(layer["y"] * options.height), options.height - height)
    return image, x, y


def background_frame(project: Project, config: dict, second: float) -> Image.Image:
    """Use the same clip durations, loop and center crop as the video compositor."""
    options = VideoOptions.model_validate(config["options"])
    assets = config["assets"]
    if not assets:
        return Image.new("RGB", (options.width, options.height), "#18263e")
    duration = read_json(project.root / "exports/audio.json", {}).get("duration", 86400)
    lengths = [
        min(duration, options.image_seconds if a["kind"] == "image" else a["duration"])
        for a in assets
    ]
    cursor = second % sum(lengths)
    chosen = assets[-1]
    for asset, length in zip(assets, lengths):
        chosen = asset
        if cursor < length:
            break
        cursor -= length
    path = asset_path(project, chosen)
    if chosen["kind"] == "image":
        with Image.open(path) as source:
            picture = ImageOps.exif_transpose(source).convert("RGB")
        return ImageOps.fit(picture, (options.width, options.height), Image.Resampling.LANCZOS)
    with tempfile.TemporaryDirectory(prefix="speakvideo-frame-") as directory:
        target = Path(directory) / "frame.png"
        ffmpeg(
            [
                "-ss",
                cursor,
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={options.width}:{options.height}:force_original_aspect_ratio=increase,"
                f"crop={options.width}:{options.height},setsar=1",
                target,
            ],
            timeout=30,
        )
        with Image.open(target) as source:
            return source.convert("RGB")


def cover_picture(project: Project, config: dict) -> Image.Image:
    options = VideoOptions.model_validate(config["options"])
    cover = CoverOptions.model_validate(config["cover"]["options"])
    if cover.background == "image":
        if not config["cover"]["image"]:
            raise ValueError("请先上传封面底图")
        with Image.open(asset_path(project, config["cover"]["image"])) as source:
            image = ImageOps.fit(
                ImageOps.exif_transpose(source).convert("RGB"),
                (options.width, options.height),
                Image.Resampling.LANCZOS,
            )
    else:
        image = background_frame(project, config, cover.frame_second)
    image = image.convert("RGBA")
    if cover.shade:
        image.alpha_composite(Image.new("RGBA", image.size, (0, 0, 0, round(255 * cover.shade))))
    if cover.show_overlays:
        timeline = video_timeline(project) if has_speaking_glow(config) else {"lines": []}
        for layer in config["overlays"]:
            if layer["start"] <= cover.frame_second and (
                layer["end"] is None or cover.frame_second < layer["end"]
            ):
                sticker, x, y = overlay_image(project, layer, options)
                amount = glow_opacity(cover.frame_second, speaking_intervals(timeline, layer))
                if amount > 0 and layer["glow_strength"] > 0:
                    glow, pad = glow_image(
                        sticker,
                        SUBTITLE_COLORS[layer["speaker_slot"]],
                        layer["glow_strength"] * amount,
                    )
                    image.alpha_composite(glow, (x - pad, y - pad))
                image.alpha_composite(sticker, (x, y))
    draw = ImageDraw.Draw(image)
    size = max(12, round(cover.font_size * options.width / 1080))
    margin = round(options.width * 0.07)
    y = round(options.height * cover.title_y)
    bottom_margin = round(options.height * 0.03)
    available = options.height - y - bottom_margin
    # Shrink long titles to keep all text inside the frame at every aspect ratio.
    while True:
        face, subface = font(size), font(max(5, round(size * 0.48)))
        title = subtitle_lines(draw, cover.title, face, options.width - 2 * margin)
        subtitle = subtitle_lines(draw, cover.subtitle, subface, options.width - 2 * margin)
        line_height, subheight = round(size * 1.35), round(size * 0.7)
        gap = round(size * 0.4) if title and subtitle else 0
        total = len(title) * line_height + len(subtitle) * subheight + gap
        if total <= available or size <= 6:
            break
        size -= 1
    y = min(y, max(0, options.height - total - bottom_margin))
    for lines, face, step in [(title, face, line_height), (subtitle, subface, subheight)]:
        for line in lines:
            box = draw.textbbox((0, 0), line, font=face)
            x = (options.width - draw.textlength(line, font=face)) / 2
            draw.text(
                (x, y - box[1]),
                line,
                font=face,
                fill=cover.color,
                stroke_width=max(1, round(size / 22)),
                stroke_fill=(0, 0, 0, 220),
            )
            y += step
        if lines is title:
            y += gap
    return image.convert("RGB")


def cover_signature(project: Project, config: dict | None = None) -> str:
    config = config or video_config(project)
    cover = config["cover"]
    data = {
        "version": 1,
        "size": [config["options"]["width"], config["options"]["height"]],
        "image_seconds": config["options"]["image_seconds"],
        "options": cover["options"],
        "image": sha256(asset_path(project, cover["image"])) if cover["image"] else None,
        "assets": [(a["id"], sha256(asset_path(project, a))) for a in config["assets"]],
        "overlays": [
            {**render_layer(a), "sha256": sha256(asset_path(project, a))}
            for a in config["overlays"]
        ],
        "audio_duration": read_json(project.root / "exports/audio.json", {}).get("duration"),
    }
    if has_speaking_glow(config):
        data["timeline"] = video_timeline(project)
        data["glow_version"] = 1
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def cover_current(project: Project) -> bool:
    try:
        meta = read_json(project.root / "exports/cover.json", {})
        return bool(
            meta
            and meta["signature"] == cover_signature(project)
            and all(
                sha256(project.root / "exports" / f["path"]) == f["sha256"]
                for f in meta["files"].values()
            )
        )
    except (OSError, ValueError, KeyError):
        return False


def render_cover(
    project: Project, cancelled=lambda: False, progress=lambda **kw: None, already_locked=False
) -> dict:
    with nullcontext() if already_locked else lock(project.root / ".project.lock"):
        if cancelled():
            raise VideoPaused("已暂停封面导出")
        config = video_config(project)
        signature = cover_signature(project, config)
        if cover_current(project):
            return read_json(project.root / "exports/cover.json")
        progress(stage="正在生成封面图片", kind="stage")
        image = cover_picture(project, config)
        folder = project.root / "exports/cover" / (signature[:20] + "-" + uuid.uuid4().hex[:8])
        folder.mkdir(parents=True)
        try:
            image.save(folder / "cover.png")
            image.save(folder / "cover.jpg", quality=95)
            if cancelled():
                raise VideoPaused("已暂停封面导出")
            meta = {
                "signature": signature,
                "files": {
                    name: {
                        "path": (folder / name).relative_to(project.root / "exports").as_posix(),
                        "sha256": sha256(folder / name),
                        "bytes": (folder / name).stat().st_size,
                    }
                    for name in ("cover.png", "cover.jpg")
                },
            }
            write_json(project.root / "exports/cover.json", meta)
            return meta
        except Exception:
            shutil.rmtree(folder)
            raise


def subtitle_lines(draw: ImageDraw.ImageDraw, text: str, face, width: int) -> list[str]:
    """Wrap at natural boundaries, keep punctuation attached, and balance short tails."""
    closing = set("，。！？；：、,.!?;:)]}）】》」』…％%")
    opening = set("([{（【《「『")
    breaks = set("，。！？；：、,.!?;: ")
    result = []
    for paragraph in text.splitlines():
        rest = paragraph.strip()
        while rest:
            end = 0
            while end < len(rest) and draw.textlength(rest[: end + 1], font=face) <= width:
                end += 1
            end = max(1, end)
            if end < len(rest):
                # Prefer the end of a clause when it is reasonably close to the right edge.
                choices = [
                    i
                    for i in range(1, end + 1)
                    if rest[i - 1] in breaks
                    and draw.textlength(rest[:i], font=face) >= width * 0.55
                ]
                end = choices[-1] if choices else end
                while end > 1 and (rest[end] in closing or rest[end - 1] in opening):
                    end -= 1
                # Keep ordinary Latin words together if the word fits on its own.
                if (
                    rest[end - 1].isascii()
                    and rest[end - 1].isalnum()
                    and rest[end].isascii()
                    and rest[end].isalnum()
                ):
                    word_start = end
                    while (
                        word_start
                        and rest[word_start - 1].isascii()
                        and rest[word_start - 1].isalnum()
                    ):
                        word_start -= 1
                    if word_start:
                        end = word_start
            result.append(rest[:end].strip())
            rest = rest[end:].lstrip()
    if len(result) >= 2 and draw.textlength(result[-1], font=face) < width * 0.45:
        combined = result[-2] + result[-1]
        choices = [
            i
            for i in range(1, len(combined))
            if combined[i] not in closing
            and combined[i - 1] not in opening
            and draw.textlength(combined[:i], font=face) <= width
            and draw.textlength(combined[i:], font=face) <= width
            and not (
                combined[i - 1].isascii()
                and combined[i - 1].isalnum()
                and combined[i].isascii()
                and combined[i].isalnum()
            )
        ]
        if choices:
            end = min(
                choices,
                key=lambda i: abs(
                    draw.textlength(combined[:i], font=face)
                    - draw.textlength(combined[i:], font=face)
                ),
            )
            result[-2:] = [combined[:end].strip(), combined[end:].strip()]
    return [line for line in result if line]


def subtitle_track(
    timeline: dict, work: Path, options: VideoOptions, duration: float, checkpoint
) -> Path:
    """A transparent subtitle movie works on FFmpeg installations without libass."""
    width = options.width
    size = min(options.font_size, max(16, round(min(width, options.height) * 0.08)))
    face = font(size)
    line_height = round(size * 1.5)
    padding = max(8, round(size * 0.45))
    side_margin = round(width * 0.08)
    text_width = width - 2 * (side_margin + padding)
    bottom_margin = round(options.height * (0.15 if options.height > width else 0.08))
    panel_height = line_height * 2 + padding * 2
    height = panel_height + bottom_margin + 4
    # Chunk long lines into readable pages inside each line's actual spoken interval.
    cues = []
    for row in timeline["lines"]:
        lines = subtitle_lines(
            ImageDraw.Draw(Image.new("RGB", (1, 1))), row["words"], face, text_width
        )
        pages = [lines[i : i + 2] for i in range(0, len(lines), 2)]
        weights = [sum(len(line) for line in page) for page in pages]
        cursor = 0
        for page, weight in zip(pages, weights):
            start = row["start"] + row["duration"] * cursor / sum(weights)
            cursor += weight
            end = row["start"] + row["duration"] * cursor / sum(weights)
            cues.append((start, end, page, row["slot"]))
    blank = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    blank.save(work / "blank.png")
    entries, cursor = [], 0.0

    def entry(name, seconds):
        if seconds > 0:
            entries.extend([f"file '{name}'", f"duration {seconds:.9f}"])

    for i, (start, end, lines, slot) in enumerate(cues):
        checkpoint()
        entry("blank.png", start - cursor)
        image = blank.copy()
        draw = ImageDraw.Draw(image)
        color = SUBTITLE_COLORS.get(slot, "#FFFFFF")
        panel_bottom = height - bottom_margin
        panel_top = panel_bottom - panel_height
        draw.rounded_rectangle(
            (side_margin, panel_top, width - side_margin, panel_bottom),
            radius=round(size * 0.35),
            fill=(8, 13, 23, 205),
        )
        draw.rounded_rectangle(
            (
                side_margin,
                panel_top + padding,
                side_margin + max(3, size // 12),
                panel_bottom - padding,
            ),
            radius=2,
            fill=color,
        )
        y = panel_top + (panel_height - len(lines) * line_height) / 2
        for line in lines:
            box = draw.textbbox((0, 0), line, font=face)
            x = (width - draw.textlength(line, font=face)) / 2
            draw.text(
                (x, y + (line_height - (box[3] - box[1])) / 2 - box[1]),
                line,
                font=face,
                fill=color,
                stroke_width=max(1, size // 24),
                stroke_fill=(0, 0, 0, 220),
            )
            y += line_height
        name = f"subtitle-{i:05}.png"
        image.save(work / name)
        entry(name, end - start)
        cursor = end
    entry("blank.png", duration - cursor)
    entries.append("file 'blank.png'")
    (work / "subtitle-list.txt").write_text("\n".join(entries) + "\n", encoding="utf-8")
    path = work / "subtitles.mov"
    ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "subtitle-list.txt",
            "-vf",
            f"fps={options.fps},format=argb",
            "-t",
            duration,
            "-c:v",
            "qtrle",
            path,
        ],
        cwd=work,
    )
    return path


def render_video(project: Project, cancelled=lambda: False, progress=lambda **kw: None) -> dict:
    with lock(project.root / ".project.lock"):
        if not audio_current(project):
            raise ValueError("请先合并导出与当前工程一致的完整音频，再合成视频")
        config = video_config(project)
        options = VideoOptions.model_validate(config["options"])
        signature = video_signature(project, config)
        if video_current(project):
            if config["cover"]["options"]["enabled"]:
                render_cover(project, cancelled, progress, already_locked=True)
            return read_json(project.root / "exports/video.json")
        audio = read_json(project.root / "exports/audio.json")
        duration = audio["duration"]
        token = uuid.uuid4().hex
        work = project.root / "audio" / f"video-{token}"
        work.mkdir(parents=True)

        def checkpoint():
            if cancelled():
                raise VideoPaused("已暂停；旧视频仍可预览与下载")

        try:
            checkpoint()
            total = len(config["assets"])
            clips = []
            for i, asset in enumerate(config["assets"]):
                progress(
                    stage=f"正在处理素材 {i + 1}/{total}：{asset['name']}",
                    kind="assets",
                    completed=i,
                    total=total,
                )
                checkpoint()
                source = asset_path(project, asset)
                path = work / f"clip-{i:04}.mp4"
                inputs = (
                    ["-loop", "1", "-i", source] if asset["kind"] == "image" else ["-i", source]
                )
                seconds = min(
                    duration,
                    options.image_seconds if asset["kind"] == "image" else asset["duration"],
                )
                ffmpeg(
                    [
                        *inputs,
                        "-t",
                        seconds,
                        "-an",
                        "-vf",
                        f"scale={options.width}:{options.height}:force_original_aspect_ratio=increase,"
                        f"crop={options.width}:{options.height},setsar=1,fps={options.fps}",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-pix_fmt",
                        "yuv420p",
                        path,
                    ]
                )
                clips.append(f"file '{path.name}'")
            if clips:
                (work / "clips.txt").write_text("\n".join(clips) + "\n")
                progress(stage="正在按顺序拼接视频素材", kind="stage")
                checkpoint()
                ffmpeg(
                    ["-f", "concat", "-safe", "0", "-i", "clips.txt", "-c", "copy", "footage.mp4"],
                    cwd=work,
                )
                inputs = ["-stream_loop", "-1", "-i", work / "footage.mp4"]
            else:
                inputs = [
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=0x18263e:s={options.width}x{options.height}:r={options.fps}",
                ]
            master = project.root / "exports" / audio["files"]["master.wav"]["path"]
            subtitles = project.root / "exports" / audio["files"]["subtitles.srt"]["path"]
            timeline = read_json(project.root / "exports" / audio["files"]["timeline.json"]["path"])
            inputs += ["-i", master]
            filters, current_stream, input_index = [], "0:v", 2
            for i, layer in enumerate(config["overlays"]):
                checkpoint()
                sticker, x, y = overlay_image(project, layer, options)
                intervals = speaking_intervals(timeline, layer)
                if intervals and layer["glow_strength"] > 0:
                    progress(stage=f"正在生成发言高光：{layer['name']}", kind="stage")
                    glow, pad = glow_image(
                        sticker, SUBTITLE_COLORS[layer["speaker_slot"]], layer["glow_strength"]
                    )
                    track = glow_track(
                        glow, intervals, work, f"glow-{i:02}", options.fps, duration, checkpoint
                    )
                    inputs += ["-i", track]
                    output = f"glow{i}"
                    filters.append(
                        f"[{current_stream}][{input_index}:v]overlay={x - pad}:{y - pad}:"
                        f"eof_action=pass[{output}]"
                    )
                    current_stream = output
                    input_index += 1
                sticker_path = work / f"overlay-{i:02}.png"
                sticker.save(sticker_path)
                inputs += ["-loop", "1", "-i", sticker_path]
                enabled = f"gte(t,{layer['start']})"
                if layer["end"] is not None:
                    enabled += f"*lt(t,{layer['end']})"
                output = f"layer{i}"
                filters.append(
                    f"[{current_stream}][{input_index}:v]overlay={x}:{y}:"
                    f"enable='{enabled}':eof_action=repeat[{output}]"
                )
                current_stream = output
                input_index += 1
            if options.burn_subtitles:
                progress(stage="正在生成按台词时间线对齐的字幕图层", kind="stage")
                checkpoint()
                track = subtitle_track(timeline, work, options, duration, checkpoint)
                inputs += ["-i", track, "-i", subtitles]
                filters.append(
                    f"[{current_stream}][{input_index}:v]overlay=0:H-h:eof_action=pass[v]"
                )
                current_stream = "v"
                input_index += 1
            else:
                inputs += ["-i", subtitles]
            mapping = [
                *(
                    ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters)]
                    if filters
                    else []
                ),
                "-map",
                f"[{current_stream}]" if filters else "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                f"{input_index}:0",
            ]
            progress(stage="正在合成画面、完整音频与字幕", kind="stage")
            checkpoint()
            final = work / "final.mp4"
            ffmpeg(
                [
                    *inputs,
                    *mapping,
                    "-t",
                    duration,
                    "-r",
                    options.fps,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-c:s",
                    "mov_text",
                    "-metadata:s:s:0",
                    "language=zho",
                    "-movflags",
                    "+faststart",
                    final,
                ]
            )
            checkpoint()
            progress(stage="正在验证并保存成片", kind="stage")
            info = probe(final)
            if not {"video", "audio", "subtitle"}.issubset(
                {s["codec_type"] for s in info["streams"]}
            ):
                raise RuntimeError("成片缺少画面、音频或字幕")
            if abs(float(info["format"]["duration"]) - duration) > max(0.15, 2 / options.fps):
                raise RuntimeError("成片时长与音频不一致")
            shutil.copyfile(subtitles, work / "subtitles.srt")
            write_json(
                work / "video-inputs.json",
                {
                    **config,
                    "audio_signature": audio["signature"],
                    "subtitle_colors": SUBTITLE_COLORS,
                    "pipeline_version": VIDEO_PIPELINE_VERSION,
                },
            )
            bundle_name = signature[:20] + "-" + token[:8]
            files = {
                name: {
                    "path": f"video/{bundle_name}/{name}",
                    "sha256": sha256(work / name),
                    "bytes": (work / name).stat().st_size,
                }
                for name in ["final.mp4", "subtitles.srt", "video-inputs.json"]
            }
            meta = {
                "version": 1,
                "signature": signature,
                "duration": duration,
                "options": options.model_dump(),
                "mode": audio["mode"],
                "files": files,
            }
            for path in work.iterdir():
                if path.name not in files:
                    path.unlink()
            checkpoint()
            destination = project.root / "exports/video" / bundle_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            work.rename(destination)
            write_json(project.root / "exports/video.json", meta)
            state = read_json(project.root / "workflow.json", {"approvals": {}})
            state["stage"] = "video_review"
            write_json(project.root / "workflow.json", state)
            if config["cover"]["options"]["enabled"]:
                render_cover(project, cancelled, progress, already_locked=True)
            return meta
        finally:
            if work.exists():
                shutil.rmtree(work)
