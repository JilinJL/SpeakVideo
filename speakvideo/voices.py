from __future__ import annotations

import hashlib
from pathlib import Path

from .config import Settings
from .storage import atomic_bytes, safe_name, sha256, write_json
from .tts.client import TTSClient


class VoiceStore:
    def __init__(self, settings: Settings):
        self.root = settings.paths.voices

    def path(self, role: str, emotion: str) -> Path:
        safe_name(role)
        safe_name(emotion)
        return self.root / role / f"{role}_{emotion}.pt"

    def list_roles(self) -> dict[str, list[str]]:
        result = {}
        for p in sorted(self.root.glob("*/*.pt")):
            role = p.parent.name
            safe_name(role)
            prefix = f"{role}_"
            if p.stem.startswith(prefix):
                emotion = p.stem[len(prefix) :]
                safe_name(emotion)
                result.setdefault(role, []).append(emotion)
        return result

    def assets(self, needed: set[tuple[str, str]] | None = None):
        return [
            (r, e, self.path(r, e).read_bytes())
            for r, es in self.list_roles().items()
            for e in es
            if needed is None or (r, e) in needed
        ]

    def import_directory(self, source: Path, overwrite: bool = False) -> int:
        count = 0
        for p in sorted(source.glob("*/*.pt")):
            role = p.parent.name
            emotion = p.stem.removeprefix(role + "_")
            dst = self.path(role, emotion)
            if dst.exists() and dst.read_bytes() != p.read_bytes() and not overwrite:
                raise ValueError(f"音色已存在且内容不同: {dst}")
            atomic_bytes(dst, p.read_bytes())
            count += 1
        self.index()
        return count

    def index(self):
        data = [
            {
                "role": r,
                "emotion": e,
                "sha256": sha256(self.path(r, e)),
                "bytes": self.path(r, e).stat().st_size,
            }
            for r, es in self.list_roles().items()
            for e in es
        ]
        write_json(self.root / "index.json", data)
        return data

    def create(
        self,
        client: TTSClient,
        reference: Path,
        role: str,
        emotion: str,
        ref_text: str,
        overwrite: bool = False,
    ):
        dst = self.path(role, emotion)
        if dst.exists() and not overwrite:
            raise ValueError("本地音色已存在；覆盖需要显式 overwrite")
        client.create_pt(
            reference=reference, role=role, emotion=emotion, ref_text=ref_text, overwrite=overwrite
        )
        content = client.download_pt(role, emotion)
        if not content:
            raise ValueError("远端返回空 pt")
        atomic_bytes(dst, content)
        write_json(
            dst.with_suffix(".json"),
            {"ref_text": ref_text, "sha256": sha256(dst), "origin": "reference-clone"},
        )
        self.index()
        return dst

    def verify_remote(self, client: TTSClient, needed: set[tuple[str, str]]):
        if client.mode == "demo":
            return
        if client.mode == "gateway":
            response = client.sync_voices(self.assets(needed))
            received = {(v["role"], v["emotion"]): v["sha256"] for v in response["voices"]}
            for r, e in needed:
                if received.get((r, e)) != sha256(self.path(r, e)):
                    raise ValueError(f"远端同步校验失败: {r}/{e}")
        else:
            for r, e in needed:
                remote = hashlib.sha256(client.download_pt(r, e)).hexdigest()
                if remote != sha256(self.path(r, e)):
                    raise ValueError(
                        f"{r}/{e} 本地与服务端 pt 不一致；请配置 gateway 同步，或手动更新 Qwen pt 目录"
                    )
