"""Platform checks without importing the inference stack."""

from __future__ import annotations

import csv
import io
import os
import platform
import shutil
import socket
import subprocess

import psutil


def profile(device="auto", model="base-small", *, system=None, machine=None, gpu_rows=None):
    system, machine = system or platform.system(), machine or platform.machine()
    if system == "Darwin":
        if machine.lower() != "arm64" or device.startswith("cuda"):
            raise ValueError("本机推理需要原生 Apple Silicon arm64 Python，设备请选择 auto / mps")
        return "macos"
    if system not in {"Windows", "Linux"} or machine.lower() not in {"amd64", "x86_64"}:
        raise ValueError("支持 Apple Silicon macOS 或 x86_64 Windows/Linux NVIDIA；不回退 CPU")
    if device == "mps":
        raise ValueError("MPS 仅适用于 Apple Silicon")
    if gpu_rows is None:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,uuid,name,memory.total,compute_cap",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            gpu_rows = list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True))
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("无法检测 NVIDIA GPU，请检查驱动和 nvidia-smi") from exc
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        gpu_rows = [
            r for identifier in visible.split(",") for r in gpu_rows if identifier.strip() in r[:2]
        ]
    try:
        row = gpu_rows[int(device.partition(":")[2] or 0)]
        memory, capability = float(row[3]), float(row[4])
    except (IndexError, ValueError) as exc:
        raise ValueError("选定 GPU 不存在或无法读取显存信息") from exc
    minimum = 4096 if model == "base-small" else 6144
    if memory < minimum:
        raise ValueError(f"{row[2]} 仅 {memory:.0f} MiB 显存；此模型至少需要 {minimum} MiB")
    if capability < 6.1 or (capability >= 10 and capability != 12.0):
        raise ValueError(f"GPU sm_{capability:g} 尚未列入当前固定依赖支持范围")
    return "cu118" if capability < 7.5 else "cu128"


def port_available(port):
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def inspect(settings):
    memory = psutil.virtual_memory()
    root = settings.runtime.models_dir or settings.paths.root
    while not root.exists():
        root = root.parent
    info = {
        "system": platform.system(),
        "machine": platform.machine(),
        "memory_total_gb": round(memory.total / 1024**3, 1),
        "memory_available_gb": round(memory.available / 1024**3, 1),
        "disk_free_gb": round(shutil.disk_usage(root).free / 1024**3, 1),
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
    }
    try:
        info.update(
            profile=profile(settings.runtime.device, settings.runtime.model),
            supported=True,
            error="",
        )
    except ValueError as exc:
        info.update(profile=None, supported=False, error=str(exc))
    info["ffmpeg_install"] = {
        "Darwin": "brew install ffmpeg",
        "Windows": "winget install --id Gyan.FFmpeg -e",
        "Linux": "sudo apt-get update && sudo apt-get install -y ffmpeg",
    }.get(info["system"], "安装 ffmpeg 和 ffprobe 并加入 PATH")
    return info
