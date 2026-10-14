import io
import time
import wave

import pytest
from fastapi.testclient import TestClient

from speakvideo.processing import ffmpeg
from speakvideo.storage import write_json
from speakvideo.tts.client import TTSClient, TTSResult
from speakvideo.voice_previews import VoicePreviews
from speakvideo.voices import VoiceStore
from speakvideo.web import create_app


def pcm():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "none"))
        audio.writeframes(b"\x01\x00" * 24000)
    return stream.getvalue()


def wait(client, task):
    for _ in range(100):
        result = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
        if result["status"] not in {"queued", "running"}:
            assert result["status"] == "succeeded", result
            return result
        time.sleep(0.02)
    pytest.fail("voice task did not finish")


@pytest.mark.parametrize(
    "text,reference",
    [
        ("", "a" * 32 + ".wav"),
        ("  \n ", "a" * 32 + ".wav"),
        ("对应台词", ""),
        ("对应台词", "../voice.wav"),
    ],
)
def test_pt_requires_original_and_corresponding_text_before_queue(project, text, reference):
    with TestClient(create_app(project.settings)) as client:
        response = client.post(
            "/api/voices/actions",
            json={"action": "create", "role": "新角色", "text": text, "reference_id": reference},
        )
        assert response.status_code == 422
        assert not client.get("/api/tasks").json()
    with TTSClient(project.settings) as client:
        with pytest.raises(ValueError, match="对应台词"):
            VoiceStore(project.settings).create(
                client, project.root / "missing.wav", "新角色", "平静", "  "
            )
        with pytest.raises(ValueError, match="对应台词"):
            client.create_pt(
                reference=project.root / "missing.wav", role="新角色", emotion="平静", ref_text=""
            )


def test_video_original_extracts_first_audio_track_and_rejects_silent_video(project):
    source = project.settings.paths.cache / "original.mp4"
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x32:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=240:duration=0.5",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            source,
        ]
    )
    with TestClient(create_app(project.settings)) as client:
        response = client.post(
            "/api/voices/reference",
            files={"file": ("角色原声.mp4", source.read_bytes(), "video/mp4")},
        )
        assert response.status_code == 200, response.text
        with wave.open(io.BytesIO(client.get(response.json()["preview_url"]).content)) as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (
                24000,
                1,
                2,
            )
        ffmpeg(["-i", source, "-an", "-c:v", "copy", source.with_name("silent.mov")])
        response = client.post(
            "/api/voices/reference",
            files={"file": ("silent.mov", source.with_name("silent.mov").read_bytes())},
        )
        assert response.status_code == 409
        assert not list(project.settings.paths.cache.glob("*.upload.*"))


def test_all_auditions_survive_restart_cache_cleanup_and_pt_replacement(project, monkeypatch):
    monkeypatch.setattr(TTSClient, "synthesize", lambda *args, **kwargs: TTSResult(pcm(), 24000))
    monkeypatch.setattr(
        TTSClient, "synthesize_design", lambda *args, **kwargs: TTSResult(pcm(), 24000)
    )
    with TestClient(create_app(project.settings)) as client:
        keys = []
        for kind, text in [
            ("preview", "第一版台词"),
            ("preview", "第二版台词"),
            ("design", "描述试听台词"),
        ]:
            task = client.post(
                "/api/voices/actions",
                json={
                    "action": kind,
                    "role": "讲述者",
                    "emotion": "平静",
                    "text": text,
                    "instruct": "清晰的声音",
                },
            ).json()
            keys.append(wait(client, task)["result"])
        history = client.get("/api/voices/previews").json()
        assert len(history) == 3 and {p["text"] for p in history} == {
            "第一版台词",
            "第二版台词",
            "描述试听台词",
        }
        assert len({p["id"] for p in history}) == 3
        assert all(client.get(p["audio_url"]).content == pcm() for p in history)
    for path in project.settings.paths.cache.glob("*"):
        if path.is_file():
            path.unlink()
    VoiceStore(project.settings).path("讲述者", "平静").write_bytes(b"replaced")
    with TestClient(create_app(project.settings)) as client:
        assert len(client.get("/api/voices/previews").json()) == 3
        assert all(client.get("/api/previews/" + key).content == pcm() for key in keys)
        assert client.get("/api/voices/previews/not-a-key/audio").status_code == 400


def test_create_retains_original_with_required_transcript(project, monkeypatch):
    monkeypatch.setattr(TTSClient, "create_pt", lambda *args, **kwargs: {})
    monkeypatch.setattr(TTSClient, "download_pt", lambda *args, **kwargs: b"new-pt")
    with TestClient(create_app(project.settings)) as client:
        original = client.post(
            "/api/voices/reference", files={"file": ("original.wav", pcm())}
        ).json()
        task = client.post(
            "/api/voices/actions",
            json={
                "action": "create",
                "role": "新角色",
                "emotion": "开心",
                "text": "角色原声对应的台词",
                "reference_id": original["reference_id"],
            },
        ).json()
        wait(client, task)
        history = client.get("/api/voices/previews").json()
        assert len(history) == 1 and history[0]["kind"] == "original"
        assert history[0]["text"] == "角色原声对应的台词"
        assert (
            client.get(history[0]["audio_url"]).content
            == client.get(original["preview_url"]).content
        )


def test_existing_cached_auditions_migrate_once_and_ignore_broken_audio(project):
    cache = project.settings.paths.cache
    key = "a" * 32
    (cache / f"{key}.wav").write_bytes(pcm())
    write_json(cache / f"{key}.wav.json", {"text": "旧试听台词", "instruct": "描述"})
    write_json(
        cache / "task-old.json",
        {"id": "old", "label": "design", "status": "succeeded", "result": f"{key}.wav"},
    )
    (cache / ("b" * 32 + ".wav")).write_bytes(b"broken")
    write_json(
        cache / "task-broken.json",
        {"id": "broken", "label": "preview", "status": "succeeded", "result": "b" * 32 + ".wav"},
    )
    previews = VoicePreviews(project.settings.paths.root)
    previews.migrate(cache)
    previews.migrate(cache)
    assert len(previews.entries()) == 1 and previews.entries()[0]["text"] == "旧试听台词"
