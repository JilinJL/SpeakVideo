import io
import wave

import pytest
from fastapi.testclient import TestClient

from speakvideo.storage import atomic_bytes, read_json, write_json
from speakvideo.voices import VoiceStore
from speakvideo.web import create_app


def pcm():
    data = b"\x01\x00" * 12000 + b"\x02\x00" * 12000 + b"\x03\x00" * 12000
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, 2, 24000, 0, "NONE", "none"))
        writer.writeframes(data)
    return output.getvalue(), data


def test_crop_uses_exact_pcm_selection_preserves_source_and_validates_bounds(project):
    content, samples = pcm()
    with TestClient(create_app(project.settings)) as client:
        source = client.post("/api/voices/reference", files={"file": ("base.wav", content)}).json()
        original = client.get(source["preview_url"]).content
        crop = client.post(
            "/api/voices/reference/crop",
            json={"reference_id": source["reference_id"], "start": 0.5, "end": 1},
        ).json()
        assert crop["duration"] == 0.5 and crop["reference_id"] != source["reference_id"]
        with wave.open(io.BytesIO(client.get(crop["preview_url"]).content)) as reader:
            assert reader.readframes(reader.getnframes()) == samples[24000:48000]
        assert client.get(source["preview_url"]).content == original
        for start, end in [(1, 0.5), (0, 3), (0, 0.01)]:
            assert (
                client.post(
                    "/api/voices/reference/crop",
                    json={"reference_id": source["reference_id"], "start": start, "end": end},
                ).status_code
                == 400
            )
        assert (
            client.post(
                "/api/voices/reference/crop",
                json={"reference_id": "../private.wav", "start": 0, "end": 1},
            ).status_code
            == 422
        )


def test_bound_delete_detaches_only_target_moods_and_removes_real_files(project):
    project.approve("script")
    project.approve("binding")
    store = VoiceStore(project.settings)
    path = store.path("讲述者", "平静")
    atomic_bytes(path.with_suffix(".wav"), pcm()[0])
    with TestClient(create_app(project.settings)) as client:
        assert client.delete("/api/voices/assets/讲述者/平静").status_code == 409
        result = client.delete("/api/voices/assets/讲述者/平静?detach=true")
        assert result.status_code == 200 and result.json()["deleted"]
        assert not path.exists() and not path.with_suffix(".wav").exists()
        assert "讲述者" not in client.get("/api/voices").json()
        assert project.approved("script") and not project.approved("binding")
        assert not project.cast()["p1"].mood_map
        assert result.json()["permanent"] and "trash_id" not in result.json()
        assert not (project.settings.paths.cache / "voice-trash").exists()


def test_local_qwen_mode_reads_real_qwen_directory_not_speakvideo_copy(project, tmp_path):
    root = tmp_path / "qwen" / "pt"
    path = root / "本机" / "本机_平静.pt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"local-prompt")
    project.settings.tts.mode = "direct"
    project.settings.tts.local_pt_root = root
    with TestClient(create_app(project.settings)) as client:
        assert client.get("/api/voices").json() == {"本机": ["平静"]}
        assert client.get("/api/voices/assets/本机/平静/download").content == b"local-prompt"
        assert client.delete("/api/voices/assets/本机/平静").json()["deleted"]
        assert (
            not path.exists()
            and (project.settings.paths.voices / "讲述者" / "讲述者_平静.pt").exists()
        )
    assert not read_json(project.settings.paths.cache / "voice-deletions.json", [])


def test_missing_old_project_does_not_break_task_console(project):
    write_json(
        project.settings.paths.cache / "task-old.json",
        {
            "id": "old",
            "label": "generate",
            "project": "removed",
            "status": "succeeded",
            "result": "旧任务",
        },
    )
    with TestClient(create_app(project.settings)) as client:
        task = client.get("/api/tasks").json()[0]
        assert task["project_missing"] and task["result"] == "旧任务"
        assert client.get("/api/runtime").status_code == 200


def test_local_mode_discovers_qwen_root_and_never_falls_back_to_cached_voices(
    project, tmp_path, monkeypatch
):
    root = tmp_path / "qwen" / "pt"
    root.mkdir(parents=True)
    project.settings.tts.mode = "direct"

    class Response:
        def json(self):
            return {"dir": str(root)}

    def request(self, method, path):
        assert method == "GET" and path == "/api/pt/list"
        return Response()

    monkeypatch.setattr("speakvideo.tts.client.TTSClient._request", request)
    assert VoiceStore(project.settings).root == root
    assert VoiceStore(project.settings).list_roles() == {}
    assert (project.settings.paths.voices / "讲述者" / "讲述者_平静.pt").exists()
    project.settings.tts.local_pt_root = tmp_path / "missing"
    with pytest.raises(ValueError, match="PT 目录不可用"):
        VoiceStore(project.settings)


def test_source_change_invalidates_binding_approval_without_changing_script(project):
    project.approve("script")
    project.approve("binding")
    project.settings.tts.base_url = "http://127.0.0.1:8002"
    assert project.approved("script") and not project.approved("binding")
    with TestClient(create_app(project.settings)) as client:
        approvals = client.get("/api/projects/test").json()["workflow"]["approvals"]
        assert approvals["script"] and not approvals["binding"]
