import hashlib
import io
import threading
import time
import wave

import pytest
from fastapi.testclient import TestClient
from test_video_workbench import wait_task

from speakvideo.jobs import report, synthesize
from speakvideo.script.schema import ScriptLine
from speakvideo.storage import atomic_bytes, read_json
from speakvideo.tts.client import TTSClient, TTSError, TTSResult
from speakvideo.voices import VoiceStore
from speakvideo.web import create_app


def wav(index):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(index.to_bytes(2, "little") * 2400)
    return TTSResult(output.getvalue(), 24000)


def gateway(project, monkeypatch, count=9):
    script, cast = project.script(), project.cast()
    script.lines = [
        ScriptLine(
            id=f"L{i:03}",
            slot=f"p{1 + i % 2}",
            words=f"并发测试第{i}组。",
            mood="normal",
            pause_after_ms=400,
        )
        for i in range(1, count + 1)
    ]
    project.save(script, cast)
    assets = VoiceStore(project.settings).assets()
    project.settings.tts.mode = "gateway"
    for role, emotion, content in assets:
        atomic_bytes(VoiceStore(project.settings).path(role, emotion), content)
    monkeypatch.setattr(VoiceStore, "remote_inventory", lambda self, client: self.details())
    monkeypatch.setattr(VoiceStore, "verify_remote", lambda *args: None)
    monkeypatch.setattr(TTSClient, "health", lambda self: {"ready": True})
    return script


@pytest.mark.parametrize("limit", [1, 3, 6])
def test_submit_window_out_of_order_audio_and_cache_reuse(project, monkeypatch, limit):
    script = gateway(project, monkeypatch)
    mutex, first_wave = threading.Lock(), threading.Event()
    submitted, payloads, finished = {}, {}, []
    peak = 0

    def submit(self, payload, key):
        nonlocal peak
        job = "j_" + hashlib.sha256(key.encode()).hexdigest()
        with mutex:
            submitted[job] = int(payload["text"].split("第")[1].split("组")[0])
            payloads[job] = payload
            peak = max(peak, len(submitted) - len(finished))
            if len(submitted) >= limit:
                first_wave.set()
        return job

    def wait_job(self, job, cancelled):
        assert first_wave.wait(2), "等待第一组结果前必须同时提交整个并发窗口"
        index = submitted[job]
        time.sleep(0.005 * (7 - index % 6))
        with mutex:
            finished.append(index)
        return wav(index)

    monkeypatch.setattr(TTSClient, "submit", submit)
    monkeypatch.setattr(TTSClient, "wait_job", wait_job)
    result = synthesize(project, concurrency=limit)
    assert result["status"] == "succeeded" and peak == limit
    assert len(submitted) == len(script.lines)
    if limit > 1:
        assert finished != list(range(1, 10))
    for index, line in enumerate(script.lines, 1):
        assert (project.root / "audio/segments" / f"{line.id}.wav").read_bytes() == wav(
            index
        ).wav_bytes
        record = result["lines"][line.id]
        assert payloads[record["job_id"]]["voice_sha256"] == record["parameters"]["voice_sha256"]
    # Scheduling changes neither synthesis fingerprints nor audio files.
    synthesize(project, concurrency=6 if limit == 1 else 1)
    assert len(submitted) == 9 and report(project, False)["reusable"] == 9


def test_pause_preserves_every_inflight_id_and_resume_does_not_resubmit(project, monkeypatch):
    gateway(project, monkeypatch)
    submitted, mutex, all_submitted, pause = (
        [],
        threading.Lock(),
        threading.Event(),
        threading.Event(),
    )

    def submit(self, payload, key):
        job = "j_" + hashlib.sha256(key.encode()).hexdigest()
        with mutex:
            submitted.append(job)
            if len(submitted) == 6:
                all_submitted.set()
        return job

    def wait_job(self, job, cancelled):
        assert all_submitted.wait(2)
        pause.set()
        raise TTSError("已暂停；远端任务 ID 已保存，可继续轮询")

    monkeypatch.setattr(TTSClient, "submit", submit)
    monkeypatch.setattr(TTSClient, "wait_job", wait_job)
    synthesize(project, concurrency=6, cancelled=pause.is_set)
    manifest = read_json(project.root / "manifest.json")
    assert manifest["status"] == "paused"
    assert len(submitted) == 6
    ids = {record["job_id"] for record in manifest["lines"].values()}
    assert ids == set(submitted)
    assert all(record["status"] == "pending" for record in manifest["lines"].values())
    pause.clear()
    monkeypatch.setattr(TTSClient, "wait_job", lambda *args: wav(1))
    assert synthesize(project, concurrency=3)["status"] == "succeeded"
    assert len(submitted) == 9 and len(set(submitted)) == 9


def test_failure_stops_refill_preserves_completed_audio_and_original_error(project, monkeypatch):
    gateway(project, monkeypatch)
    mutex, initial, saved = threading.Lock(), threading.Event(), threading.Event()
    submitted, mapping = [], {}
    from speakvideo import jobs

    original_write = jobs.write_json

    def write(path, content):
        original_write(path, content)
        if content.get("lines", {}).get("L001", {}).get("status") == "succeeded":
            saved.set()

    def submit(self, payload, key):
        with mutex:
            job = "j_" + hashlib.sha256(key.encode()).hexdigest()
            mapping[job] = int(payload["text"].split("第")[1].split("组")[0])
            submitted.append(job)
            if len(submitted) == 3:
                initial.set()
        return job

    def wait_job(self, job, cancelled):
        assert initial.wait(2)
        if mapping[job] == 1:
            return wav(1)
        if mapping[job] == 2:
            assert saved.wait(2)
            raise TTSError("远端任务失败", detail="CUDA oom")
        while not cancelled():
            time.sleep(0.002)
        raise TTSError("保留远端任务，停止轮询")

    monkeypatch.setattr(jobs, "write_json", write)
    monkeypatch.setattr(TTSClient, "submit", submit)
    monkeypatch.setattr(TTSClient, "wait_job", wait_job)
    with pytest.raises(TTSError, match="CUDA oom"):
        synthesize(project, concurrency=3)
    manifest = read_json(project.root / "manifest.json")
    assert manifest["status"] == "failed" and manifest["lines"]["L002"]["status"] == "failed"
    assert manifest["lines"]["L003"]["status"] == "pending"
    assert (project.root / "audio/segments/L001.wav").read_bytes() == wav(1).wav_bytes
    assert len(submitted) <= 4  # One free slot may refill before the failure is observed.
    monkeypatch.setattr(TTSClient, "wait_job", lambda *args: wav(1))
    assert synthesize(project, concurrency=6)["status"] == "succeeded"
    assert len(submitted) == 10  # Only the confirmed failure needs a new job ID.


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
