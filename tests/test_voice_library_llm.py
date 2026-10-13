import io
import time
import wave
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from speakvideo.config import LLMSettings, load_settings
from speakvideo.connections import connection_status
from speakvideo.llm_config import public_llm, save_llm
from speakvideo.processing import ffmpeg
from speakvideo.script.engine import OpenAICompatibleProvider
from speakvideo.tts.client import TTSError
from speakvideo.web import create_app


def pt(content=b"tensor-data"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("archive/data.pkl", content)
        bundle.writestr("archive/version", "3")
    return buffer.getvalue()


def test_voice_library_replacement_invalidation_usage_guard_and_permanent_delete(project):
    project.approve("script")
    project.approve("binding")
    with TestClient(create_app(project.settings)) as client:
        assets = client.get("/api/voices/assets").json()
        original = next(a for a in assets if a["role"] == "讲述者")
        assert original["usage"] == [{"project": "test", "slot": "p1"}]
        assert client.delete("/api/voices/assets/讲述者/平静").status_code == 409
        assert (
            client.put(
                "/api/voices/assets/讲述者/平静", json={"role": "改名", "emotion": "平静"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/voices/import",
                data={"role": "讲述者", "emotion": "平静"},
                files={"file": ("x.pt", pt())},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/voices/import",
                data={"role": "讲述者", "emotion": "平静", "overwrite": "true"},
                files={"file": ("x.pt", pt())},
            ).status_code
            == 200
        )
        assert not project.approved("binding")
        assert client.get("/api/voices/trash").status_code == 404
        assert not (project.settings.paths.cache / "voice-trash").exists()

        added = client.post(
            "/api/voices/import",
            data={"role": "新角色", "emotion": "平静"},
            files={"file": ("x.pt", pt())},
        )
        assert added.status_code == 200
        edited = client.put(
            "/api/voices/assets/新角色/平静",
            json={"role": "改名", "emotion": "开心", "ref_text": "你好", "notes": "录音"},
        )
        assert edited.status_code == 200 and edited.json()["notes"] == "录音"
        assert client.get("/api/voices/assets/改名/开心/download").content == pt()
        deleted = client.delete("/api/voices/assets/改名/开心").json()
        assert client.get("/api/voices/assets/改名/开心").status_code == 400
        assert deleted["permanent"] and "trash_id" not in deleted
        assert client.post("/api/voices/trash/legacy/restore").status_code in {404, 405}
        assert not (project.settings.paths.cache / "voice-trash").exists()


def test_voice_import_rejects_invalid_pt_and_paths(project):
    with TestClient(create_app(project.settings)) as client:
        for name, payload in [("x.pt", b"not a torch file"), ("x.pt", b""), ("x.txt", pt())]:
            assert (
                client.post(
                    "/api/voices/import",
                    data={"role": "新", "emotion": "平静"},
                    files={"file": (name, payload)},
                ).status_code
                == 400
            )
        assert (
            client.post(
                "/api/voices/import",
                data={"role": "../escape", "emotion": "平静"},
                files={"file": ("x.pt", pt())},
            ).status_code
            == 400
        )


def test_browser_webm_and_m4a_recordings_normalize_to_previewable_pcm(project):
    with TestClient(create_app(project.settings)) as client:
        for suffix, codec in [("webm", "libopus"), ("m4a", "aac")]:
            source = project.settings.paths.cache / f"recording.{suffix}"
            ffmpeg(["-f", "lavfi", "-i", "sine=frequency=240:duration=0.6", "-c:a", codec, source])
            response = client.post(
                "/api/voices/reference", files={"file": (source.name, source.read_bytes())}
            )
            assert response.status_code == 200, response.text
            result = response.json()
            preview = client.get(result["preview_url"])
            assert preview.status_code == 200 and result["reference_id"].endswith(".wav")
            with wave.open(io.BytesIO(preview.content)) as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
        assert not list(project.settings.paths.cache.glob("*.upload.*"))
        assert (
            client.post("/api/voices/reference", files={"file": ("bad.webm", b"bad")}).status_code
            == 409
        )


def config_body():
    return {
        "active_provider": "one",
        "failover": True,
        "providers": [
            {
                "id": "one",
                "name": "供应商一",
                "base_url": "https://one.example/v1",
                "model": "model-a",
                "api_keys": ["key-a", "key-b"],
            },
            {
                "id": "two",
                "name": "供应商二",
                "base_url": "https://two.example/v1",
                "model": "model-b",
                "api_keys": ["key-c"],
            },
        ],
    }


def test_llm_config_keeps_secrets_private_preserves_keys_and_reloads(project):
    settings = project.settings
    public = save_llm(settings, config_body())
    assert "key-a" not in str(public) and public["providers"][0]["key_count"] == 2
    path = settings.paths.cache / "llm.local.json"
    assert path.stat().st_mode & 0o777 == 0o600
    reloaded = load_settings(settings.paths.root)
    assert reloaded.llm.providers[0].api_keys[0].get_secret_value() == "key-a"
    public["providers"][0]["name"] = "新名称"
    saved = save_llm(settings, public)
    assert saved["providers"][0]["name"] == "新名称"
    assert settings.llm.providers[0].api_keys[1].get_secret_value() == "key-b"
    public["providers"][0]["api_keys"] = []
    save_llm(settings, public)
    assert not settings.llm.providers[0].api_keys
    with TestClient(create_app(settings)) as client:
        bad = config_body()
        bad["providers"][0]["api_keys"] = ["VERY_SECRET_TEST_KEY"]
        bad["active_provider"] = "missing"
        response = client.put("/api/llm/config", json=bad)
        assert response.status_code == 400 and "VERY_SECRET_TEST_KEY" not in response.text
        assert "key-c" not in client.get("/api/llm/config").text


def test_llm_key_and_provider_failover_json_mode_and_no_secret_errors(monkeypatch):
    settings = LLMSettings.model_validate(config_body())
    calls = []
    client_class = httpx.Client

    def handler(request):
        calls.append((request.url.host, request.headers["Authorization"]))
        if request.url.host == "one.example":
            return httpx.Response(401 if len(calls) == 1 else 429, json={"error": "key-a secret"})
        assert b'"response_format"' in request.content and b'"json_object"' in request.content
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]},
        )

    monkeypatch.setattr(
        "speakvideo.script.engine.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler)),
    )
    provider = OpenAICompatibleProvider(settings)
    assert provider.complete("s", "u", json_output=True) == '{"ok":true}'
    assert calls == [
        ("one.example", "Bearer key-a"),
        ("one.example", "Bearer key-b"),
        ("two.example", "Bearer key-c"),
    ]
    settings.failover = False
    with pytest.raises(RuntimeError, match="连接失败") as error:
        OpenAICompatibleProvider(settings).complete("s", "u")
    assert "key-a" not in str(error.value)
    assert public_llm(settings)["providers"][0]["key_count"] == 2


def test_connection_status_distinguishes_auth_offline_and_network(project, monkeypatch):
    for code, gateway, worker in [
        (401, "connected", "unknown"),
        (503, "connected", "unknown"),
        (None, "unreachable", "unknown"),
    ]:

        def fail(self):
            raise TTSError("test", status_code=code)

        monkeypatch.setattr("speakvideo.connections.TTSClient.health", fail)
        status = connection_status(project.settings)
        assert not status["ready"] and status["gateway"] == gateway and status["worker"] == worker


def test_voice_preview_is_background_task_and_saves_playable_audio(project):
    with TestClient(create_app(project.settings)) as client:
        task = client.post(
            "/api/voices/actions",
            json={"action": "preview", "role": "讲述者", "emotion": "平静", "text": "你好"},
        ).json()
        for _ in range(100):
            result = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
            if result["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert result["status"] == "succeeded", result
        assert client.get("/api/previews/" + result["result"]).content.startswith(b"RIFF")
