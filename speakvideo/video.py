"""Compose ordered local footage with the immutable audio delivery and timed subtitles."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from .audio import audio_current
from .media import font
from .processing import ffmpeg, probe
from .project import Project
from .storage import lock, read_json, sha256, write_json

SUBTITLE_COLORS = {"p1": "#70BAFF", "p2": "#FFB266"}
VIDEO_PIPELINE_VERSION = 2


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


class VideoPaused(RuntimeError):
    pass


def video_config(project: Project) -> dict:
    config = read_json(project.root / "video.json", {})
    return {
        "options": VideoOptions.model_validate(config.get("options", {})).model_dump(),
        "assets": config.get("assets", []),
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
        return {"kind": "image", "duration": None}
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
        "pipeline": VIDEO_PIPELINE_VERSION,
        "subtitle_colors": SUBTITLE_COLORS,
        "audio": audio.get("signature"),
        "audio_files": {k: v["sha256"] for k, v in audio.get("files", {}).items()},
        "options": config["options"],
        "assets": [(a["id"], sha256(asset_path(project, a))) for a in config["assets"]],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


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
            if options.burn_subtitles:
                progress(stage="正在生成按台词时间线对齐的字幕图层", kind="stage")
                checkpoint()
                track = subtitle_track(timeline, work, options, duration, checkpoint)
                inputs += ["-i", track, "-i", subtitles]
                mapping = [
                    "-filter_complex",
                    "[0:v][2:v]overlay=0:H-h:eof_action=pass[v]",
                    "-map",
                    "[v]",
                    "-map",
                    "1:a:0",
                    "-map",
                    "3:0",
                ]
            else:
                inputs += ["-i", subtitles]
                mapping = ["-map", "0:v:0", "-map", "1:a:0", "-map", "2:0"]
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
            return meta
        finally:
            if work.exists():
                shutil.rmtree(work)
