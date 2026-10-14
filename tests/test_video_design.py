import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from speakvideo.audio import audio_current, build_audio
from speakvideo.delivery import build_delivery, delivery_current
from speakvideo.jobs import synthesize
from speakvideo.processing import ffmpeg
from speakvideo.storage import read_json
from speakvideo.video import cover_current, render_video, video_current
from speakvideo.web import create_app


def png(color, transparent=False):
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0) if transparent else color)
    if transparent:
        ImageDraw.Draw(image).rectangle((30, 30, 69, 69), fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def body(config):
    return {
        **config["options"],
        "asset_ids": [a["id"] for a in config["assets"]],
        "overlays": [
            {k: layer[k] for k in ("id", "x", "y", "width", "opacity", "flip", "start", "end")}
            for layer in config["overlays"]
        ],
        "cover": config["cover"]["options"],
    }


def pixel(project, path, second, x, y):
    destination = project.root / f"check-{second}.png"
    ffmpeg(["-ss", second, "-i", path, "-frames:v", "1", destination])
    with Image.open(destination) as image:
        return image.convert("RGB").getpixel((x, y))


def test_alpha_timed_layers_and_cover_only_changes_preserve_video_and_audio(project):
    project.approve("script")
    project.approve("binding")
    synthesize(project)
    build_audio(project)
    project.approve("audio")
    with TestClient(create_app(project.settings)) as client:
        config = client.post(
            "/api/projects/test/assets/video", files={"file": ("background.png", png("#202080"))}
        ).json()
        config = client.post(
            "/api/projects/test/assets/overlay",
            files={"file": ("transparent.png", png("#ff0000", True))},
        ).json()
        config = client.post(
            "/api/projects/test/assets/overlay", files={"file": ("top.png", png("#00ff00"))}
        ).json()
        request = body(config)
        request.update(width=640, height=360, fps=24, burn_subtitles=False)
        request["overlays"][0].update(x=0, y=0, width=0.25, start=0.5, end=1.5)
        request["overlays"][1].update(x=0.1, y=0.15, width=0.05, start=0.75, end=1.25)
        request["cover"].update(enabled=True, frame_second=1, shade=0, title="")
        assert client.put("/api/projects/test/video", json=request).status_code == 200
        meta = render_video(project)
        path = project.root / "exports" / meta["files"]["final.mp4"]["path"]
        # Transparent corners retain the background; the opaque middle appears only in its interval.
        assert pixel(project, path, 1, 10, 10)[2] > 110
        before = pixel(project, path, 0.1, 90, 90)
        during = pixel(project, path, 0.6, 90, 90)
        after = pixel(project, path, 1.7, 90, 90)
        assert before[2] > 110 and after[2] > 110
        assert during[0] > 220 and during[2] < 30
        top = pixel(project, path, 1, 80, 70)
        assert top[1] > 220 and top[0] < 30
        cover = client.get("/api/projects/test").json()["cover_export"]
        assert video_current(project) and cover_current(project)
        project.approve("video")
        original_video = (project.root / "exports/video.json").read_bytes()
        original_audio = (project.root / "exports/audio.json").read_bytes()
        original_cover = (project.root / "exports/cover.json").read_bytes()
        with Image.open(project.root / "exports" / cover["files"]["cover.png"]["path"]) as image:
            assert image.size == (640, 360) and image.getpixel((80, 70))[1] > 220
        request["cover"].update(title="新的封面标题", subtitle="无需重做整段视频", color="#ffffff")
        assert client.put("/api/projects/test/video", json=request).status_code == 200
        assert video_current(project) and audio_current(project) and not cover_current(project)
        with pytest.raises(ValueError, match="封面"):
            project.approve("video")
        preview = client.post("/api/projects/test/cover-preview", json=request)
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/png"
        # Preview does not publish or change any export pointers.
        assert (project.root / "exports/cover.json").read_bytes() == original_cover
        task = client.post("/api/projects/test/actions", json={"action": "cover"}).json()
        from test_video_workbench import wait_task

        assert wait_task(client, task["id"])["status"] == "succeeded"
        assert cover_current(project)
        cover = read_json(project.root / "exports/cover.json")
        with (
            Image.open(io.BytesIO(preview.content)) as predicted,
            Image.open(project.root / "exports" / cover["files"]["cover.png"]["path"]) as exported,
        ):
            assert predicted.tobytes() == exported.tobytes()
        assert (project.root / "exports/video.json").read_bytes() == original_video
        assert (project.root / "exports/audio.json").read_bytes() == original_audio
        project.approve("video")
        delivery = build_delivery(project)
        with zipfile.ZipFile(
            project.root / "exports" / delivery["files"]["delivery.zip"]["path"]
        ) as archive:
            assert {"cover/cover.png", "cover/cover.jpg"}.issubset(archive.namelist())
        assert delivery_current(project)


def test_design_api_validates_images_ids_intervals_and_keeps_old_settings(project):
    with TestClient(create_app(project.settings)) as client:
        assert (
            client.post(
                "/api/projects/test/assets/overlay", files={"file": ("bad.png", b"broken")}
            ).status_code
            == 400
        )
        assert not list((project.root / "assets").iterdir())
        config = client.post(
            "/api/projects/test/assets/overlay", files={"file": ("valid.png", png("red"))}
        ).json()
        original = (project.root / "video.json").read_bytes()
        request = body(config)
        request["overlays"][0]["end"] = 0.1
        request["overlays"][0]["start"] = 1
        assert client.put("/api/projects/test/video", json=request).status_code == 422
        request = body(config)
        request["overlays"][0]["id"] = "f" * 32
        assert client.put("/api/projects/test/video", json=request).status_code == 400
        request = body(config)
        request["overlays"] *= 2
        assert client.put("/api/projects/test/video", json=request).status_code == 400
        assert (project.root / "video.json").read_bytes() == original
        request = body(config)
        request["cover"]["background"] = "image"
        assert client.put("/api/projects/test/video", json=request).status_code == 400
        # Legacy clients may save ordinary video settings without losing existing sticker layers.
        request.pop("cover")
        request.pop("overlays")
        assert (
            client.put("/api/projects/test/video", json=request).json()["overlays"]
            == config["overlays"]
        )
        config = client.post(
            "/api/projects/test/assets/cover", files={"file": ("cover.jpg", png("blue"))}
        ).json()
        assert config["cover"]["options"]["background"] == "image"
        preview = client.post("/api/projects/test/cover-preview", json=body(config))
        assert preview.status_code == 200
        assert not (project.root / "exports/cover.json").exists()
