import os

from fastapi.testclient import TestClient

from speakvideo.project import Project
from speakvideo.storage import write_json
from speakvideo.web import create_app


def test_home_lists_recent_projects_and_guides_unfinished_work(project):
    blank = Project(project.settings, "new")
    blank.create("一个新问题")
    draft = Project(project.settings, "draft")
    draft.create("继续追问")
    script = draft.script()
    script.meta.brief = "先问为什么，再用具体例子解释。"
    draft.save(script, draft.cast())
    # Control file times to check recent activity, including saved media options.
    for i, p in enumerate((project, draft, blank)):
        for file in p.root.glob("*.json"):
            os.utime(file, (100 + i, 100 + i))
    write_json(project.root / "video.json", {"options": {}, "assets": []})
    os.utime(project.root / "video.json", (200, 200))
    with TestClient(create_app(project.settings)) as client:
        rows = client.get("/api/home").json()["projects"]
        assert [row["name"] for row in rows] == ["test", "new", "draft"]
        assert rows[0]["line_count"] == 3
        assert rows[0]["stage"] == "台词待定稿"
        assert rows[1]["stage"] == "从话题开始"
        assert rows[2]["stage"] == "草稿待整理"
        project.approve("script")
        row = next(r for r in client.get("/api/home").json()["projects"] if r["name"] == "test")
        assert row["tab"] == "binding"


def test_home_requires_existing_delivery_and_prioritizes_script_changes(project):
    project.approve("script")
    project.approve("binding")
    exports = project.root / "exports"
    exports.mkdir(exist_ok=True)
    write_json(exports / "video.json", {"files": {"成片.mp4": {"path": "missing.mp4"}}})
    with TestClient(create_app(project.settings)) as client:

        def summary():
            return client.get("/api/home").json()["projects"][0]

        assert summary()["stage"] == "可以开始配音"
        (exports / "master.mp3").write_bytes(b"test delivery")
        write_json(exports / "audio.json", {"files": {"master.mp3": {"path": "master.mp3"}}})
        assert summary()["stage"] == "已有完整音频"
        (exports / "成片.mp4").write_bytes(b"test delivery")
        write_json(exports / "video.json", {"files": {"成片.mp4": {"path": "成片.mp4"}}})
        assert summary()["tab"] == "video"
        script = project.script()
        script.lines[0].words += "继续讨论。"
        project.save(script, project.cast())
        assert summary()["tab"] == "script"
