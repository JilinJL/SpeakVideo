from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .storage import atomic_bytes, read_json, safe_name, sha256, write_json
from .tts.client import TTSClient


class VoiceStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.tts.mode == "direct":
            if urlparse(settings.tts.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("本地模式必须连接本机 Qwen 服务")
            if settings.tts.local_pt_root is None:
                with TTSClient(settings) as client:
                    info = client._request("GET", "/api/pt/list").json()
                settings.tts.local_pt_root = Path(info["dir"])
            root = settings.tts.local_pt_root
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("本机 Qwen PT 目录不可用，请检查 tts.local_pt_root")
        self.root = (
            settings.tts.local_pt_root
            if settings.tts.mode == "direct" and settings.tts.local_pt_root
            else settings.paths.voices
        )

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
            "has_reference": path.with_suffix(".wav").is_file(),
        }

    def details(self) -> list[dict]:
        return [self.detail(r, e) for (r, es) in self.list_roles().items() for e in es]

    @staticmethod
    def validate_pt(content: bytes):
        if not content or len(content) > 32 * 1024 * 1024:
            raise ValueError("PT 文件必须非空，且不超过 32 MB")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as bundle:
                names = bundle.namelist()
                if not any((n.endswith("/data.pkl") for n in names)) or not any(
                    (n.endswith("/version") for n in names)
                ):
                    raise ValueError("请选择 torch.save 生成的 PT 文件")
        except zipfile.BadZipFile as exc:
            raise ValueError("PT 格式无效；请使用当前 Qwen 导出的音色文件") from exc

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
        if exists and (not overwrite):
            raise RuntimeError("同名音色已存在；请选择替换文件")
        atomic_bytes(target, content)
        if exists:
            target.with_suffix(".wav").unlink(missing_ok=True)
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
        (source, target) = (self.path(role, emotion), self.path(new_role, new_emotion))
        self.detail(role, emotion)
        if source != target:
            if self.usage(role, emotion):
                raise RuntimeError("该 PT 正被工程绑定，请先修改这些工程的绑定再重命名")
            if target.exists():
                raise RuntimeError("目标名称已存在")
            atomic_bytes(target, source.read_bytes())
            if source.with_suffix(".wav").is_file():
                atomic_bytes(target.with_suffix(".wav"), source.with_suffix(".wav").read_bytes())
        meta = read_json(source.with_suffix(".json"), {})
        meta.update(ref_text=ref_text, notes=notes, sha256=sha256(target))
        write_json(target.with_suffix(".json"), meta)
        if source != target:
            for old in (source, source.with_suffix(".json"), source.with_suffix(".wav")):
                old.unlink(missing_ok=True)
            if not any(source.parent.iterdir()):
                source.parent.rmdir()
        self.index()
        return self.detail(new_role, new_emotion)

    def delete(self, role: str, emotion: str, detach: bool = False, remote_deleted: bool = False):
        source = self.path(role, emotion)
        self.detail(role, emotion)
        used = self.usage(role, emotion)
        if used and (not detach):
            raise RuntimeError("该 PT 正被工程绑定，请先解除绑定再删除")
        if detach:
            for item in used:
                folder = self.settings.paths.projects / item["project"]
                from .storage import lock

                with lock(folder / ".project.lock"):
                    cast = read_json(folder / "cast.json", {})
                    binding = cast[item["slot"]]
                    binding["mood_map"] = {
                        mood: value
                        for (mood, value) in binding["mood_map"].items()
                        if value != emotion
                    }
                    write_json(folder / "cast.json", cast)
                    state = read_json(folder / "workflow.json", {"approvals": {}})
                    for gate in ("binding", "audio"):
                        state["approvals"].pop(gate, None)
                    state["stage"] = "binding_review"
                    write_json(folder / "workflow.json", state)
        source.unlink()
        source.with_suffix(".json").unlink(missing_ok=True)
        source.with_suffix(".wav").unlink(missing_ok=True)
        if not any(source.parent.iterdir()):
            source.parent.rmdir()
        self.index()
        return {"deleted": True, "permanent": True, "detached": used if detach else []}

    def deletions(self):
        return []

    def flush_deletions(self, client: TTSClient):
        return []

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
            for (r, es) in self.list_roles().items()
            for e in es
            if needed is None or (r, e) in needed
        ]

    def import_directory(self, source: Path, overwrite: bool = False) -> int:
        count = 0
        for p in sorted(source.glob("*/*.pt")):
            role = p.parent.name
            emotion = p.stem.removeprefix(role + "_")
            dst = self.path(role, emotion)
            if dst.exists() and dst.read_bytes() != p.read_bytes() and (not overwrite):
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
            for (r, es) in self.list_roles().items()
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
        ref_text = ref_text.strip()
        if not ref_text:
            raise ValueError("请填写角色原声的对应台词")
        dst = self.path(role, emotion)
        if dst.exists() and (not overwrite):
            raise ValueError("当前音色库已存在同名音色；覆盖需要显式 overwrite")
        client.create_pt(
            reference=reference, role=role, emotion=emotion, ref_text=ref_text, overwrite=overwrite
        )
        content = client.download_pt(role, emotion)
        if not content:
            raise ValueError("配音服务返回空 PT")
        atomic_bytes(dst, content)
        atomic_bytes(dst.with_suffix(".wav"), reference.read_bytes())
        write_json(
            dst.with_suffix(".json"),
            {"ref_text": ref_text, "sha256": sha256(dst), "origin": "reference-clone"},
        )
        self.index()
        return dst

    def verify_remote(self, client: TTSClient, needed: set[tuple[str, str]]):
        if client.mode == "demo":
            return
        for r, e in needed:
            remote = hashlib.sha256(client.download_pt(r, e)).hexdigest()
            if remote != sha256(self.path(r, e)):
                raise ValueError(
                    f"{r}/{e} 本机文件与 Qwen PT 不一致；请检查实际 PT 目录并刷新音色库"
                )
