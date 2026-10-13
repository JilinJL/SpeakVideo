"""Windows-owned PT assets. No GPU/model imports for metadata and file operations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path

NAME = re.compile(r"^[\w-]{1,60}$")


class VoiceLibrary:
    def __init__(self, root: Path, install):
        self.root, self.install = root, install

    def path(self, role, emotion):
        if not NAME.fullmatch(role) or not NAME.fullmatch(emotion):
            raise ValueError("invalid voice name")
        path = (self.root / "pt" / role / f"{role}_{emotion}.pt").resolve()
        if not path.is_relative_to((self.root / "pt").resolve()):
            raise ValueError("voice path escapes PT root")
        return path

    def detail(self, role, emotion):
        path = self.path(role, emotion)
        meta = (
            json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            if path.with_suffix(".json").exists()
            else {}
        )
        return {
            **meta,
            "role": role,
            "emotion": emotion,
            "filename": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "has_reference": path.with_suffix(".wav").is_file(),
        }

    def save_reference(self, role, emotion, content, text):
        path = self.path(role, emotion)
        path.with_suffix(".wav").write_bytes(content)
        path.with_suffix(".json").write_text(
            json.dumps({"ref_text": text, "origin": "reference-clone"}, ensure_ascii=False),
            encoding="utf-8",
        )

    def manage(self, body):
        action = body["action"]
        if action == "list":
            assets = []
            for path in sorted((self.root / "pt").glob("*/*.pt")):
                role = path.parent.name
                if path.stem.startswith(role + "_"):
                    assets.append(self.detail(role, path.stem[len(role) + 1 :]))
            return {"assets": assets, "source": "windows"}
        if action in {"trash", "restore"}:
            raise ValueError("voice backups and restore are no longer supported")
        role, emotion = body["role"], body["emotion"]
        path = self.path(role, emotion)
        if action == "import":
            data = base64.b64decode(body["content_base64"], validate=True)
            if not data or len(data) > 32 * 1024 * 1024:
                raise ValueError("invalid PT size")
            if path.exists():
                if not body.get("overwrite"):
                    raise ValueError("voice already exists")
            self.install(
                self.root,
                [
                    {
                        "path": f"{role}/{role}_{emotion}.pt",
                        "content_base64": body["content_base64"],
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
            )
            path.with_suffix(".wav").unlink(missing_ok=True)
            path.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "ref_text": body.get("ref_text", ""),
                        "notes": body.get("notes", ""),
                        "origin": "pt-import",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return self.detail(role, emotion)
        detail = self.detail(role, emotion)
        if body.get("sha256") != detail["sha256"]:
            raise ValueError("voice SHA256 changed; refresh before editing")
        if action == "reference":
            return {
                "content_base64": base64.b64encode(path.with_suffix(".wav").read_bytes()).decode()
            }
        if action in {"delete", "delete_permanent"}:
            for suffix in (".pt", ".json", ".wav"):
                path.with_suffix(suffix).unlink(missing_ok=True)
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
            return {"deleted": True, "permanent": True, "remote_status": "deleted"}
        if action == "edit":
            new_role, new_emotion = body["new_role"], body["new_emotion"]
            target = self.path(new_role, new_emotion)
            if target != path:
                if target.exists():
                    raise ValueError("target voice already exists")
                target.parent.mkdir(parents=True, exist_ok=True)
                for suffix in (".pt", ".json", ".wav"):
                    source = path.with_suffix(suffix)
                    if source.exists():
                        os.replace(source, target.with_suffix(suffix))
            meta = {
                "origin": detail.get("origin", "imported"),
                "ref_text": body.get("ref_text", ""),
                "notes": body.get("notes", ""),
            }
            target.with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
            return self.detail(new_role, new_emotion)
        raise ValueError("unknown voice action")
