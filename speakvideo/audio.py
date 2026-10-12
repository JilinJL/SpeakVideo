"""Audio delivery pipeline. No video rendering, model loading or network calls."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
import wave
from contextlib import ExitStack
from pathlib import Path

from .jobs import parameters, report, reusable
from .processing import ffmpeg, normalize, probe, srt_time
from .project import Project
from .storage import lock, read_json, safe_name, sha256, write_json


class AudioPaused(RuntimeError):
    pass


def mix_options(project: Project, bgm: Path | None = None, volume: float | None = None) -> dict:
    media = read_json(project.root / "media.json", {})
    path = bgm or (project.root / media["bgm"] if media.get("bgm") else None)
    gain = (
        volume if volume is not None else media.get("bgm_volume", project.settings.audio.bgm_volume)
    )
    if not 0 <= gain <= 1:
        raise ValueError("BGM 音量必须在 0–1 之间")
    return {"bgm": str(path.expanduser().resolve()) if path else None, "bgm_volume": gain}


def audio_signature(project: Project, options: dict) -> str:
    script, cast = project.script(), project.cast()
    manifest = read_json(project.root / "manifest.json", {"lines": {}})
    data = {
        "lines": [
            {
                "id": line.id,
                "slot": line.slot,
                "words": line.words,
                "pause_after_ms": line.pause_after_ms,
                "params": parameters(project, line, cast),
                "sha256": manifest["lines"].get(line.id, {}).get("sha256"),
            }
            for line in script.lines
        ],
        "audio_settings": project.settings.audio.model_dump(),
        "mix": {**options, "bgm_sha256": sha256(Path(options["bgm"])) if options["bgm"] else None},
        "pipeline_version": 1,
    }
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def audio_current(project: Project, options: dict | None = None) -> bool:
    meta = read_json(project.root / "exports/audio.json", {})
    try:
        if not meta or meta["signature"] != audio_signature(
            project, options or mix_options(project)
        ):
            return False
        manifest = read_json(project.root / "manifest.json", {"lines": {}})
        script, cast = project.script(), project.cast()
        if not all(
            reusable(project, line, cast, manifest["lines"].get(line.id)) for line in script.lines
        ):
            return False
        return all(
            sha256(project.root / "exports" / f["path"]) == f["sha256"]
            for f in meta["files"].values()
        )
    except (OSError, KeyError, ValueError):
        return False


def assemble(
    project: Project, destination: Path, cancelled=lambda: False
) -> tuple[list[dict], Path]:
    script, cast = project.script(), project.cast()
    manifest = read_json(project.root / "manifest.json", {"lines": {}})
    if not script.lines or not all(
        reusable(project, line, cast, manifest["lines"].get(line.id)) for line in script.lines
    ):
        raise ValueError("存在缺失、损坏或过期分句，请先合成/续跑")
    rate = project.settings.audio.model_sample_rate
    raw = destination / "dialogue.raw.wav"
    roles = {line.slot: destination / f"{safe_name(line.slot)}.raw.wav" for line in script.lines}
    timeline, subtitles, cursor = [], [], 0
    with ExitStack() as stack:
        combined = stack.enter_context(wave.open(str(raw), "wb"))
        stems = {
            slot: stack.enter_context(wave.open(str(path), "wb")) for slot, path in roles.items()
        }
        for stream in [combined, *stems.values()]:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        for index, line in enumerate(script.lines, 1):
            if cancelled():
                raise AudioPaused("已暂停；已生成的分句不受影响，旧音频交付保持可用")
            source = project.root / "audio/segments" / f"{line.id}.wav"
            pcm = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(source),
                    "-f",
                    "s16le",
                    "-ac",
                    "1",
                    "-ar",
                    str(rate),
                    "-",
                ],
                capture_output=True,
                check=True,
            ).stdout
            frames = len(pcm) // 2
            pause_frames = round(line.pause_after_ms * rate / 1000)
            silence = bytes(pause_frames * 2)
            combined.writeframes(pcm + silence)
            for slot, stream in stems.items():
                stream.writeframes((pcm if slot == line.slot else bytes(len(pcm))) + silence)
            start, end = cursor / rate, (cursor + frames) / rate
            timeline.append(
                {
                    "id": line.id,
                    "slot": line.slot,
                    "words": line.words,
                    "start": start,
                    "end": end,
                    "duration": frames / rate,
                    "pause": pause_frames / rate,
                    "start_frame": cursor,
                    "end_frame": cursor + frames,
                    "pause_frames": pause_frames,
                }
            )
            subtitles.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{line.words}\n")
            cursor += frames + pause_frames
    (destination / "subtitles.srt").write_text("\n".join(subtitles), encoding="utf-8")
    write_json(
        destination / "timeline.json",
        {"sample_rate": rate, "total_frames": cursor, "duration": cursor / rate, "lines": timeline},
    )
    for slot, source in roles.items():
        ffmpeg(
            [
                "-i",
                source,
                "-ar",
                project.settings.audio.master_sample_rate,
                "-c:a",
                "pcm_s16le",
                destination / f"{slot}.wav",
            ]
        )
    return timeline, raw


def build_audio(
    project: Project,
    bgm: Path | None = None,
    bgm_volume: float | None = None,
    cancelled=lambda: False,
) -> dict:
    """Publish an immutable output bundle only after every export succeeds."""
    with lock(project.root / ".project.lock"):
        checks = report(project, False)
        if not checks["ok"] or checks["reusable"] != checks["line_count"]:
            raise ValueError(
                "完整音频导出需要全部有效分句："
                + "；".join(checks["errors"] or ["请先合成 / 续跑"])
            )
        options = mix_options(project, bgm, bgm_volume)
        if audio_current(project, options):
            return read_json(project.root / "exports/audio.json")
        signature = audio_signature(project, options)
        token = uuid.uuid4().hex
        staging = project.root / "audio" / f"build-{token}"
        staging.mkdir(parents=True)
        cfg = project.settings.audio

        def checkpoint():
            if cancelled():
                raise AudioPaused("已暂停；旧音频交付保持可用")

        try:
            checkpoint()
            timeline, raw = assemble(project, staging, cancelled)
            narration = staging / "narration.wav"
            stats = {
                "narration": normalize(
                    raw,
                    narration,
                    cfg.master_sample_rate,
                    cfg.narrative_lufs,
                    cfg.narrative_true_peak,
                )
            }
            checkpoint()
            duration = sum(row["duration"] + row["pause"] for row in timeline)
            mixed = staging / "mixed.raw.wav"
            if options["bgm"]:
                filt = (
                    f"[0:a]asplit=2[n][side];[1:a]aformat=sample_rates={cfg.master_sample_rate}:"
                    f"channel_layouts=stereo,volume={options['bgm_volume']}[music];"
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
                        options["bgm"],
                        "-filter_complex",
                        filt,
                        "-map",
                        "[mix]",
                        "-t",
                        duration,
                        "-ar",
                        cfg.master_sample_rate,
                        "-ac",
                        2,
                        "-c:a",
                        "pcm_s16le",
                        mixed,
                    ]
                )
            else:
                ffmpeg(
                    [
                        "-i",
                        narration,
                        "-ar",
                        cfg.master_sample_rate,
                        "-ac",
                        2,
                        "-c:a",
                        "pcm_s16le",
                        mixed,
                    ]
                )
            checkpoint()
            master = staging / "master.wav"
            stats["master"] = normalize(
                mixed, master, cfg.master_sample_rate, cfg.master_lufs, cfg.master_true_peak
            )
            checkpoint()
            ffmpeg(["-i", master, "-c:a", "libmp3lame", "-b:a", "320k", staging / "master.mp3"])
            stats["decoded_master"] = measure(master)
            stats["decoded_mp3"] = measure(staging / "master.mp3")
            write_json(staging / "loudness.json", stats)
            # Standalone bundle contains the exact generation inputs and manifest snapshot.
            write_json(staging / "script.json", project.script().model_dump())
            write_json(
                staging / "cast.json", {k: v.model_dump() for k, v in project.cast().items()}
            )
            write_json(staging / "manifest.json", read_json(project.root / "manifest.json"))
            public = [
                "master.wav",
                "master.mp3",
                "narration.wav",
                "timeline.json",
                "subtitles.srt",
                "loudness.json",
                "script.json",
                "cast.json",
                "manifest.json",
            ]
            public += [
                f"{safe_name(slot)}.wav" for slot in {line.slot for line in project.script().lines}
            ]
            bundle_name = signature[:20] + "-" + token[:8]
            files = {
                name: {
                    "path": f"audio/{bundle_name}/{name}",
                    "sha256": sha256(staging / name),
                    "bytes": (staging / name).stat().st_size,
                }
                for name in public
            }
            metadata = {
                "version": 1,
                "signature": signature,
                "options": options,
                "duration": duration,
                "mode": project.settings.tts.mode,
                "files": files,
                "audio": probe(master),
                "status": "succeeded",
            }
            checkpoint()
            bundle = project.root / "exports/audio" / bundle_name
            bundle.parent.mkdir(parents=True, exist_ok=True)
            # Remove only generated work files in this new staging directory.
            for path in staging.glob("*.raw.wav"):
                path.unlink()
            staging.rename(bundle)
            write_json(project.root / "exports/audio.json", metadata)
            if bgm is not None or bgm_volume is not None:
                media = read_json(project.root / "media.json", {})
                media.update(bgm=options["bgm"], bgm_volume=options["bgm_volume"])
                write_json(project.root / "media.json", media)
            state = read_json(project.root / "workflow.json", {"approvals": {}})
            state["stage"] = "audio_review"
            write_json(project.root / "workflow.json", state)
            return metadata
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def measure(path: Path) -> dict:
    stderr = ffmpeg(
        [
            "-i",
            path,
            "-vn",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    import re

    return json.loads(re.findall(r'\{\s*"input_i".*?\}', stderr, re.DOTALL)[-1])
