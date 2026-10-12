from pathlib import Path

import pytest

from speakvideo.config import Paths, Settings, TTSSettings, VideoSettings
from speakvideo.demo import populate
from speakvideo.project import Project
from speakvideo.storage import atomic_bytes
from speakvideo.voices import VoiceStore


@pytest.fixture
def project(tmp_path: Path):
    settings = Settings(
        paths=Paths(root=tmp_path),
        tts=TTSSettings(mode="demo", max_retries=0),
        video=VideoSettings(width=640, height=360, fps=24),
    )
    settings.ensure_dirs()
    store = VoiceStore(settings)
    for role in ["讲述者", "回应者"]:
        atomic_bytes(store.path(role, "平静"), f"fixture-{role}".encode())
    p = Project(settings, "test")
    p.create("测试")
    populate(p)
    return p
