import time
import wave

import pytest
from fastapi.testclient import TestClient

from speakvideo.audio import AudioPaused, audio_current, build_audio
from speakvideo.jobs import synthesize
from speakvideo.processing import ffmpeg, probe
from speakvideo.storage import read_json, write_json
from speakvideo.web import create_app


def exported(project, meta, name):
    return project.root / "exports" / meta["files"][name]["path"]


def test_real_audio_exports_timeline_stems_loudness_and_bgm(project):
    synthesize(project)
    meta = build_audio(project)
    master = exported(project, meta, "master.wav")
    timeline = read_json(exported(project, meta, "timeline.json"))
    assert timeline["lines"][1]["start"] == pytest.approx(
        timeline["lines"][0]["end"] + 0.4, abs=1 / 24000
    )
    with wave.open(str(master)) as wav:
        assert wav.getframerate() == 48000 and wav.getnchannels() == 2
        assert wav.getnframes() == timeline["total_frames"] * 2
    assert probe(exported(project, meta, "master.mp3"))["streams"][0]["codec_name"] == "mp3"
    stats = read_json(exported(project, meta, "loudness.json"))
    assert stats["narration"]["first_pass"] and stats["narration"]["second_pass"]
    assert float(stats["decoded_master"]["input_i"]) == pytest.approx(-16, abs=0.3)
    assert float(stats["decoded_master"]["input_tp"]) <= -1.4
    assert float(stats["decoded_mp3"]["input_i"]) == pytest.approx(-16, abs=0.6)
    with wave.open(str(exported(project, meta, "p1.wav"))) as wav:
        assert wav.getnframes() == timeline["total_frames"] * 2
        # Resampling can have a short edge tail; test the interior of the other speaker's line.
        row = timeline["lines"][1]
        wav.setpos(round((row["start"] + 0.02) * 48000))
        assert not any(wav.readframes(round((row["duration"] - 0.04) * 48000)))
    assert audio_current(project)
    assert build_audio(project)["signature"] == meta["signature"]
    assert build_audio(project)["files"] == meta["files"]  # reuse same immutable bundle
    project.approve("audio")
    assert project.approved("audio")
    before = project.script()
    before.lines[0].pause_after_ms += 50
    project.save(before, project.cast())
    assert not audio_current(project)
    with pytest.raises(ValueError, match="过期"):
        project.approve("audio")
    before.lines[0].pause_after_ms -= 50
    project.save(before, project.cast())
    bgm = project.root / "music.wav"
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=120:duration=2", "-c:a", "pcm_s16le", bgm])
    mixed = build_audio(project, bgm=bgm, bgm_volume=0.25)
    assert mixed["signature"] != meta["signature"]
    assert exported(project, meta, "master.wav").exists()  # previous bundle remains usable
    assert audio_current(project)
    stats = read_json(exported(project, mixed, "loudness.json"))
    assert float(stats["decoded_master"]["input_i"]) == pytest.approx(-16, abs=0.3)
    media = read_json(project.root / "media.json")
    media["bgm_volume"] = 0.1
    write_json(project.root / "media.json", media)
    assert not audio_current(project)


def test_failed_or_paused_mix_does_not_replace_delivery(project):
    synthesize(project)
    old = build_audio(project)
    before = (project.root / "exports/audio.json").read_bytes()
    invalid = project.root / "invalid.wav"
    invalid.write_bytes(b"not audio")
    with pytest.raises(RuntimeError):
        build_audio(project, bgm=invalid)
    assert (project.root / "exports/audio.json").read_bytes() == before
    script = project.script()
    script.lines[0].pause_after_ms += 100
    project.save(script, project.cast())
    with pytest.raises(AudioPaused):
        build_audio(project, cancelled=lambda: True)
    assert (project.root / "exports/audio.json").read_bytes() == before
    assert not list((project.root / "audio").glob("build-*"))
    assert exported(project, old, "master.mp3").exists()


def test_editor_api_guards_and_background_synth(project):
    with TestClient(create_app(project.settings)) as client:
        assert client.get("/").status_code == 200
        assert "完整音频试听" in client.get("/").text
        assert "B-roll" not in client.get("/").text
        result = client.get("/api/projects/test").json()
        assert result["preflight"]["ok"]
        result["script"]["lines"][0]["mood"] = "angry"
        saved = client.put(
            "/api/projects/test", json={"script": result["script"], "cast": result["cast"]}
        )
        assert not saved.json()["preflight"]["ok"]
        assert client.post("/api/projects/test/approve/binding").status_code == 400
        result["script"]["lines"][0]["mood"] = "normal"
        assert (
            client.put(
                "/api/projects/test", json={"script": result["script"], "cast": result["cast"]}
            ).status_code
            == 200
        )
        assert client.post("/api/projects/test/approve/script").status_code == 200
        assert client.post("/api/projects/test/approve/binding").status_code == 200
        t = client.post("/api/projects/test/actions", json={"action": "synth"}).json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = next(x for x in client.get("/api/tasks").json() if x["id"] == t["id"])
            if state["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert state["status"] == "succeeded", state
        assert state["progress"]["completed"] == 3
        assert (
            client.post("/api/projects/test/actions", json={"action": "render"}).status_code == 400
        )
        assert client.put("/api/projects/test/mix", json={"bgm_volume": 2}).status_code == 422
        assert (
            client.put(
                "/api/projects/test/mix", json={"bgm_volume": 0.2, "remove_bgm": True}
            ).status_code
            == 200
        )
        assert client.post("/api/projects", json={"name": "../escape"}).status_code == 400
        assert (
            client.post(
                "/api/projects", json={"name": "x"}, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.get("/api/config", headers={"Host": "evil.test"}).status_code == 400
        assert client.get("/api/projects/test/files/%2e%2e/config.local.toml").status_code == 404


def test_server_restart_marks_tasks_interrupted(project):
    write_json(
        project.settings.paths.cache / "task-old.json",
        {"id": "old", "status": "running", "label": "synth"},
    )
    with TestClient(create_app(project.settings)) as client:
        assert client.get("/api/tasks").json()[0]["status"] == "interrupted"


def test_audio_workflow_gates_and_cached_completion(project):
    from speakvideo.workflow import advance

    assert advance(project) == "script_review"
    project.approve("script")
    assert advance(project) == "binding_review"
    project.approve("binding")
    assert advance(project) == "audio_review"
    meta = read_json(project.root / "exports/audio.json")
    assert advance(project) == "audio_review"
    assert read_json(project.root / "exports/audio.json") == meta
    project.approve("audio")
    assert advance(project) == "complete"
