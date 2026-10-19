import io
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from speakvideo.audio import assemble
from speakvideo.jobs import group_audio, report, synthesize
from speakvideo.script.engine import ScriptEngine
from speakvideo.script.prompts import DIALOGUE_QUALITY_CONTRACT
from speakvideo.script.quality import question_particles
from speakvideo.script.schema import ScriptPrompts
from speakvideo.storage import atomic_bytes, read_json, write_json
from speakvideo.tts.client import TTSClient
from speakvideo.tts.grouping import speech_groups
from speakvideo.web import create_app


def continuous(project):
    script, cast = project.script(), project.cast()
    script.lines = [script.lines[0], script.lines[2], script.lines[1]]
    project.save(script, cast)
    return script, cast


def frames(path_or_bytes):
    source = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else str(path_or_bytes)
    with wave.open(source, "rb") as audio:
        return audio.readframes(audio.getnframes())


def test_grouped_calls_preserve_script_and_continuous_master(project, monkeypatch, tmp_path):
    script, _ = continuous(project)
    original = TTSClient.synthesize
    calls, audio_bytes = [], []
    saved = (project.root / "script.json").read_bytes()

    def track(self, **kwargs):
        calls.append(kwargs)
        audio = original(self, **kwargs)
        audio_bytes.append(audio.wav_bytes)
        return audio

    monkeypatch.setattr(TTSClient, "synthesize", track)
    result = synthesize(project)
    assert len(calls) == 2
    assert calls[0]["text"] == script.lines[0].words + script.lines[1].words
    assert len(calls[0]["text"]) <= 200
    assert (project.root / "script.json").read_bytes() == saved
    assert result["lines"]["L001"]["group_id"] == result["lines"]["L003"]["group_id"]
    assert report(project, False)["reusable"] == 3
    assert (project.root / "audio/groups/L001.wav").read_bytes() == audio_bytes[0]
    assert not (project.root / "audio/segments/L001.wav").exists()
    assert not (project.root / "audio/segments/L003.wav").exists()
    destination = tmp_path / "assembled"
    destination.mkdir()
    timeline, raw = assemble(project, destination)
    assert len(timeline) == 2
    assert timeline[0]["line_ids"] == ["L001", "L003"]
    assert timeline[0]["words"] == calls[0]["text"]
    assert timeline[0]["pause"] == script.lines[1].pause_after_ms / 1000
    assert timeline[0]["end_frame"] == len(frames(audio_bytes[0])) // 2
    assert timeline[0]["alignment"] == "group"
    assert (destination / "subtitles.srt").read_text(encoding="utf-8").count(" --> ") >= 3
    assert frames(raw).startswith(frames(audio_bytes[0]))
    synthesize(project)
    assert len(calls) == 2


def test_edits_corruption_and_explicit_retry_regenerate_whole_group_only(project, monkeypatch):
    script, cast = continuous(project)
    original = TTSClient.synthesize
    calls = []

    def track(self, **kwargs):
        calls.append(kwargs["text"])
        return original(self, **kwargs)

    monkeypatch.setattr(TTSClient, "synthesize", track)
    synthesize(project)
    script.lines[1].words += "补充说明。"
    project.save(script, cast)
    assert report(project, False)["reusable"] == 1
    synthesize(project)
    assert len(calls) == 3
    synthesize(project, retry_line="L003")
    assert len(calls) == 4
    (project.root / "audio/groups/L001.wav").write_bytes(b"corrupt")
    synthesize(project)
    assert len(calls) == 5
    assert report(project, False)["reusable"] == 3
    script.lines[0].pause_after_ms = 500
    project.save(script, cast)
    synthesize(project)
    assert len(calls) == 5
    script.lines[0].pause_after_ms = 1200
    project.save(script, cast)
    synthesize(project)
    assert len(calls) == 7


def test_voice_slot_length_and_long_pause_are_batch_boundaries(project):
    script, cast = continuous(project)
    script.lines[1].mood = "serious"
    cast["p1"].mood_map["serious"] = "平静"
    assert len(speech_groups(script.lines, cast)) == 2  # moods may share a PT voice
    cast["p1"].mood_map["serious"] = "严肃"
    assert len(speech_groups(script.lines, cast)) == 3
    script.lines[1].mood = "normal"
    script.lines[0].words = "字" * 99 + "。"
    script.lines[1].words = "字" * 99 + "。"
    assert len(speech_groups(script.lines, cast)[0].text) == 200
    script.lines[1].words += "字"
    assert len(speech_groups(script.lines, cast)) == 3
    script.lines[0].words = "没有句号"
    script.lines[1].words = "下一句也没有"
    assert speech_groups(script.lines, cast)[0].text == "没有句号。下一句也没有"
    cast["p2"].role = cast["p1"].role
    assert len(speech_groups(script.lines, cast)) == 2  # never cross speaker slots


@pytest.mark.parametrize(
    "text, expected",
    [
        ("到底为什么？", "到底为什么呢？"),
        ("这样对吗？", "这样对吗？"),
        ("这是什么呢？", "这是什么呢？"),
        ("还有办法么？", "还有办法么？"),
        ("他说“为什么？”但我不认同。", "他说“为什么？”但我不认同。"),
        ("那怎么处理?这能行吗？", "那怎么处理呢?这能行吗？"),
    ],
)
def test_missing_question_particles_are_completed(text, expected):
    assert question_particles(text) == expected


def test_quality_rules_also_apply_to_custom_drafts_extensions_and_json(project):
    requests = []
    output = {**project.script().model_dump(), "meta": {}}
    output["lines"][0]["words"] = "为什么？"

    class Provider:
        def complete(self, system, user, *, json_output=False):
            requests.append(system)
            return (
                json.dumps(output)
                if json_output
                else "p1：为什么？\np2：原因讲清楚以后，就知道该怎么做了。"
            )

    engine = ScriptEngine(Provider())
    prompts = ScriptPrompts(brief="自由发挥", structure="自由发挥")
    assert "为什么呢？" in engine.brief("话题", [], prompts=prompts)
    script = project.script()
    script.meta.brief = "原来的结论。"
    script.meta.prompts = prompts
    assert engine.extend_brief(script).startswith("原来的结论。\n\n")
    result = engine.structure("话题", [], "原来的结论。", prompts=prompts)
    assert result.lines[0].words == "为什么呢？"
    assert all(DIALOGUE_QUALITY_CONTRACT in request for request in requests)
    assert "新增内容也必须回到原话题" in requests[1]


@pytest.mark.parametrize("length, accepted", [(2, False), (3, True), (200, True), (201, False)])
def test_manual_line_limits_at_save_and_preflight(project, length, accepted):
    script = project.script()
    script.lines[0].words = "字" * length
    with TestClient(create_app(project.settings)) as client:
        body = client.get("/api/projects/test").json()
        body["script"] = script.model_dump()
        response = client.put("/api/projects/test", json=body)
        assert (response.status_code == 200) is accepted
    project.save(script, project.cast())  # legacy scripts remain readable for correction
    assert report(project, False)["ok"] is accepted


def test_duration_formula_matches_sample_and_updates_pauses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend duration logic")
    source = Path("speakvideo/static/dialogue-timing.js").resolve()
    javascript = """
require(process.argv[1]);
const lines = Array.from({length: 66}, (_, i) => ({words: '字'.repeat(i === 65 ? 66 : 27), pause_after_ms: i === 65 ? 0 : 350}));
const timing = SpeakVideoTiming.estimate(lines);
if (timing.chars !== 1821 || Math.abs(timing.seconds - 382) > 1e-9) throw Error(JSON.stringify(timing));
if (SpeakVideoTiming.format(timing.seconds) !== '6 分 22 秒') throw Error('format');
lines[0].pause_after_ms += 1000;
if (Math.abs(SpeakVideoTiming.estimate(lines).seconds - 383) > 1e-9) throw Error('pause');
if (SpeakVideoTiming.format(119.9) !== '2 分 0 秒') throw Error('rounding');
if (SpeakVideoTiming.characterCount('  汉😀字  ') !== 3) throw Error('unicode');
"""
    subprocess.run([node, "-e", javascript, str(source)], check=True, capture_output=True)


def test_retry_task_counts_the_whole_speech_group(project):
    import time

    continuous(project)
    synthesize(project)
    with TestClient(create_app(project.settings)) as client:
        task = client.post(
            "/api/projects/test/actions", json={"action": "retry", "line_id": "L003"}
        ).json()
        assert task["line_ids"] == ["L001", "L003"]
        for _ in range(100):
            result = next(
                item for item in client.get("/api/tasks").json() if item["id"] == task["id"]
            )
            if result["status"] not in {"running", "queued"}:
                break
            time.sleep(0.01)
        assert result["status"] == "succeeded"
        assert result["progress"]["total"] == result["progress"]["completed"] == 2


def test_legacy_group_recovery_is_lossless_and_never_generates_or_mutates(
    project, monkeypatch, tmp_path
):
    import hashlib

    script, cast = continuous(project)
    synthesize(project)
    path = project.root / "audio/groups/L001.wav"
    original = path.read_bytes()
    pcm = frames(original)
    manifest = read_json(project.root / "manifest.json")
    # Model an old manifest with an arbitrary existing cut; recovery must simply rejoin it.
    boundary = len(pcm) // 4 * 2
    for line, chunk in zip(script.lines[:2], [pcm[:boundary], pcm[boundary:]]):
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(chunk)
        part = out.getvalue()
        atomic_bytes(project.root / "audio/segments" / f"{line.id}.wav", part)
        record = manifest["lines"][line.id]
        record.pop("audio_unit")
        record.update(alignment="estimated", sha256=hashlib.sha256(part).hexdigest())
    path.unlink()
    write_json(project.root / "manifest.json", manifest)
    saved = (project.root / "manifest.json").read_bytes()
    monkeypatch.setattr(
        TTSClient, "synthesize", lambda *a, **kw: pytest.fail("must not regenerate")
    )
    synthesize(project)
    assert report(project, False)["reusable"] == 3
    assert frames(group_audio(project, speech_groups(script.lines, cast)[0], cast, manifest)) == pcm
    with TestClient(create_app(project.settings)) as client:
        data = client.get("/api/projects/test").json()
        audio = client.get(
            "/api/projects/test/groups/L001/audio",
            params={"version": data["group_audio"]["L001"]["sha256"]},
        )
        assert audio.status_code == 200 and frames(audio.content) == pcm
        assert frames(client.get("/api/projects/test/lines/L003/audio").content) == pcm
    destination = tmp_path / "legacy-merged"
    destination.mkdir()
    timeline, raw = assemble(project, destination)
    assert len(timeline) == 2 and timeline[0]["alignment"] == "group"
    assert frames(raw).startswith(pcm)
    assert (project.root / "manifest.json").read_bytes() == saved


def test_group_audition_retry_versions_and_group_deduplication(project, monkeypatch):
    import threading

    from test_tts_concurrency import wav
    from test_video_workbench import wait_task

    continuous(project)
    synthesize(project)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def generate(self, **params):
        calls.append(params["text"])
        entered.set()
        assert release.wait(5)
        return wav(79)

    monkeypatch.setattr(TTSClient, "synthesize", generate)
    with TestClient(create_app(project.settings)) as client:
        before = client.get("/api/projects/test").json()
        assert before["speech_groups"][0]["line_ids"] == ["L001", "L003"]
        assert len(before["group_audio"]) == 2 and "line_audio" not in before
        old = before["group_audio"]["L001"]["sha256"]
        task = client.post(
            "/api/projects/test/actions", json={"action": "retry", "line_ids": ["L001", "L003"]}
        ).json()
        try:
            assert entered.wait(2)
            assert task["line_ids"] == ["L001", "L003"]
            duplicate = client.post(
                "/api/projects/test/actions", json={"action": "retry", "line_id": "L003"}
            ).json()
            assert duplicate["id"] == task["id"]
            running = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
            assert running["progress"]["running_ids"] == ["L001", "L003"]
            assert client.get("/api/projects/test/groups/L003/audio").status_code == 409
            assert client.get("/api/projects/test/groups/L002/audio").status_code == 200
        finally:
            release.set()
        assert wait_task(client, task["id"])["status"] == "succeeded"
        assert len(calls) == 1
        after = client.get("/api/projects/test").json()
        new = after["group_audio"]["L001"]["sha256"]
        assert new != old
        assert (
            client.get("/api/projects/test/groups/L001/audio", params={"version": old}).status_code
            == 409
        )
        for member in ["L001", "L003"]:
            audio = client.get(f"/api/projects/test/groups/{member}/audio", params={"version": new})
            assert audio.status_code == 200 and audio.content == wav(79).wav_bytes
        assert after["group_audio"]["L002"] == before["group_audio"]["L002"]


def test_long_group_subtitles_page_at_readable_size_without_touching_audio(tmp_path, monkeypatch):
    from PIL import Image

    from speakvideo import video

    monkeypatch.setattr(video, "ffmpeg", lambda *args, **kwargs: None)
    row = {
        "slot": "p2",
        "words": "连续语音需要分句显示，不能把整段文字同时塞进画面。" * 8,
        "start": 0.5,
        "end": 7.75,
        "duration": 7.25,
    }
    saved = dict(row)
    video.subtitle_track(
        {"lines": [row]},
        tmp_path,
        video.VideoOptions(width=640, height=360, font_size=36),
        8,
        lambda: None,
    )
    cues = (tmp_path / "subtitle-list.txt").read_text(encoding="utf-8")
    assert cues.count("file 'subtitle-") >= 8
    assert row == saved
    images = list(tmp_path.glob("subtitle-*.png"))
    for path in images:
        with Image.open(path) as image:
            # A constant two-line canvas; no whole-paragraph font shrinking.
            assert image.height == 2 * round(29 * 1.5) + 2 * round(29 * 0.45) + 36 + 4
            assert image.getbbox() is not None
