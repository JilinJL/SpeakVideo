import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_video_design import body, png
from test_video_workbench import frame

from speakvideo.audio import build_audio
from speakvideo.jobs import synthesize
from speakvideo.processing import ffmpeg
from speakvideo.storage import read_json
from speakvideo.video import cover_current, render_video, video_current, video_signature
from speakvideo.video_glow import glow_opacity, glow_track, speaking_intervals
from speakvideo.web import create_app


@pytest.mark.parametrize("fps", [24, 30, 60])
def test_glow_track_does_not_shift_speech_boundaries(tmp_path, fps):
    intervals = [(0.23, 0.71), (0.71, 0.91), (1.15, 1.62)]
    image = Image.new("RGBA", (8, 8), (112, 186, 255, 200))
    track = glow_track(image, intervals, tmp_path, "clock", fps, 2, lambda: None)
    raw = tmp_path / "frames.rgba"
    ffmpeg(["-i", track, "-pix_fmt", "rgba", "-f", "rawvideo", raw])
    pixels = raw.read_bytes()
    assert len(pixels) == 2 * fps * 8 * 8 * 4
    for frame_index in range(2 * fps):
        actual = pixels[frame_index * 8 * 8 * 4 + 3]
        expected = round(200 * round(glow_opacity(frame_index / fps, intervals) * 16) / 16)
        assert actual == expected, (frame_index, actual, expected)


def test_intervals_merge_contiguous_lines_but_preserve_silence_and_layer_bounds():
    timeline = {
        "lines": [
            {"slot": "p1", "start": 0, "end": 1},
            {"slot": "p1", "start": 1, "end": 2},
            {"slot": "p2", "start": 2, "end": 3},
            {"slot": "p1", "start": 3.5, "end": 4},
        ]
    }
    layer = {"speaker_slot": "p1", "start": 0.5, "end": 3.75}
    intervals = speaking_intervals(timeline, layer)
    assert intervals == [(0.5, 2), (3.5, 3.75)]
    assert glow_opacity(1, intervals) == 1
    assert glow_opacity(2.1, intervals) == 0
    assert 0 < glow_opacity(0.55, intervals) < 1
    assert 0 < glow_opacity(3.7, intervals) < 1
    layer["speaking_glow"] = False
    assert speaking_intervals(timeline, layer) == []


def test_export_always_shows_both_stickers_and_only_active_speaker_glows(project):
    project.approve("script")
    project.approve("binding")
    synthesize(project)
    audio = build_audio(project)
    audio_bytes = (project.root / "exports/audio.json").read_bytes()
    timeline = read_json(project.root / "exports" / audio["files"]["timeline.json"]["path"])
    rows = timeline["lines"]
    with TestClient(create_app(project.settings)) as client:
        for name in ["blue.png", "orange.png"]:
            config = client.post(
                "/api/projects/test/assets/overlay", files={"file": (name, png("white", True))}
            ).json()
        # New defaults must not invalidate existing static sticker exports.
        old = {
            **config,
            "overlays": [
                {
                    key: value
                    for key, value in layer.items()
                    if key not in {"speaker_slot", "speaking_glow", "glow_strength"}
                }
                for layer in config["overlays"]
            ],
        }
        assert video_signature(project, old) == video_signature(project, config)
        request = body(config)
        request.update(width=640, height=360, fps=30, burn_subtitles=False)
        for index, layer in enumerate(request["overlays"]):
            layer.update(
                x=0.1 + index * 0.5,
                y=0.1,
                width=0.25,
                speaker_slot=f"p{index + 1}",
                speaking_glow=True,
                glow_strength=1,
            )
        p1_time = (rows[0]["start"] + rows[0]["end"]) / 2
        p2_time = (rows[1]["start"] + rows[1]["end"]) / 2
        silence = rows[0]["end"] + 0.2
        request["cover"].update(enabled=True, frame_second=p1_time, shade=0, title="")
        assert client.put("/api/projects/test/video", json=request).status_code == 200
        saved = client.get("/api/projects/test").json()
        assert saved["video"]["overlays"][1]["speaker_slot"] == "p2"
        assert saved["video_timeline"] == timeline
        meta = render_video(project)
        path = (
            project.root
            / "exports"
            / next(v["path"] for n, v in meta["files"].items() if n.endswith(".mp4"))
        )
        pictures = [
            frame(path, project.root / f"glow-check-{i}.png", second)
            for i, second in enumerate([p1_time, p2_time, silence])
        ]
        for image in pictures:
            # Both white sticker interiors remain visible even when the other speaks or pauses.
            assert min(image.getpixel((140, 116))) > 230
            assert min(image.getpixel((460, 116))) > 230
        orange, blue, quiet = pictures
        # Sample just outside the opaque silhouettes, where only the alpha glow can appear.
        o = orange.getpixel((110, 116))
        q1 = quiet.getpixel((110, 116))
        b = blue.getpixel((430, 116))
        q2 = quiet.getpixel((430, 116))
        assert o[0] > q1[0] + 35 and o[0] > o[2] + 35
        assert b[2] > q2[2] + 35 and b[2] > b[0] + 35
        assert max(abs(a - b) for a, b in zip(orange.getpixel((430, 116)), q2)) < 8
        assert max(abs(a - b) for a, b in zip(blue.getpixel((110, 116)), q1)) < 8
        assert video_current(project) and cover_current(project)
        preview = client.post("/api/projects/test/cover-preview", json=request)
        cover = read_json(project.root / "exports/cover.json")
        with (
            Image.open(io.BytesIO(preview.content)) as predicted,
            Image.open(project.root / "exports" / cover["files"]["cover.png"]["path"]) as exported,
        ):
            assert predicted.tobytes() == exported.tobytes()
            assert predicted.getpixel((110, 116))[0] > q1[0] + 35
        assert (project.root / "exports/audio.json").read_bytes() == audio_bytes
        request["overlays"][0]["speaker_slot"] = "p3"
        assert client.put("/api/projects/test/video", json=request).status_code == 422
        request["overlays"][0]["speaker_slot"] = "p1"
        request["overlays"][0]["glow_strength"] = 1.1
        assert client.put("/api/projects/test/video", json=request).status_code == 422
        # An audio edit removes the old clock from previews and invalidates its glowing cover.
        script, bindings = project.script(), project.cast()
        script.lines[0].words += "测试修改"
        project.save(script, bindings)
        assert client.get("/api/projects/test").json()["video_timeline"] == {"lines": []}
        assert not cover_current(project)
