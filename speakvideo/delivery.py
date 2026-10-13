"""Build a verified, versioned delivery from the reviewed local audio and video."""

from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from .audio import audio_current
from .project import Project
from .storage import lock, read_json, sha256, write_json
from .video import video_current


def delivery_current(project: Project) -> bool:
    meta = read_json(project.root / "exports/delivery.json", {})
    try:
        path = (project.root / "exports" / meta["files"]["delivery.zip"]["path"]).resolve()
        return (
            path.is_relative_to((project.root / "exports/delivery").resolve())
            and all(project.approved(g) for g in ("script", "binding", "audio", "video"))
            and audio_current(project)
            and video_current(project)
            and meta["review"] == project.digest("video")
            and sha256(path) == meta["files"]["delivery.zip"]["sha256"]
        )
    except (KeyError, OSError, ValueError, TypeError):
        return False


def build_delivery(project: Project, cancelled=lambda: False, progress=lambda **kw: None) -> dict:
    with lock(project.root / ".project.lock"):
        if not all(project.approved(g) for g in ("script", "binding", "audio", "video")):
            raise ValueError("请先确认剧本、音色绑定、完整音频与成片")
        if not audio_current(project) or not video_current(project):
            raise ValueError("音视频交付已过期或损坏，请重新生成并确认")
        if delivery_current(project):
            return read_json(project.root / "exports/delivery.json")
        progress(stage="正在校验并打包成片、音频、字幕与工程快照", kind="stage")
        review = project.digest("video")
        token = uuid.uuid4().hex
        folder = project.root / "exports/delivery"
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / (token + ".tmp.zip")
        target = folder / (review[:20] + "-" + token[:8] + ".zip")
        checksums = {}
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED) as bundle:
                for kind in ("audio", "video"):
                    metadata = read_json(project.root / f"exports/{kind}.json")
                    for name, record in metadata["files"].items():
                        if cancelled():
                            raise RuntimeError("已暂停交付打包，原有交付保留")
                        relative = Path(name)
                        if relative.is_absolute() or ".." in relative.parts:
                            raise ValueError("交付文件名越界")
                        path = (project.root / "exports" / record["path"]).resolve()
                        if not path.is_relative_to((project.root / "exports").resolve()):
                            raise ValueError("交付文件路径越界")
                        digest = sha256(path)
                        if digest != record["sha256"]:
                            raise ValueError(f"交付文件哈希不一致：{name}")
                        archive_name = f"{kind}/{relative.as_posix()}"
                        bundle.write(path, archive_name)
                        checksums[archive_name] = digest
                # Only named project files are included; machine configuration is never traversed.
                for name in ("script.json", "cast.json", "workflow.json", "video.json"):
                    path = project.root / name
                    if path.is_file():
                        content = path.read_bytes()
                        bundle.writestr("project/" + name, content)
                        checksums["project/" + name] = hashlib.sha256(content).hexdigest()
                bundle.writestr("SHA256.json", json.dumps(checksums, ensure_ascii=False, indent=2))
                bundle.writestr(
                    "README.txt",
                    "SpeakVideo 交付包\n"
                    f"工程：{project.name}\n话题：{project.script().meta.topic}\n"
                    "video/final.mp4：已确认的成片\naudio/：WAV、MP3、字幕、时间线及生成快照\n"
                    "project/：剧本、绑定及成片设置\nSHA256.json：文件校验清单\n"
                    "本包用于本地交付和手动上传，未自动发布到任何平台。\n",
                )
            if cancelled():
                raise RuntimeError("已暂停交付打包，原有交付保留")
            temporary.replace(target)
            meta = {
                "version": 1,
                "review": review,
                "created_at": datetime.now(UTC).isoformat(),
                "files": {
                    "delivery.zip": {
                        "path": target.relative_to(project.root / "exports").as_posix(),
                        "bytes": target.stat().st_size,
                        "sha256": sha256(target),
                    }
                },
            }
            write_json(project.root / "exports/delivery.json", meta)
            return meta
        finally:
            temporary.unlink(missing_ok=True)
