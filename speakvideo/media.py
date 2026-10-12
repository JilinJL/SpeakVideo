from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .jobs import reusable
from .processing import ffmpeg, normalize, probe, srt_time
from .project import Project
from .storage import lock, read_json, sha256, write_json


def merge_audio(project: Project) -> tuple[list[dict], Path]:
    script, cast = project.script(), project.cast()
    manifest = read_json(project.root / "manifest.json")
    if not script.lines or not all(
        reusable(project, line, cast, manifest["lines"].get(line.id)) for line in script.lines
    ):
        raise ValueError("存在缺失、损坏或过期音频，请先合成/续跑")
    out = project.root / "exports"
    work = project.root / "audio"
    out.mkdir(parents=True, exist_ok=True)
    raw = work / "dialogue.raw.wav"
    # Decode each input through ffmpeg: valid PCM WAVs may have differing channel/sample widths.
    timeline, subs, cursor = [], [], 0
    rate = project.settings.audio.model_sample_rate
    role_paths = {slot: out / f"{slot}.wav" for slot in {line.slot for line in script.lines}}
    from contextlib import ExitStack

    with ExitStack() as stack:
        main = stack.enter_context(wave.open(str(raw), "wb"))
        speakers = {
            slot: stack.enter_context(wave.open(str(path), "wb"))
            for slot, path in role_paths.items()
        }
        for w in [main, *speakers.values()]:
            w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        for index, line in enumerate(script.lines, 1):
            src = work / "segments" / f"{line.id}.wav"
            cmd = [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(src),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                str(rate),
                "-",
            ]
            pcm = subprocess.run(cmd, capture_output=True, check=True).stdout
            duration = len(pcm) / (2 * rate)
            start, end = cursor / rate, cursor / rate + duration
            silence = b"\x00\x00" * round(line.pause_after_ms * rate / 1000)
            main.writeframes(pcm + silence)
            for slot, w in speakers.items():
                w.writeframes((pcm if slot == line.slot else bytes(len(pcm))) + silence)
            timeline.append(
                {
                    "id": line.id,
                    "slot": line.slot,
                    "words": line.words,
                    "start": start,
                    "end": end,
                    "duration": duration,
                    "pause": len(silence) / (rate * 2),
                }
            )
            subs.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{line.words}\n")
            cursor += len(pcm) // 2 + len(silence) // 2
    (out / "subtitles.srt").write_text("\n".join(subs), encoding="utf-8")
    write_json(out / "timeline.json", timeline)
    cfg = project.settings.audio
    measurements = normalize(
        raw,
        out / "narration.wav",
        cfg.master_sample_rate,
        cfg.narrative_lufs,
        cfg.narrative_true_peak,
    )
    write_json(out / "narration.loudness.json", measurements)
    return timeline, out / "narration.wav"


def font(size: int):
    for path in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def wrapped(draw: ImageDraw.ImageDraw, text: str, face, width: int) -> list[str]:
    lines, current = [], ""
    for c in text:
        if c == "\n" or draw.textlength(current + c, font=face) > width:
            lines.append(current)
            current = "" if c == "\n" else c
        else:
            current += c
    if current:
        lines.append(current)
    return lines


def render_signature(project: Project, broll=None, bgm=None, burn_subtitles=False) -> str:
    import hashlib

    script, cast = project.script(), project.cast()
    manifest = read_json(project.root / "manifest.json")
    data = {
        "script": script.model_dump(),
        "cast": {k: v.model_dump() for k, v in cast.items()},
        "audio": {
            line.id: manifest["lines"].get(line.id, {}).get("sha256") for line in script.lines
        },
        "settings": {
            "audio": project.settings.audio.model_dump(),
            "video": project.settings.video.model_dump(),
        },
        "broll": [(str(p.resolve()), sha256(p)) for p in (broll or [])],
        "bgm": (str(bgm.resolve()), sha256(bgm)) if bgm else None,
        "burn_subtitles": burn_subtitles,
    }
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def render_current(project: Project, broll=None, bgm=None, burn_subtitles=False) -> bool:
    meta = read_json(project.root / "exports" / "render.json", {})
    try:
        return meta.get("signature") == render_signature(
            project, broll, bgm, burn_subtitles
        ) and meta.get("sha256") == sha256(project.root / "exports" / "final.mp4")
    except OSError:
        return False


def render_video(
    project: Project,
    broll: list[Path] | None = None,
    bgm: Path | None = None,
    burn_subtitles: bool = False,
) -> Path:
    with lock(project.root / ".project.lock"):
        timeline, narration = merge_audio(project)
        out = narration.parent
        work = project.root / "audio" / "render"
        work.mkdir(parents=True, exist_ok=True)
        cfg = project.settings.video
        duration = sum(row["duration"] + row["pause"] for row in timeline)
        if bgm:
            bgm = bgm.expanduser().resolve()
            mix = work / "mix.wav"
            filt = (
                f"[0:a]asplit=2[n][side];[1:a]volume={cfg.bgm_volume}[music];"
                "[music][side]sidechaincompress=threshold=0.03:ratio=8:attack=15:release=350[duck];"
                "[n][duck]amix=inputs=2:duration=first:normalize=0[mix]"
            )
            ffmpeg(
                [
                    "-i",
                    narration,
                    "-stream_loop",
                    "-1",
                    "-i",
                    bgm,
                    "-filter_complex",
                    filt,
                    "-map",
                    "[mix]",
                    "-t",
                    duration,
                    "-ar",
                    48000,
                    "-c:a",
                    "pcm_s16le",
                    mix,
                ]
            )
        else:
            mix = narration
        sound = work / "final-audio.wav"
        measured = normalize(
            mix,
            sound,
            48000,
            project.settings.audio.final_lufs,
            project.settings.audio.final_true_peak,
        )
        write_json(out / "final.loudness.json", measured)
        label = "AI 生成 · SpeakVideo" + (
            " · 测试音频" if project.settings.tts.mode == "demo" else ""
        )
        overlay = Image.new("RGBA", (cfg.width, cfg.height), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        d.rounded_rectangle(
            (35, 30, min(cfg.width - 30, 650), 92), radius=18, fill=(15, 22, 35, 210)
        )
        d.text((55, 41), label, font=font(28), fill="#c6ddff")
        overlay.save(work / "label.png")
        broll = [p.expanduser().resolve() for p in (broll or [])]
        if broll:
            # Normalize each clip independently, concatenate in specified order, then loop as needed.
            clips = []
            for i, path in enumerate(broll):
                dst = work / f"broll-{i:04}.mp4"
                ffmpeg(
                    [
                        "-i",
                        path,
                        "-an",
                        "-vf",
                        f"scale={cfg.width}:{cfg.height}:force_original_aspect_ratio=increase,crop={cfg.width}:{cfg.height},setsar=1",
                        "-r",
                        cfg.fps,
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-pix_fmt",
                        "yuv420p",
                        dst,
                    ]
                )
                clips.append(f"file '{dst.name}'")
            (work / "broll.txt").write_text("\n".join(clips) + "\n")
            source = work / "broll.mp4"
            ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", "broll.txt", "-c", "copy", source], cwd=work
            )
            video_input = ["-stream_loop", "-1", "-i", source]
        else:
            cards = []
            for i, row in enumerate(timeline):
                img = Image.new("RGB", (cfg.width, cfg.height), "#0e1627")
                d = ImageDraw.Draw(img)
                d.rounded_rectangle(
                    (65, 160, cfg.width - 65, cfg.height - 125), radius=40, fill="#18263e"
                )
                d.text((110, 210), project.script().meta.topic, font=font(40), fill="#92a8c7")
                d.text((110, 330), row["slot"].upper(), font=font(32), fill="#6de0bc")
                face = font(54)
                for n, text in enumerate(wrapped(d, row["words"], face, cfg.width - 230)):
                    d.text((110, 420 + n * 78), text, font=face, fill="#f0f5ff")
                name = f"card-{i:04}.png"
                img.save(work / name)
                cards += [f"file '{name}'", f"duration {row['duration'] + row['pause']:.9f}"]
            cards.append(f"file 'card-{len(timeline) - 1:04}.png'")
            (work / "cards.txt").write_text("\n".join(cards) + "\n")
            video_input = ["-f", "concat", "-safe", "0", "-i", "cards.txt"]
        if burn_subtitles and broll:
            filters = subprocess.run(
                ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True
            ).stdout
            if " subtitles " not in filters:
                raise ValueError(
                    "当前 ffmpeg 未编译 libass，无法烧录 B-roll 字幕；默认仍输出 SRT 和内嵌字幕"
                )
            shutil.copyfile(out / "subtitles.srt", work / "subtitles.srt")
            video_filter = "[0:v][2:v]overlay=0:0,subtitles=subtitles.srt[v]"
        else:
            video_filter = "[0:v][2:v]overlay=0:0[v]"
        target = out / "final.mp4"
        temp = work / "final.tmp.mp4"
        ffmpeg(
            [
                *video_input,
                "-i",
                sound,
                "-loop",
                "1",
                "-i",
                "label.png",
                "-i",
                out / "subtitles.srt",
                "-filter_complex",
                video_filter,
                "-map",
                "[v]",
                "-map",
                "1:a",
                "-map",
                "3:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                cfg.fps,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=zho",
                "-t",
                duration,
                "-movflags",
                "+faststart",
                temp,
            ],
            cwd=work,
        )
        temp.replace(target)
        write_json(
            out / "render.json",
            {
                "duration": duration,
                "mode": project.settings.tts.mode,
                "inputs": {"broll": [str(p) for p in broll], "bgm": str(bgm) if bgm else None},
                "signature": render_signature(project, broll, bgm, burn_subtitles),
                "sha256": sha256(target),
                "probe": probe(target),
            },
        )
        state = read_json(project.root / "workflow.json")
        state["stage"] = "preview_review"
        write_json(project.root / "workflow.json", state)
        return target
