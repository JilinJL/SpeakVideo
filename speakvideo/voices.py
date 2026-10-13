from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .storage import atomic_bytes, read_json, safe_name, sha256, write_json
from .tts.client import TTSClient


class VoiceStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.paths.voices

    def path(self, role: str, emotion: str) -> Path:
        safe_name(role)
        safe_name(emotion)
        target = self.root / role / f"{role}_{emotion}.pt"
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("音色路径越界")
        return target

    def usage(self, role: str, emotion: str) -> list[dict]:
        result = []
        for path in sorted(self.settings.paths.projects.glob("*/cast.json")):
            for slot, binding in read_json(path, {}).items():
                if binding.get("role") == role and emotion in binding.get("mood_map", {}).values():
                    result.append({"project": path.parent.name, "slot": slot})
        return result

    def detail(self, role: str, emotion: str) -> dict:
        path = self.path(role, emotion)
        if not path.is_file():
            raise ValueError("音色不存在")
        meta = read_json(path.with_suffix(".json"), {})
        return {
            "role": role,
            "emotion": emotion,
            "filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "ref_text": meta.get("ref_text", ""),
            "notes": meta.get("notes", ""),
            "origin": meta.get("origin", "imported"),
            "usage": self.usage(role, emotion),
        }

    def details(self) -> list[dict]:
        return [self.detail(r, e) for r, es in self.list_roles().items() for e in es]

    @staticmethod
    def validate_pt(content: bytes):
        if not content or len(content) > 32 * 1024 * 1024:
            raise ValueError("PT 文件必须非空，且不超过 32 MB")
        # Inspect the container without unpickling uploaded Torch data on the Mac.
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as bundle:
                names = bundle.namelist()
                if not any(n.endswith("/data.pkl") for n in names) or not any(
                    n.endswith("/version") for n in names
                ):
                    raise ValueError("请选择 torch.save 生成的 PT 文件")
        except zipfile.BadZipFile as exc:
            raise ValueError("PT 格式无效；请使用当前 Qwen 导出的音色文件") from exc

    def archive(self, role: str, emotion: str, reason: str) -> str:
        source = self.path(role, emotion)
        key = uuid.uuid4().hex
        folder = self.settings.paths.cache / "voice-trash" / key
        atomic_bytes(folder / "voice.pt", source.read_bytes())
        write_json(
            folder / "entry.json",
            {
                "role": role,
                "emotion": emotion,
                "reason": reason,
                "created_at": datetime.now(UTC).isoformat(),
                "sha256": sha256(source),
                "bytes": source.stat().st_size,
                "metadata": read_json(source.with_suffix(".json"), {}),
            },
        )
        return key

    def import_pt(
        self,
        role: str,
        emotion: str,
        content: bytes,
        overwrite: bool = False,
        ref_text: str | None = None,
        notes: str | None = None,
    ):
        self.validate_pt(content)
        target = self.path(role, emotion)
        exists = target.exists()
        if exists and not overwrite:
            raise RuntimeError("同名音色已存在；请选择替换文件")
        if exists:
            self.archive(role, emotion, "replaced")
        atomic_bytes(target, content)
        meta = read_json(target.with_suffix(".json"), {}) if exists else {}
        meta.update(origin="pt-import", sha256=sha256(target))
        if ref_text is not None:
            meta["ref_text"] = ref_text
        if notes is not None:
            meta["notes"] = notes
        write_json(target.with_suffix(".json"), meta)
        self.index()
        return self.detail(role, emotion)

    def edit(
        self, role: str, emotion: str, new_role: str, new_emotion: str, ref_text: str, notes: str
    ):
        source, target = self.path(role, emotion), self.path(new_role, new_emotion)
        self.detail(role, emotion)
        if source != target:
            if self.usage(role, emotion):
                raise RuntimeError("该 PT 正被工程绑定，请先修改这些工程的绑定再重命名")
            if target.exists():
                raise RuntimeError("目标名称已存在")
            atomic_bytes(target, source.read_bytes())
        meta = read_json(source.with_suffix(".json"), {})
        meta.update(ref_text=ref_text, notes=notes, sha256=sha256(target))
        write_json(target.with_suffix(".json"), meta)
        if source != target:
            source.unlink()
            source.with_suffix(".json").unlink(missing_ok=True)
        self.index()
        return self.detail(new_role, new_emotion)

    def delete(self, role: str, emotion: str):
        source = self.path(role, emotion)
        self.detail(role, emotion)
        if self.usage(role, emotion):
            raise RuntimeError("该 PT 正被工程绑定，请先解除绑定再删除")
        key = self.archive(role, emotion, "deleted")
        source.unlink()
        source.with_suffix(".json").unlink(missing_ok=True)
        self.index()
        return {"deleted": True, "trash_id": key}

    def trash(self):
        return [
            {"id": p.parent.name, **read_json(p)}
            for p in sorted((self.settings.paths.cache / "voice-trash").glob("*/entry.json"))
        ]

    def restore(self, key: str):
        safe_name(key)
        folder = self.settings.paths.cache / "voice-trash" / key
        entry = read_json(folder / "entry.json")
        if not entry:
            raise ValueError("备份不存在")
        target = self.path(entry["role"], entry["emotion"])
        if target.exists():
            raise RuntimeError("同名音色已存在，恢复不会覆盖当前文件")
        atomic_bytes(target, (folder / "voice.pt").read_bytes())
        write_json(target.with_suffix(".json"), entry["metadata"])
        self.index()
        return self.detail(entry["role"], entry["emotion"])

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
        if dst.exists():
            self.archive(role, emotion, "replaced")
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
