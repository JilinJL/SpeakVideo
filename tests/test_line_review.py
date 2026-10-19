import threading

from fastapi.testclient import TestClient
from test_tts_concurrency import wav
from test_video_workbench import wait_task

from speakvideo.audio import audio_current, build_audio
from speakvideo.jobs import report, synthesize
from speakvideo.storage import read_json
from speakvideo.tts.client import TTSClient
from speakvideo.web import create_app


def test_lines_play_before_merge_refresh_versions_and_reject_stale_audio(project, monkeypatch):
    synthesize(project)
    with TestClient(create_app(project.settings)) as client:
        data = client.get("/api/projects/test").json()
        assert not data["audio_export"] and len(data["group_audio"]) == 3
        old = data["group_audio"]["L001"]["sha256"]
        route = "/api/projects/test/lines/L001/audio"
        response = client.get(route, params={"version": old})
        assert response.status_code == 200 and response.content.startswith(b"RIFF")
        assert response.headers["content-type"] == "audio/wav"
        assert "immutable" in response.headers["cache-control"]
        monkeypatch.setattr(TTSClient, "synthesize", lambda *args, **kwargs: wav(23))
        task = client.post(
            "/api/projects/test/actions", json={"action": "retry", "line_id": "L001"}
        ).json()
        assert wait_task(client, task["id"])["status"] == "succeeded"
        fresh = client.get("/api/projects/test").json()["group_audio"]["L001"]["sha256"]
        assert fresh != old
        assert client.get(route, params={"version": old}).status_code == 409
        assert client.get(route, params={"version": fresh}).content == wav(23).wav_bytes
        assert not (project.root / "exports/audio.json").exists()
        script = project.script()
        script.lines[0].words += "修改"
        project.save(script, project.cast())
        assert "L001" not in client.get("/api/projects/test").json()["group_audio"]
        assert client.get(route).status_code == 409
        assert client.get("/api/projects/test/lines/UNKNOWN/audio").status_code == 404


def test_retries_queue_deduplicate_and_keep_actual_running_progress(project, monkeypatch):
    synthesize(project)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def generate(self, **params):
        calls.append(params["text"])
        if params["text"] == project.script().lines[0].words:
            entered.set()
            assert release.wait(5)
        return wav(len(calls))

    monkeypatch.setattr(TTSClient, "synthesize", generate)
    with TestClient(create_app(project.settings)) as client:

        def retry(ids):
            result = client.post(
                "/api/projects/test/actions", json={"action": "retry", "line_ids": ids}
            )
            assert result.status_code == 200, result.text
            return result.json()

        first = retry(["L001"])
        try:
            assert entered.wait(2)
            second = retry(["L002"])
            assert second["status"] == "queued"
            assert retry(["L001"])["id"] == first["id"]
            third = retry(["L002", "L003"])
            assert third["line_ids"] == ["L003"]
            tasks = {task["id"]: task for task in client.get("/api/tasks").json()}
            assert tasks[first["id"]]["progress"]["running_ids"] == ["L001"]
            assert tasks[first["id"]]["progress"]["completed"] == 0
            assert tasks[first["id"]]["progress"]["completed_ids"] == []
            assert tasks[second["id"]]["status"] == "queued"
            # Other audio can be auditioned while synthesis owns the project lock.
            assert client.get("/api/projects/test/lines/L003/audio").status_code == 200
            assert (
                client.post("/api/projects/test/actions", json={"action": "merge"}).status_code
                == 409
            )
            assert (
                client.post(
                    "/api/projects/test/actions", json={"action": "retry", "line_ids": ["missing"]}
                ).status_code
                == 400
            )
        finally:
            release.set()
        for task in [first, second, third]:
            result = wait_task(client, task["id"])
            assert result["status"] == "succeeded", result
            assert result["progress"]["completed"] == result["progress"]["total"] == 1
        assert calls == [line.words for line in project.script().lines]
        assert report(project, False)["reusable"] == 3


def test_multiselect_retries_run_in_parallel_and_only_change_selected_groups(project, monkeypatch):
    synthesize(project)
    before = read_json(project.root / "manifest.json")
    both_started, mutex = threading.Event(), threading.Lock()
    calls = []

    def generate(self, **params):
        with mutex:
            calls.append(params["text"])
            if len(calls) == 2:
                both_started.set()
        assert both_started.wait(2), "Selected groups must fill the concurrency window"
        return wav(99)

    monkeypatch.setattr(TTSClient, "synthesize", generate)
    with TestClient(create_app(project.settings)) as client:
        task = client.post(
            "/api/projects/test/actions",
            json={
                "action": "retry",
                "line_ids": ["L001", "L003", "L001"],
                "concurrency": 2,
            },
        ).json()
        result = wait_task(client, task["id"])
        assert result["status"] == "succeeded", result
        assert result["line_ids"] == ["L001", "L003"]
        assert result["progress"]["completed_ids"] == ["L001", "L003"]
    after = read_json(project.root / "manifest.json")
    assert len(calls) == 2
    assert after["lines"]["L002"] == before["lines"]["L002"]
    assert after["lines"]["L001"]["attempts"] == before["lines"]["L001"]["attempts"] + 1


def test_web_workflow_stops_at_line_review_until_user_merges(project):
    project.approve("script")
    project.approve("binding")
    with TestClient(create_app(project.settings)) as client:
        task = client.post("/api/projects/test/actions", json={"action": "run"}).json()
        result = wait_task(client, task["id"])
        assert result["status"] == "succeeded" and "整组" in result["result"]
        assert report(project, False)["reusable"] == 3
        assert not (project.root / "exports/audio.json").exists()
        task = client.post("/api/projects/test/actions", json={"action": "merge"}).json()
        assert wait_task(client, task["id"])["status"] == "succeeded"
        assert audio_current(project)


def test_retry_invalidates_master_and_preserves_it_until_manual_merge(project, monkeypatch):
    synthesize(project)
    build_audio(project)
    original = (project.root / "exports/audio.json").read_bytes()
    monkeypatch.setattr(TTSClient, "synthesize", lambda *args, **kwargs: wav(45))
    synthesize(project, retry_lines=["L002"])
    assert (project.root / "exports/audio.json").read_bytes() == original
    assert not audio_current(project)


def test_pausing_queued_retry_does_not_generate_it_or_block_following_task(project, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def generate(self, **params):
        calls.append(params["text"])
        entered.set()
        assert release.wait(5)
        return wav(7)

    monkeypatch.setattr(TTSClient, "synthesize", generate)
    with TestClient(create_app(project.settings)) as client:
        first = client.post(
            "/api/projects/test/actions", json={"action": "retry", "line_id": "L001"}
        ).json()
        try:
            assert entered.wait(2)
            second = client.post(
                "/api/projects/test/actions", json={"action": "retry", "line_id": "L002"}
            ).json()
            assert client.post(f"/api/tasks/{second['id']}/pause").status_code == 200
        finally:
            release.set()
        assert wait_task(client, first["id"])["status"] == "succeeded"
        assert wait_task(client, second["id"])["status"] == "paused"
        assert calls == [project.script().lines[0].words]
