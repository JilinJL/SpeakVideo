from __future__ import annotations

import hashlib
import json
import uuid

from .config import Settings
from .script.schema import Script, VoiceBinding
from .storage import read_json, safe_name, sha256, write_json


class Project:
    def __init__(self, settings: Settings, name: str):
        self.settings = settings
        self.name = safe_name(name)
        self.root = settings.paths.projects / self.name

    def create(self, topic: str = "", keywords: list[str] | None = None) -> None:
        if self.root.exists():
            raise ValueError(f"工程已存在: {self.name}")
        self.root.mkdir(parents=True)
        write_json(
            self.root / "script.json",
            {
                "schema_version": "1.0",
                "template": "duo_dialogue",
                "meta": {"topic": topic, "keywords": keywords or [], "brief": ""},
                "cast": [
                    {"slot": "p1", "persona": "理性克制的主持人"},
                    {"slot": "p2", "persona": "犀利幽默的嘉宾"},
                ],
                "lines": [],
            },
        )
        write_json(self.root / "cast.json", {})
        write_json(self.root / "workflow.json", {"stage": "script_review", "approvals": {}})
        write_json(
            self.root / "manifest.json",
            {
                "version": 1,
                "project_id": uuid.uuid4().hex,
                "lines": {},
                "status": "idle",
            },
        )

    def script(self) -> Script:
        return Script.model_validate(read_json(self.root / "script.json"))

    def cast(self) -> dict[str, VoiceBinding]:
        return {
            k: VoiceBinding.model_validate(v)
            for k, v in read_json(self.root / "cast.json", {}).items()
        }

    def voice_digests(self):
        from .voices import VoiceStore

        store = VoiceStore(self.settings)
        result = {}
        for binding in self.cast().values():
            if binding.role:
                for emotion in set(binding.mood_map.values()):
                    path = store.path(binding.role, emotion)
                    result[f"{binding.role}/{emotion}"] = (
                        sha256(path) if path.exists() else "missing"
                    )
        return result

    def digest(self, gate: str) -> str:
        if gate == "script":
            data = self.script().model_dump()
        elif gate == "binding":
            data = {
                "script": self.script().model_dump(),
                "cast": {k: v.model_dump() for k, v in self.cast().items()},
                "voices": self.voice_digests(),
            }
        elif gate == "audio":
            metadata = read_json(self.root / "exports/audio.json")
            data = {
                "binding": self.digest("binding"),
                "signature": metadata["signature"],
                "mix": read_json(self.root / "media.json", {}),
                "master": sha256(self.root / "exports" / metadata["files"]["master.wav"]["path"]),
            }
        else:
            raise ValueError("未知确认阶段")
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def approved(self, gate: str) -> bool:
        try:
            return read_json(self.root / "workflow.json", {}).get("approvals", {}).get(
                gate
            ) == self.digest(gate)
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def approve(self, gate: str) -> None:
        if gate == "script" and not self.script().lines:
            raise ValueError("空剧本不能定稿")
        if gate == "script":
            from .script.schema import validate_ids

            errors = validate_ids(self.script())
            if errors:
                raise ValueError("；".join(errors))
        if gate == "audio":
            from .audio import audio_current

            if not audio_current(self):
                raise ValueError("音频已过期、缺失或损坏，请先重新合并混音")
        state = read_json(self.root / "workflow.json", {"approvals": {}})
        state["approvals"][gate] = self.digest(gate)
        state["stage"] = {"script": "binding_review", "binding": "ready", "audio": "complete"}[gate]
        write_json(self.root / "workflow.json", state)

    def save(self, script: Script, cast: dict[str, VoiceBinding]) -> None:
        write_json(self.root / "script.json", script.model_dump())
        write_json(self.root / "cast.json", {k: v.model_dump() for k, v in cast.items()})
