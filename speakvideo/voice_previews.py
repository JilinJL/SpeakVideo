"""Durable voice auditions, independent of task and inference caches."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .storage import atomic_bytes, read_json, write_json
from .tts.client import TTSError, wav_info


class VoicePreviews:
    def __init__(self, root: Path):
        self.root = root / "voice-previews"

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("试听记录不存在")
        return self.root / f"{key}.wav"

    def save(
        self,
        content: bytes,
        *,
        kind: str,
        role: str = "",
        emotion: str = "",
        text: str = "",
        instruct: str = "",
        voice_sha256: str = "",
        key: str | None = None,
        created_at: str | None = None,
    ):
        key = key or uuid.uuid4().hex
        path = self.path(key)
        _, duration = wav_info(content)
        entry = {
            "id": key,
            "kind": kind,
            "role": role,
            "emotion": emotion,
            "text": text,
            "instruct": instruct,
            "voice_sha256": voice_sha256,
            "duration": duration,
            "created_at": created_at or datetime.now(UTC).isoformat(),
            "audio_url": f"/api/voices/previews/{key}/audio",
        }
        atomic_bytes(path, content)
        write_json(path.with_suffix(".json"), entry)
        return entry

    def entries(self):
        return sorted(
            (read_json(p) for p in self.root.glob("*.json") if p.with_suffix(".wav").is_file()),
            key=lambda entry: entry["created_at"],
            reverse=True,
        )

    def migrate(self, cache: Path):
        for task_file in cache.glob("task-*.json"):
            task = read_json(task_file, {})
            if task.get("label") not in {"design", "preview"} or task.get("status") != "succeeded":
                continue
            name = str(task.get("result", ""))
            if not re.fullmatch(r"[a-f0-9]{32}\.wav", name):
                continue
            source = cache / name
            if self.path(source.stem).exists() or not source.is_file():
                continue
            meta = read_json(cache / (name + ".json"), {})
            stamp = datetime.fromtimestamp(source.stat().st_mtime, UTC).isoformat()
            try:
                self.save(
                    source.read_bytes(),
                    key=source.stem,
                    kind=task["label"],
                    role=meta.get("role", ""),
                    emotion=meta.get("emotion", ""),
                    text=meta.get("text", ""),
                    instruct=meta.get("instruct", ""),
                    created_at=stamp,
                )
            except (ValueError, TTSError):
                continue  # An invalid legacy WAV must not prevent starting the workbench.
