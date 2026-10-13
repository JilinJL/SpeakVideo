import hashlib
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from speakvideo.audio import build_audio
from speakvideo.delivery import build_delivery, delivery_current
from speakvideo.jobs import synthesize
from speakvideo.storage import read_json, write_json
from speakvideo.video import VideoOptions, render_video
from speakvideo.web import create_app
from speakvideo.workflow import advance


def reviewed_audio(project):
    project.approve("script")
    project.approve("binding")
    synthesize(project)
    build_audio(project)
    project.approve("audio")
    write_json(
        project.root / "video.json",
        {
            "options": VideoOptions(width=640, height=360, fps=24, font_size=24).model_dump(),
            "assets": [],
        },
    )


def test_video_workflow_stops_at_review_and_packages_after_explicit_approval(project):
    reviewed_audio(project)
    assert advance(project, include_video=True) == "video_review"
    with pytest.raises(ValueError, match="确认"):
        build_delivery(project)
    with TestClient(create_app(project.settings)) as web:
        assert web.post("/api/projects/test/approve/video").status_code == 200
        assert web.get("/api/projects/test").json()["workflow"]["approvals"]["video"]
    assert advance(project, include_video=True) == "delivery_ready"
    assert delivery_current(project)
    meta = read_json(project.root / "exports/delivery.json")
    path = project.root / "exports" / meta["files"]["delivery.zip"]["path"]
    # A private machine config beside the project must never enter the deliverable.
    (project.root / "config.local.toml").write_text('token="PRIVATE_SECRET"')
    with zipfile.ZipFile(path) as bundle:
        assert bundle.testzip() is None
        names = bundle.namelist()
        assert {
            "video/final.mp4",
            "audio/master.wav",
            "audio/master.mp3",
            "project/script.json",
        }.issubset(names)
        assert "config.local.toml" not in names
        for name, digest in json.loads(bundle.read("SHA256.json")).items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == digest
    assert build_delivery(project) == meta
    config = read_json(project.root / "video.json")
    config["options"]["font_size"] += 1
    write_json(project.root / "video.json", config)
    assert not project.approved("video") and not delivery_current(project)
    with pytest.raises(ValueError):
        project.approve("video")
    assert path.is_file()


def test_packaging_cancellation_preserves_old_delivery_and_removes_partial(project):
    reviewed_audio(project)
    render_video(project)
    project.approve("video")
    first = build_delivery(project)
    pointer = (project.root / "exports/delivery.json").read_bytes()
    # Corruption invalidates the existing ZIP, forcing a rebuild that can be cancelled.
    path = project.root / "exports" / first["files"]["delivery.zip"]["path"]
    path.write_bytes(b"corrupt old zip")
    with pytest.raises(RuntimeError, match="暂停"):
        build_delivery(project, cancelled=lambda: True)
    assert (project.root / "exports/delivery.json").read_bytes() == pointer
    assert path.read_bytes() == b"corrupt old zip"
    assert not list((project.root / "exports/delivery").glob("*.tmp.zip"))
