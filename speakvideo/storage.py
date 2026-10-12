from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_json(path: Path, data: Any) -> None:
    atomic_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("该工程或 GPU 队列正在运行，请稍后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
