from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


def ffmpeg(args: list[str], cwd: Path | None = None) -> str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("请安装 ffmpeg：brew install ffmpeg")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", *map(str, args)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("ffmpeg 失败：" + result.stderr[-3500:])
    return result.stderr


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def normalize(source: Path, target: Path, rate: int, lufs: float, peak: float) -> dict:
    base = f"loudnorm=I={lufs}:TP={peak}:LRA=11"
    stderr = ffmpeg(["-i", source, "-vn", "-af", base + ":print_format=json", "-f", "null", "-"])
    matches = re.findall(r'\{\s*"input_i".*?\}', stderr, flags=re.DOTALL)
    if not matches:
        raise RuntimeError("无法读取 loudnorm 第一遍测量")
    measured = json.loads(matches[-1])
    if any(
        str(measured[k]) in {"-inf", "inf", "nan"} for k in ("input_i", "input_tp", "input_thresh")
    ):
        raise ValueError("音轨静音或不足以测量响度")
    filt = (
        base + f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true:print_format=json"
    )
    stderr2 = ffmpeg(["-i", source, "-vn", "-af", filt, "-ar", rate, "-c:a", "pcm_s16le", target])
    second = re.findall(r'\{\s*"input_i".*?\}', stderr2, flags=re.DOTALL)
    return {
        "target_lufs": lufs,
        "target_peak": peak,
        "first_pass": measured,
        "second_pass": json.loads(second[-1]) if second else {},
    }


def srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
