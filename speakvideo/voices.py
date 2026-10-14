from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .storage import atomic_bytes, read_json, safe_name, sha256, write_json
from .tts.client import TTSClient, TTSError


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
            settings.paths.cache / "remote-voices"
            if settings.tts.mode == "gateway"
            else settings.tts.local_pt_root
            if settings.tts.mode == "direct" and settings.tts.local_pt_root
            else settings.paths.voices
        )

    def remote_inventory(self, client: TTSClient) -> list[dict]:
        try:
            remote = client.manage_voices("list")
            entries, editable = remote["assets"], True
        except TTSError as exc:
            if exc.status_code != 404:
                raise
            entries = [
                {"role": r, "emotion": e} for r, es in client.list_roles().items() for e in es
            ]
            editable = False
        self.inventory_status = {
            "editable": editable,
            "source": "windows",
            "message": "Windows 音色管理已连接"
            if editable
            else "Gateway 缺少 PT 管理路由，请升级网关",
        }
        keep = set()
        updates = []
        for entry in entries:
            role, emotion = entry["role"], entry["emotion"]
            target = self.path(role, emotion)
            keep.add(target)
            if not target.exists() or entry.get("sha256") != sha256(target):
                content = client.download_pt(role, emotion)
                digest = hashlib.sha256(content).hexdigest()
                if entry.get("sha256") and digest != entry["sha256"]:
                    raise ValueError(f"Windows PT 下载哈希不一致: {role}/{emotion}；请刷新后重试")
                self.validate_pt(content)
                updates.append((target, content))
        # Validate every download before replacing any cached inference asset.
        for target, content in updates:
            atomic_bytes(target, content)
        for entry in entries:
            target = self.path(entry["role"], entry["emotion"])
            write_json(target.with_suffix(".json"), entry)
        for target in self.root.glob("*/*.pt"):
            if target not in keep:
                target.unlink()
                target.with_suffix(".json").unlink(missing_ok=True)
                target.with_suffix(".wav").unlink(missing_ok=True)
        self.index()
        return [
            {
                **self.detail(v["role"], v["emotion"]),
                "source": "windows",
                "editable": editable,
                "has_reference": v.get("has_reference", False),
            }
            for v in entries
        ]

    def remote_mutation(self, client: TTSClient, action: str, **values):
        result = client.manage_voices(action, **values)
        self.remote_inventory(client)
        return result

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
        source, target = self.path(role, emotion), self.path(new_role, new_emotion)
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
            self.queue_deletion(role, emotion, sha256(source))
            source.unlink()
            source.with_suffix(".json").unlink(missing_ok=True)
            source.with_suffix(".wav").unlink(missing_ok=True)
        self.index()
        return self.detail(new_role, new_emotion)

    def delete(self, role: str, emotion: str, detach: bool = False, remote_deleted: bool = False):
        source = self.path(role, emotion)
        self.detail(role, emotion)
        used = self.usage(role, emotion)
        if used and not detach:
            raise RuntimeError("该 PT 正被工程绑定，请先解除绑定再删除")
        if not remote_deleted:
            self.queue_deletion(role, emotion, sha256(source))
        if detach:
            for item in used:
                folder = self.settings.paths.projects / item["project"]
                from .storage import lock

                with lock(folder / ".project.lock"):
                    cast = read_json(folder / "cast.json", {})
                    binding = cast[item["slot"]]
                    binding["mood_map"] = {
                        mood: value
                        for mood, value in binding["mood_map"].items()
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

    def queue_deletion(self, role: str, emotion: str, digest: str):
        if self.settings.tts.mode != "gateway":
            return
        items = self.deletions()
        items.append(
            {
                "id": uuid.uuid4().hex,
                "role": role,
                "emotion": emotion,
                "sha256": digest,
                "status": "pending",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        write_json(self.settings.paths.cache / "voice-deletions.json", items)

    def deletions(self):
        return read_json(self.settings.paths.cache / "voice-deletions.json", [])

    def flush_deletions(self, client: TTSClient):
        items = self.deletions()
        for item in items:
            if item["status"] != "pending":
                continue
            # Restoring/recreating a name cancels the old deletion. Never delete its new PT.
            if self.path(item["role"], item["emotion"]).exists():
                item.update(status="cancelled", error="")
            else:
                try:
                    client.delete_pt(item["role"], item["emotion"], item["sha256"])
                    item.update(status="deleted", error="")
                except TTSError as exc:
                    item["error"] = str(exc)
            write_json(self.settings.paths.cache / "voice-deletions.json", items)
        return items

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
        ref_text = ref_text.strip()
        if not ref_text:
            raise ValueError("请填写角色原声的对应台词")
        dst = self.path(role, emotion)
        if dst.exists() and not overwrite:
            raise ValueError("当前音色库已存在同名音色；覆盖需要显式 overwrite")
        client.create_pt(
            reference=reference, role=role, emotion=emotion, ref_text=ref_text, overwrite=overwrite
        )
        content = client.download_pt(role, emotion)
        if not content:
            raise ValueError("远端返回空 pt")
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
        if client.mode == "gateway":
            for r, e in needed:
                remote = client.download_pt(r, e)
                if hashlib.sha256(remote).hexdigest() != sha256(self.path(r, e)):
                    raise ValueError(f"Windows PT 已变化: {r}/{e}；请刷新音色库并重新确认绑定")
        else:
            for r, e in needed:
                remote = hashlib.sha256(client.download_pt(r, e)).hexdigest()
                if remote != sha256(self.path(r, e)):
                    raise ValueError(
                        f"{r}/{e} 本机文件与 Qwen PT 不一致；请检查实际 PT 目录并刷新音色库"
                    )
