import base64
import hashlib
import io
import wave
import zipfile

import pytest
from fastapi.testclient import TestClient

from speakvideo.storage import atomic_bytes, read_json, write_json
from speakvideo.tts.client import TTSError
from speakvideo.voices import VoiceStore
from speakvideo.web import create_app
from worker.speakvideo_agent import delete_voice_file
from worker.voice_library import VoiceLibrary


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


def test_windows_crud_renames_reference_and_permanently_deletes_actual_pt(tmp_path):
    def install(root, files):
        for entry in files:
            path = root / "pt" / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(entry["content_base64"]))
        return []

    library = VoiceLibrary(tmp_path, install)
    asset = library.manage(
        {
            "action": "import",
            "role": "角色",
            "emotion": "平静",
            "content_base64": base64.b64encode(b"prompt").decode(),
        }
    )
    library.save_reference("角色", "平静", pcm()[0], "参考台词")
    renamed = library.manage(
        {
            "action": "edit",
            "role": "角色",
            "emotion": "平静",
            "sha256": asset["sha256"],
            "new_role": "角色",
            "new_emotion": "开心",
            "ref_text": "参考台词",
            "notes": "录音",
        }
    )
    assert renamed["has_reference"] and not library.path("角色", "平静").exists()
    with pytest.raises(ValueError, match="SHA256"):
        library.manage({"action": "delete", "role": "角色", "emotion": "开心", "sha256": "0" * 64})
    deleted = library.manage(
        {
            "action": "delete_permanent",
            "role": "角色",
            "emotion": "开心",
            "sha256": renamed["sha256"],
        }
    )
    assert deleted["deleted"] and not library.path("角色", "开心").exists()
    assert library.manage({"action": "list"})["assets"] == []
    assert deleted["permanent"] and "trash_id" not in deleted
    assert not (tmp_path / ".speakvideo" / "voice-trash").exists()
    assert not library.path("角色", "开心").with_suffix(".wav").exists()
    with pytest.raises(ValueError, match="no longer supported"):
        library.manage({"action": "restore", "id": "a" * 32})


def test_windows_hash_guard_and_idempotent_file_deletion(tmp_path):
    path = tmp_path / "pt" / "角色" / "角色_开心.pt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"prompt")
    with pytest.raises(ValueError, match="SHA256"):
        delete_voice_file(tmp_path, "角色", "开心", "0" * 64)
    digest = hashlib.sha256(b"prompt").hexdigest()
    assert delete_voice_file(tmp_path, "角色", "开心", digest)["deleted"]
    assert not path.exists()
    assert delete_voice_file(tmp_path, "角色", "开心", digest)["already_missing"]
    assert not (tmp_path / ".speakvideo" / "voice-trash").exists()


def test_remote_library_uses_windows_assets_and_blocks_unsupported_writes(project, monkeypatch):
    settings = project.settings
    settings.tts.mode = "gateway"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("archive/data.pkl", b"windows-prompt")
        bundle.writestr("archive/version", "3")
    content = buffer.getvalue()
    monkeypatch.setattr(
        "speakvideo.tts.client.TTSClient.list_roles", lambda self: {"Windows角色": ["开心"]}
    )
    monkeypatch.setattr(
        "speakvideo.tts.client.TTSClient.download_pt", lambda self, role, emotion: content
    )

    def unsupported(self, action, **values):
        raise TTSError("Windows PT 编辑接口尚未部署", 404)

    monkeypatch.setattr("speakvideo.tts.client.TTSClient.manage_voices", unsupported)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/voices").json() == {"Windows角色": ["开心"]}
        asset = client.get("/api/voices/assets").json()[0]
        assert asset["source"] == "windows" and not asset["editable"]
        assert asset["sha256"] == hashlib.sha256(content).hexdigest()
        assert client.delete("/api/voices/assets/Windows角色/开心").status_code == 409
        assert VoiceStore(settings).path("Windows角色", "开心").read_bytes() == content
        assert (settings.paths.voices / "讲述者" / "讲述者_平静.pt").exists()


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
            not path.exists() and (project.settings.paths.voices / "讲述者" / "讲述者_平静.pt").exists()
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


@pytest.mark.parametrize("old_worker", ["unsupported", "soft_ack"])
def test_gateway_permanent_delete_keeps_cache_without_permanent_ack(
    project, monkeypatch, old_worker
):
    settings = project.settings
    settings.tts.mode = "gateway"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("archive/data.pkl", b"remote-test-prompt")
        bundle.writestr("archive/version", "3")
    content = buffer.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    actions = []

    def manage(self, action, **values):
        actions.append(action)
        if action == "list":
            return {"assets": [{"role": "远端角色", "emotion": "平静", "sha256": digest}]}
        assert action == "delete_permanent" and values["sha256"] == digest
        if old_worker == "unsupported":
            raise TTSError("unknown voice action", 404)
        return {"deleted": True}  # A legacy soft-delete acknowledgement is insufficient.

    monkeypatch.setattr("speakvideo.tts.client.TTSClient.manage_voices", manage)
    monkeypatch.setattr("speakvideo.tts.client.TTSClient.download_pt", lambda *args: content)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/voices/assets").status_code == 200
        result = client.delete("/api/voices/assets/远端角色/平静?detach=true")
        assert result.status_code >= 400
        assert VoiceStore(settings).path("远端角色", "平静").read_bytes() == content
        assert "delete_permanent" in actions
        assert not VoiceStore(settings).deletions()
