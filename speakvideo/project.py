from __future__ import annotations

import hashlib
import json
import uuid

from .config import Settings
from .script.prompts import DEFAULT_PERSONAS
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
                    {"slot": slot, "persona": persona} for slot, persona in DEFAULT_PERSONAS.items()
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
        # Writing controls affect future generations, not the reviewed dialogue.
        # Excluding the new field also preserves approvals on legacy projects.
        script_data = self.script().model_dump(exclude={"meta": {"prompts"}})
        if gate == "script":
            data = script_data
        elif gate == "binding":
            data = {
                "script": script_data,
                "cast": {k: v.model_dump() for k, v in self.cast().items()},
                "voices": self.voice_digests(),
                "source": {
                    "mode": self.settings.tts.mode,
                    "endpoint": self.settings.tts.base_url,
                    "worker_id": self.settings.tts.worker_id
                    if self.settings.tts.mode == "gateway"
                    else None,
                    "local_pt_root": str(self.settings.tts.local_pt_root)
                    if self.settings.tts.mode == "direct"
                    else None,
                },
            }
        elif gate == "audio":
            metadata = read_json(self.root / "exports/audio.json")
            data = {
                "binding": self.digest("binding"),
                "signature": metadata["signature"],
                "mix": read_json(self.root / "media.json", {}),
                "master": sha256(self.root / "exports" / metadata["files"]["master.wav"]["path"]),
            }
        elif gate == "video":
            from .video import video_config, video_signature

            metadata = read_json(self.root / "exports/video.json")
            data = {
                "audio": self.digest("audio"),
                "signature": metadata["signature"],
                "current_signature": video_signature(self),
                "settings": read_json(self.root / "video.json", {}),
                "video": sha256(self.root / "exports" / metadata["files"]["final.mp4"]["path"]),
            }
            if video_config(self)["cover"]["options"]["enabled"]:
                cover = read_json(self.root / "exports/cover.json", {})
                data["cover"] = {
                    "signature": cover.get("signature"),
                    "files": {
                        name: sha256(self.root / "exports" / f["path"])
                        for name, f in cover.get("files", {}).items()
                    },
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
        if gate == "video":
            from .video import cover_current, video_config, video_current

            if not video_current(self):
                raise ValueError("成片已过期、缺失或损坏，请先重新合成视频")
            if video_config(self)["cover"]["options"]["enabled"] and not cover_current(self):
                raise ValueError("封面已过期、缺失或损坏，请先重新导出封面")
            if not all(self.approved(g) for g in ("script", "binding", "audio")):
                raise ValueError("请先完成剧本、音色绑定与完整音频确认")
        state = read_json(self.root / "workflow.json", {"approvals": {}})
        state["approvals"][gate] = self.digest(gate)
        state["stage"] = {
            "script": "binding_review",
            "binding": "ready",
            "audio": "complete",
            "video": "delivery_ready",
        }[gate]
        write_json(self.root / "workflow.json", state)

    def save(self, script: Script, cast: dict[str, VoiceBinding]) -> None:
        write_json(self.root / "script.json", script.model_dump())
        write_json(self.root / "cast.json", {k: v.model_dump() for k, v in cast.items()})
