import pytest

from speakvideo.jobs import report, synthesize
from speakvideo.storage import atomic_bytes, read_json
from speakvideo.tts.client import TTSClient, TTSError
from speakvideo.voices import VoiceStore


def test_preflight_never_falls_back(project):
    script, cast = project.script(), project.cast()
    script.lines[0].mood = "angry"
    cast["p1"].default_emotion = "平静"
    project.save(script, cast)
    result = report(project, False)
    assert not result["ok"]
    assert any("angry" in e and "未映射" in e for e in result["errors"])


def test_resume_order_pause_text_and_voice_changes(project, monkeypatch):
    original = TTSClient.synthesize
    calls = []

    def track(self, **kwargs):
        calls.append(kwargs["text"])
        return original(self, **kwargs)

    monkeypatch.setattr(TTSClient, "synthesize", track)
    synthesize(project)
    assert len(calls) == 3
    assert report(project, False)["reusable"] == 3
    script, cast = project.script(), project.cast()
    script.lines.reverse()
    script.lines[0].pause_after_ms = 900
    project.save(script, cast)
    synthesize(project)
    assert len(calls) == 3
    script.lines[0].words += "修改"
    project.save(script, cast)
    synthesize(project)
    assert len(calls) == 4
    store = VoiceStore(project.settings)
    atomic_bytes(store.path("讲述者", "平静"), b"changed-voice")
    synthesize(project)
    assert len(calls) == 6  # p1 has two lines
    p = project.root / "audio/segments/L002.wav"
    p.write_bytes(b"corrupt")
    synthesize(project)
    assert len(calls) == 7


def test_interrupted_synthesis_only_retries_missing_line(project, monkeypatch):
    original = TTSClient.synthesize

    def fail(self, **kw):
        if kw["role"] == "回应者":
            raise TTSError("CUDA oom")
        return original(self, **kw)

    monkeypatch.setattr(TTSClient, "synthesize", fail)
    with pytest.raises(TTSError):
        synthesize(project)
    first = (project.root / "audio/segments/L001.wav").read_bytes()
    assert read_json(project.root / "manifest.json")["lines"]["L002"]["status"] == "failed"
    monkeypatch.setattr(TTSClient, "synthesize", original)
    synthesize(project)
    assert (project.root / "audio/segments/L001.wav").read_bytes() == first
    assert report(project, False)["reusable"] == 3


def test_gateway_poll_resume_does_not_resubmit(project, monkeypatch):
    with TTSClient(project.settings) as c:
        audio = c.synthesize(text="test", role="讲述者")
    project.settings.tts.mode = "gateway"
    monkeypatch.setattr(TTSClient, "health", lambda self: {"ready": True})
    monkeypatch.setattr(VoiceStore, "verify_remote", lambda *args: None)
    submissions = []
    monkeypatch.setattr(
        TTSClient, "submit", lambda self, payload, key: submissions.append(key) or "job1"
    )
    monkeypatch.setattr(
        TTSClient, "wait_job", lambda *args: (_ for _ in ()).throw(TTSError("network timeout"))
    )
    with pytest.raises(TTSError):
        synthesize(project)
    assert len(submissions) == 1
    monkeypatch.setattr(TTSClient, "wait_job", lambda *args: audio)
    synthesize(project)
    assert len(submissions) == 3  # only L002 and L003 submitted on resume
    synthesize(project, retry_line="L001")
    assert len(submissions) == 4  # explicit retry creates a fresh job


def test_gateway_lost_submit_ack_reuses_idempotency_key(project, monkeypatch):
    project.settings.tts.mode = "gateway"
    monkeypatch.setattr(TTSClient, "health", lambda self: {"ready": True})
    monkeypatch.setattr(VoiceStore, "verify_remote", lambda *args: None)
    keys = []

    def lost(self, payload, key):
        keys.append(key)
        raise TTSError("lost acknowledgement")

    monkeypatch.setattr(TTSClient, "submit", lost)
    for _ in range(2):
        with pytest.raises(TTSError):
            synthesize(project)
    assert keys[0] == keys[1]


def test_approvals_invalidated_by_edit(project):
    project.approve("script")
    project.approve("binding")
    assert project.approved("script") and project.approved("binding")
    script = project.script()
    script.lines[0].words += "编辑"
    project.save(script, project.cast())
    assert not project.approved("script")
    assert not project.approved("binding")


def test_cached_synthesis_does_not_need_remote_service(project, monkeypatch):
    synthesize(project)
    monkeypatch.setattr(
        TTSClient, "list_roles", lambda self: (_ for _ in ()).throw(TTSError("offline"))
    )
    monkeypatch.setattr(
        VoiceStore, "verify_remote", lambda *args: (_ for _ in ()).throw(TTSError("offline"))
    )
    assert synthesize(project)["status"] == "succeeded"


def test_single_line_retry_reports_other_missing_lines(project):
    result = synthesize(project, retry_line="L001")
    assert result["status"] == "partial"
    assert report(project, False)["reusable"] == 1


def test_voice_overwrite_invalidates_binding_confirmation(project):
    project.approve("script")
    project.approve("binding")
    atomic_bytes(VoiceStore(project.settings).path("讲述者", "平静"), b"new-voice")
    assert project.approved("script")
    assert not project.approved("binding")
