import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw, ImageStat

from speakvideo.audio import audio_current, build_audio
from speakvideo.jobs import synthesize
from speakvideo.media import font
from speakvideo.processing import ffmpeg, probe
from speakvideo.storage import read_json, write_json
from speakvideo.video import (
    SUBTITLE_COLORS,
    VideoOptions,
    VideoPaused,
    render_video,
    subtitle_lines,
    video_current,
)
from speakvideo.web import create_app
from speakvideo.workflow import extend_brief


def wait_task(client, task_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = next(t for t in client.get("/api/tasks").json() if t["id"] == task_id)
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.025)
    pytest.fail(f"任务未完成：{task}")


def prepare_video(project, **options):
    synthesize(project)
    build_audio(project)
    config = {
        "options": VideoOptions(
            width=640, height=360, fps=24, font_size=24, **options
        ).model_dump(),
        "assets": [],
    }
    write_json(project.root / "video.json", config)
    return config


def frame(path, destination, second):
    ffmpeg(["-ss", second, "-i", path, "-frames:v", "1", destination])
    return Image.open(destination).convert("RGB")


def test_video_defaults_to_portrait_and_exposes_stable_speaker_colors(project):
    with TestClient(create_app(project.settings)) as client:
        options = client.get("/api/projects/test").json()["video"]["options"]
        assert (options["width"], options["height"]) == (1080, 1920)
        assert client.get("/api/config").json()["subtitle_colors"] == SUBTITLE_COLORS


@pytest.mark.parametrize(
    "text",
    [
        "新闻里总说失业率百分之五，可我身边一抓一把没工作的。为什么会这样？",
        "先看「统计范围」，再区分【主动找工作的人】和暂时退出劳动力市场的人。",
        "讨论 OpenAI model 的使用方式，English words 应该尽量保持完整。",
    ],
)
def test_subtitle_wrapping_keeps_punctuation_inside_safe_width(text):
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    face = font(24)
    lines = subtitle_lines(draw, text, face, 220)
    assert lines and all(draw.textlength(line, font=face) <= 220 for line in lines)
    assert all(line[0] not in "，。！？；：、）】》」』" for line in lines)
    assert all(line[-1] not in "（【《「『" for line in lines)
    if "English" in text:
        assert any("English" in line for line in lines)
        assert any("OpenAI" in line for line in lines)


def test_real_portrait_video_has_blue_and_orange_subtitles_with_safe_margins(project):
    config = prepare_video(project)
    config["options"].update(width=480, height=854, font_size=28)
    write_json(project.root / "video.json", config)
    meta = render_video(project)
    path = project.root / "exports" / meta["files"]["final.mp4"]["path"]
    audio = read_json(project.root / "exports/audio.json")
    timeline = read_json(project.root / "exports" / audio["files"]["timeline.json"]["path"])
    pictures = [
        frame(path, project.root / f"speaker-{i}.png", row["start"] + 0.2)
        for i, row in enumerate(timeline["lines"][:2])
    ]
    predicates = [
        lambda r, g, b: b > 210 and b > r + 60 and g > r + 25,
        lambda r, g, b: r > 210 and r > b + 60 and g > b + 25,
    ]
    for picture, matches in zip(pictures, predicates):
        points = [
            (x, y)
            for y in range(picture.height)
            for x in range(picture.width)
            if matches(*picture.getpixel((x, y)))
        ]
        assert len(points) > 50
        assert min(x for x, _ in points) >= picture.width * 0.075
        assert max(x for x, _ in points) <= picture.width * 0.925
        assert max(y for _, y in points) < picture.height * 0.86
        # The lowest 14 percent contains no caption or opaque subtitle panel.
        bottom = picture.crop((0, round(picture.height * 0.86), picture.width, picture.height))
        assert max(ImageStat.Stat(bottom).stddev) < 1


def test_real_video_burns_chinese_subtitles_at_spoken_times_and_preserves_delivery(project):
    config = prepare_video(project)
    before_audio = (project.root / "exports/audio.json").read_bytes()
    stages = []
    meta = render_video(project, progress=lambda **kw: stages.append(kw["stage"]))
    path = project.root / "exports" / meta["files"]["final.mp4"]["path"]
    info = probe(path)
    assert {s["codec_name"] for s in info["streams"]} >= {"h264", "aac", "mov_text"}
    assert float(info["format"]["duration"]) == pytest.approx(meta["duration"], abs=0.1)
    assert video_current(project)
    assert render_video(project) == meta
    assert any("字幕图层" in stage for stage in stages)
    spoken = frame(path, project.root / "spoken.png", 0.2)
    audio = read_json(project.root / "exports/audio.json")
    timeline = read_json(project.root / "exports" / audio["files"]["timeline.json"]["path"])
    row = timeline["lines"][0]
    paused = frame(path, project.root / "pause.png", row["end"] + row["pause"] / 2)
    lower = (0, 250, 640, 360)
    # Burned-in text changes pixels only while the speaker is talking.
    assert (
        sum(ImageStat.Stat(ImageChops.difference(spoken.crop(lower), paused.crop(lower))).mean) > 2
    )
    config["options"]["burn_subtitles"] = False
    write_json(project.root / "video.json", config)
    assert not video_current(project)
    soft = render_video(project)
    soft_path = project.root / "exports" / soft["files"]["final.mp4"]["path"]
    unburned = frame(soft_path, project.root / "unburned.png", 0.2)
    assert (
        sum(ImageStat.Stat(ImageChops.difference(paused.crop(lower), unburned.crop(lower))).mean)
        < 1
    )
    assert path.is_file() and audio_current(project)
    assert (project.root / "exports/audio.json").read_bytes() == before_audio
    pointer = (project.root / "exports/video.json").read_bytes()
    config["options"]["font_size"] += 1
    write_json(project.root / "video.json", config)
    with pytest.raises(VideoPaused):
        render_video(project, cancelled=lambda: True)
    assert (project.root / "exports/video.json").read_bytes() == pointer
    assert not list((project.root / "audio").glob("video-*"))
    config["assets"] = [
        {"id": "bad", "path": "assets/bad.mp4", "kind": "video", "duration": 1, "name": "bad"}
    ]
    (project.root / "assets").mkdir(exist_ok=True)
    (project.root / "assets/bad.mp4").write_bytes(b"invalid video")
    write_json(project.root / "video.json", config)
    with pytest.raises(RuntimeError):
        render_video(project)
    assert (project.root / "exports/video.json").read_bytes() == pointer
    assert soft_path.is_file()


def test_video_api_upload_sort_loop_render_and_guards(project):
    prepare_video(project)
    source = project.root / "source.mp4"
    ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=320x240:r=24:d=0.6", "-c:v", "libx264", source])
    image = io.BytesIO()
    Image.new("RGB", (320, 240), "blue").save(image, format="PNG")
    with TestClient(create_app(project.settings)) as client:
        assert 'data-action="extend"' in client.get("/").text
        assert 'data-tab="video"' in client.get("/").text
        assert (
            client.post(
                "/api/projects/test/assets/video", files={"file": ("broken.mp4", b"bad")}
            ).status_code
            == 400
        )
        assert not list((project.root / "assets").iterdir())
        config = client.post(
            "/api/projects/test/assets/video", files={"file": ("red.mp4", source.read_bytes())}
        ).json()
        config = client.post(
            "/api/projects/test/assets/video", files={"file": ("blue.png", image.getvalue())}
        ).json()
        assert [a["kind"] for a in config["assets"]] == ["video", "image"]
        ids = [a["id"] for a in reversed(config["assets"])]
        body = {
            **config["options"],
            "asset_ids": ids,
            "image_seconds": 0.5,
            "burn_subtitles": False,
        }
        assert (
            client.put(
                "/api/projects/test/video", json={**body, "asset_ids": ["../bad"]}
            ).status_code
            == 400
        )
        assert (
            client.put(
                "/api/projects/test/video", json={**body, "asset_ids": [ids[0], ids[0]]}
            ).status_code
            == 400
        )
        assert (
            client.put("/api/projects/test/video", json={**body, "width": 641}).status_code == 422
        )
        assert client.put("/api/projects/test/video", json=body).status_code == 200
        task = client.post("/api/projects/test/actions", json={"action": "render"}).json()
        assert wait_task(client, task["id"])["status"] == "succeeded"
        data = client.get("/api/projects/test").json()
        assert data["video_current"] and data["audio_current"]
        meta = data["video_export"]
        path = project.root / "exports" / meta["files"]["final.mp4"]["path"]
        first = frame(path, project.root / "first.png", 0.1).getpixel((40, 40))
        second = frame(path, project.root / "second.png", 0.7).getpixel((40, 40))
        loop = frame(path, project.root / "loop.png", 1.25).getpixel((40, 40))
        assert first[2] > 200 and second[0] > 200 and loop[2] > 200
        download = client.get(
            "/api/projects/test/files/exports/" + meta["files"]["final.mp4"]["path"]
        )
        assert download.status_code == 200 and download.headers["content-type"] == "video/mp4"
        script = project.script()
        script.lines[0].words += "变更"
        project.save(script, project.cast())
        assert not client.get("/api/projects/test").json()["video_current"]
        assert (
            client.post("/api/projects/test/actions", json={"action": "render"}).status_code == 400
        )


def test_extend_preserves_prefix_inputs_history_and_original_on_error(project, monkeypatch):
    script = project.script()
    script.meta.brief = "p1：为什么会失业？\np2：先区分几种情况。\n"
    script.meta.guidance = "多讲生活中的例子"
    project.save(script, project.cast())
    calls = []

    def complete(self, system, user, *, json_output=False):
        calls.append((system, json.loads(user)))
        return "p1：那刚毕业找不到工作属于哪一种？\np2：可以先看岗位和经验的匹配。"

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", complete)
    result = extend_brief(project)
    assert result.startswith(script.meta.brief + "\n\n")
    assert calls[0][1]["brief"] == script.meta.brief
    assert calls[0][1]["guidance"] == script.meta.guidance
    assert calls[0][1]["cast"] == [c.model_dump() for c in script.cast]
    assert "只输出新增对话" in calls[0][0]
    assert project.script().lines == script.lines
    backup = list((project.root / "history").glob("*.script.json"))
    assert read_json(backup[0])["meta"]["brief"] == script.meta.brief
    before = (project.root / "script.json").read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("连接失败")

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", fail)
    with pytest.raises(RuntimeError, match="连接失败"):
        extend_brief(project)
    assert (project.root / "script.json").read_bytes() == before


def test_progress_uses_llm_stage_and_freezes_finished_line_counts(project, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def complete(self, system, user, **kwargs):
        entered.set()
        assert release.wait(5)
        return "p1：继续问。\np2：继续答。"

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", complete)
    with TestClient(create_app(project.settings)) as client:
        task = client.post("/api/projects/test/actions", json={"action": "brief"}).json()
        assert entered.wait(2)
        try:
            running = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
            assert running["progress"]["kind"] == "stage"
            assert "模型" in running["progress"]["stage"]
            assert "total" not in running["progress"]
            assert running["elapsed_seconds"] >= 0
            assert client.put("/api/projects/test/video", json={}).status_code == 409
        finally:
            release.set()
        finished = wait_task(client, task["id"])
        assert finished["status"] == "succeeded" and "字" in finished["result"]
        task = client.post("/api/projects/test/actions", json={"action": "synth"}).json()
        done = wait_task(client, task["id"])
        assert done["progress"]["total"] == done["progress"]["completed"] == 3
        script = project.script()
        script.lines = script.lines[:1]
        project.save(script, project.cast())
        historical = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
        assert historical["progress"]["total"] == 3
        script.meta.brief = ""
        project.save(script, project.cast())
        assert (
            client.post("/api/projects/test/actions", json={"action": "extend"}).status_code == 400
        )
