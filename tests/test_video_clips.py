import asyncio
import uuid

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from test_video_workbench import frame, prepare_video, wait_task

from speakvideo.audio import audio_current
from speakvideo.processing import ffmpeg, probe
from speakvideo.storage import read_json
from speakvideo.video import background_frame, render_video, video_current
from speakvideo.web import create_app


def source_video(project, long=False):
    path = project.root / "source.mp4"
    duration, fps = (600, 1) if long else (1, 24)
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s=320x240:r={fps}:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=320x240:r={fps}:d={duration}",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            path,
        ]
    )
    return path


def request_body(config, clips):
    return {
        **config["options"],
        "asset_ids": [a["id"] for a in config["assets"]],
        "clips": clips,
        "burn_subtitles": False,
    }


def clip(asset, start, end, speed=1):
    return {"id": uuid.uuid4().hex, "asset_id": asset, "start": start, "end": end, "speed": speed}


def test_twenty_minute_video_multiple_ranges_speed_loop_cover_and_audio(project):
    prepare_video(project)
    source = source_video(project, long=True)
    before_audio = (project.root / "exports/audio.json").read_bytes()
    with TestClient(create_app(project.settings)) as client:
        config = client.post(
            "/api/projects/test/assets/video", files={"file": ("long.mp4", source.read_bytes())}
        ).json()
        asset = config["assets"][0]
        assert asset["duration"] == pytest.approx(1200)
        # First use a later blue range, sped up to half a second; then an earlier red range.
        clips = [clip(asset["id"], 605, 606, 2), clip(asset["id"], 2, 2.5)]
        response = client.put("/api/projects/test/video", json=request_body(config, clips))
        assert response.status_code == 200, response.text
        assert client.get("/api/projects/test").json()["video"]["clips"] == clips
        config = response.json()
        assert background_frame(project, config, 0.2).getpixel((40, 40))[2] > 200
        assert background_frame(project, config, 0.7).getpixel((40, 40))[0] > 200
        assert background_frame(project, config, 1.2).getpixel((40, 40))[2] > 200
        meta = render_video(project)
        target = project.root / "exports" / next(v["path"] for n, v in meta["files"].items() if n.endswith(".mp4"))
        for second, channel in [(0.2, 2), (0.7, 0), (1.2, 2)]:
            assert (
                frame(target, project.root / f"check-{second}.png", second).getpixel((40, 40))[
                    channel
                ]
                > 200
            )
        assert float(probe(target)["format"]["duration"]) == pytest.approx(
            meta["duration"], abs=0.1
        )
        assert video_current(project) and audio_current(project)
        # Range streaming supports seeking without downloading the entire original.
        result = client.get(
            "/api/projects/test/files/" + asset["path"], headers={"Range": "bytes=0-63"}
        )
        assert result.status_code == 206 and len(result.content) == 64
        assert result.headers["accept-ranges"] == "bytes"
        clips[0]["start"] = 607
        clips[0]["end"] = 608
        assert (
            client.put("/api/projects/test/video", json=request_body(config, clips)).status_code
            == 200
        )
        assert not video_current(project) and audio_current(project)
        assert (project.root / "exports/audio.json").read_bytes() == before_audio
        assert target.exists() and source.exists()


def test_clip_validation_empty_sequence_and_compatible_preview(project):
    prepare_video(project)
    source = source_video(project)
    with TestClient(create_app(project.settings)) as client:
        config = client.post(
            "/api/projects/test/assets/video", files={"file": ("short.mp4", source.read_bytes())}
        ).json()
        asset = config["assets"][0]
        good = clip(asset["id"], 0.2, 0.8)
        for fields, status in [
            ({"start": -0.1}, 422),
            ({"end": 0.1}, 422),
            ({"end": 3}, 400),
            ({"speed": 0}, 422),
            ({"asset_id": "f" * 32}, 400),
        ]:
            bad = {**good, **fields}
            assert (
                client.put("/api/projects/test/video", json=request_body(config, [bad])).status_code
                == status
            )
            assert read_json(project.root / "video.json")["clips"][0]["start"] == 0
        assert (
            client.put(
                "/api/projects/test/video", json=request_body(config, [good, good])
            ).status_code
            == 400
        )
        assert (
            client.put("/api/projects/test/video", json=request_body(config, [])).status_code == 200
        )
        latest = client.get("/api/projects/test").json()["video"]
        assert latest["clips"] == [] and len(latest["assets"]) == 1
        assert background_frame(project, latest, 0).getpixel((0, 0)) == (24, 38, 62)
        task = client.post(f"/api/projects/test/video-preview/{asset['id']}").json()
        assert wait_task(client, task["id"])["status"] == "succeeded"
        ready = client.post(f"/api/projects/test/video-preview/{asset['id']}").json()
        preview = project.root / ready["path"]
        assert probe(preview)["streams"][0]["codec_name"] == "h264"
        assert float(probe(preview)["format"]["duration"]) == pytest.approx(2, abs=0.05)
        latest = client.get("/api/projects/test").json()["video"]
        assert latest["clips"] == []
        assert latest["assets"][0]["preview_path"] == ready["path"]
        assert client.post("/api/projects/test/video-preview/" + "f" * 32).status_code == 404


def test_real_upload_over_previous_500mb_limit_streams_to_disk(project):
    source = source_video(project)
    with source.open("r+b") as f:
        f.truncate(501 * 1024 * 1024)
    app = create_app(project.settings)
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", "") == "/api/projects/{name}/assets/{kind}"
    )
    with source.open("rb") as stream:
        upload = UploadFile(filename="large.mp4", file=stream)
        config = asyncio.run(endpoint("test", "video", upload))
    asset = config["assets"][0]
    assert asset["bytes"] == 501 * 1024 * 1024
    assert (project.root / asset["path"]).stat().st_size == asset["bytes"]
    assert asset["duration"] == pytest.approx(2)
