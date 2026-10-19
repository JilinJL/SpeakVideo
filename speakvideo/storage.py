from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

SAFE = re.compile(r"^[\w-]{1,60}$")


def safe_name(value: str) -> str:
    if not SAFE.fullmatch(value) or value in {".", ".."}:
        raise ValueError("名称仅支持字母、数字、中文、下划线、横线（1–60 字）")
    return value


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    try:
        temp.replace(path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    atomic_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = FileLock(str(path), timeout=0)
    try:
        guard.acquire()
    except Timeout as exc:
        raise RuntimeError("该工程或 GPU 队列正在运行，请稍后重试") from exc
    try:
        yield
    finally:
        guard.release()
