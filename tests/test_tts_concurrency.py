import io
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient
from test_video_workbench import wait_task

from speakvideo.jobs import report, synthesize
from speakvideo.script.schema import ScriptLine
from speakvideo.storage import read_json
from speakvideo.tts.client import TTSClient, TTSError, TTSResult
from speakvideo.web import create_app


def wav(index):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(index.to_bytes(2, "little") * 2400)
    return TTSResult(output.getvalue(), 24000)


def lines(project):
    script = project.script()
    script.lines = [
        ScriptLine(
            id=f"L{i:03}",
            slot=f"p{1 + i % 2}",
            words=f"并发测试第{i}组。",
            mood="normal",
            pause_after_ms=400,
        )
        for i in range(1, 10)
    ]
    project.save(script, project.cast())
    return script


@pytest.mark.parametrize("limit", [1, 3, 6])
def test_local_window_out_of_order_audio_and_cache_reuse(project, monkeypatch, limit):
    script = lines(project)
    mutex = threading.Lock()
    wave_ready = threading.Event()
    submitted = []
    finished = []
    peak = 0

    def generate(self, **kw):
        nonlocal peak
        index = int(kw["text"].split("第")[1].split("组")[0])
        with mutex:
            submitted.append(index)
            peak = max(peak, len(submitted) - len(finished))
            if len(submitted) >= limit:
                wave_ready.set()
        assert wave_ready.wait(2)
        time.sleep(0.005 * (7 - index % 6))
        with mutex:
            finished.append(index)
        return wav(index)

    monkeypatch.setattr(TTSClient, "synthesize", generate)
    result = synthesize(project, concurrency=limit)
    assert result["status"] == "succeeded" and peak == limit and len(submitted) == 9
    if limit > 1:
        assert finished != list(range(1, 10))
    for i, line in enumerate(script.lines, 1):
        assert (project.root / "audio/segments" / f"{line.id}.wav").read_bytes() == wav(i).wav_bytes
    synthesize(project, concurrency=6 if limit == 1 else 1)
    assert len(submitted) == 9 and report(project, False)["reusable"] == 9


def test_local_failure_retains_valid_audio_and_resume_only_missing(project, monkeypatch):
    lines(project)
    completed = []

    def fail(self, **kw):
        index = int(kw["text"].split("第")[1].split("组")[0])
        if index == 2:
            raise TTSError("CUDA oom")
        completed.append(index)
        return wav(index)

    monkeypatch.setattr(TTSClient, "synthesize", fail)
    with pytest.raises(TTSError, match="CUDA oom"):
        synthesize(project, concurrency=1)
    first = (project.root / "audio/segments/L001.wav").read_bytes()
    assert read_json(project.root / "manifest.json")["status"] == "failed"
    monkeypatch.setattr(TTSClient, "synthesize", lambda self, **kw: wav(1))
    assert synthesize(project, concurrency=3)["status"] == "succeeded"
    assert (project.root / "audio/segments/L001.wav").read_bytes() == first


def test_project_concurrency_validation_persistence_and_task_snapshot(project, monkeypatch):
    seen, entered, release = [], threading.Event(), threading.Event()
    from speakvideo import web

    def generate(p, cancelled, concurrency):
        seen.append(concurrency)
        entered.set()
        assert release.wait(2)
        return {"status": "succeeded"}

    monkeypatch.setattr(web, "synthesize", generate)
    with TestClient(create_app(project.settings)) as client:
        assert client.get("/api/projects/test").json()["synthesis"] == {"concurrency": 1}
        for invalid in [0, 7, 1.5, True, "6"]:
            assert (
                client.put(
                    "/api/projects/test/synthesis", json={"concurrency": invalid}
                ).status_code
                == 422
            )
            assert (
                client.post(
                    "/api/projects/test/actions", json={"action": "synth", "concurrency": invalid}
                ).status_code
                == 422
            )
        assert (
            client.put("/api/projects/test/synthesis", json={"concurrency": 6}).status_code == 200
        )
        task = client.post("/api/projects/test/actions", json={"action": "synth"}).json()
        assert entered.wait(2) and task["concurrency"] == 6
        assert (
            client.put("/api/projects/test/synthesis", json={"concurrency": 1}).status_code == 409
        )
        release.set()
        assert wait_task(client, task["id"])["status"] == "succeeded"
        assert seen == [6]
        monkeypatch.setattr(
            web, "advance", lambda p, **kwargs: seen.append(kwargs["concurrency"]) or "audio_review"
        )
        workflow = client.post(
            "/api/projects/test/actions", json={"action": "run", "concurrency": 3}
        ).json()
        assert wait_task(client, workflow["id"])["status"] == "succeeded"
        assert seen == [6, 3] and workflow["concurrency"] == 3
    with TestClient(create_app(project.settings)) as client:
        assert client.get("/api/projects/test").json()["synthesis"] == {"concurrency": 6}
