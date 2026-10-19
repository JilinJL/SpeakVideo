import io
import json
import zipfile

import httpx
import pytest
from pydantic import ValidationError

from speakvideo.config import load_settings
from speakvideo.script.engine import ScriptEngine
from speakvideo.script.schema import ScriptLine, validate_ids
from speakvideo.storage import safe_name
from speakvideo.tts.client import TTSClient, TTSError, wav_info
from speakvideo.voices import VoiceStore


@pytest.mark.parametrize("name", ["../secret", ".", "..", "a/b", "a\\b", "a b", ""])
def test_safe_names(name):
    with pytest.raises(ValueError):
        safe_name(name)


@pytest.mark.parametrize("change", [{"mood": "happy"}, {"id": "../../x"}, {"pause_after_ms": -1}])
def test_line_schema_rejects_invalid(change):
    with pytest.raises(ValidationError):
        ScriptLine.model_validate({"id": "L001", "slot": "p1", "words": "hi", **change})


def test_duplicate_ids_slots_and_blank(project):
    script = project.script()
    script.lines[1].id = script.lines[0].id
    script.lines[0].words = " "
    script.cast[1].slot = script.cast[0].slot
    assert len(validate_ids(script)) >= 3


def test_config_overrides_and_secret_redaction(tmp_path, monkeypatch):
    (tmp_path / "config.local.toml").write_text('[tts]\nmode="direct"\n[llm]\nmodel="local"')
    monkeypatch.setenv("SPEAKVIDEO_TTS_MODE", "gateway")
    monkeypatch.setenv("SPEAKVIDEO_LLM_API_KEY", "secret-test")
    config = load_settings(tmp_path)
    assert config.tts.mode == "gateway"
    assert "secret-test" not in repr(config)


def test_invalid_wav_rejected():
    with pytest.raises(TTSError):
        wav_info(b'{"error":"bad"}')


def test_direct_api_matches_qwen_contract(project):
    project.settings.tts.mode = "direct"
    with TTSClient(project.settings) as client:

        def handler(request):
            if request.url.path == "/api/tts":
                payload = json.loads(request.content)
                assert payload == {
                    "text": "你好",
                    "role": "讲述者",
                    "emotion": "平静",
                    "language": "Chinese",
                    "mode": "file",
                }
                with TTSClient(project.settings.model_copy(deep=True)):
                    pass
                project.settings.tts.mode = "demo"
                with TTSClient(project.settings) as tone:
                    data = tone.synthesize(text="你好", role="讲述者").wav_bytes
                return httpx.Response(200, content=data)
            return httpx.Response(200, json={"roles": {"讲述者": ["平静"]}})

        client._client.close()
        client._client = httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        assert client.list_roles() == {"讲述者": ["平静"]}
        assert client.synthesize(text="你好", role="讲述者").sample_rate == 24000


def test_error_retains_service_detail(project):
    with TTSClient(project.settings) as client:
        client._client.close()
        client._client = httpx.Client(
            base_url="http://test",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(503, json={"detail": "CUDA out of memory"})
            ),
        )
        with pytest.raises(TTSError, match="CUDA out of memory"):
            client._request("POST", "/api/tts")


def test_two_step_engine_preserves_original_brief(project):
    script = project.script()

    class Provider:
        def __init__(self):
            self.calls = []

        def complete(self, system, user, *, json_output=False):
            self.calls.append(json_output)
            return (
                json.dumps({**script.model_dump(), "meta": {}})
                if json_output
                else "原样阐述\n带包袱"
            )

    provider = Provider()
    engine = ScriptEngine(provider)
    brief = engine.brief("话题", ["词"])
    result = engine.structure("话题", ["词"], brief)
    assert provider.calls == [False, True]
    assert result.meta.brief == brief
    assert result.meta.topic == "话题"


def test_voice_bundle_checksums(project):
    assets = VoiceStore(project.settings).assets()
    project.settings.tts.mode = "gateway"
    with TTSClient(project.settings) as client:

        def handler(request):
            content = request.content
            start = content.index(b"PK\x03\x04")
            end = content.rindex(b"PK\x05\x06") + 22
            with zipfile.ZipFile(io.BytesIO(content[start:end])) as z:
                manifest = json.loads(z.read("manifest.json"))
                assert len(manifest) == 2
                assert z.read(manifest[0]["path"])
            return httpx.Response(200, json={"voices": manifest})

        client._client.close()
        client._client = httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        assert len(client.sync_voices(assets)["voices"]) == 2


def test_gateway_rejects_cross_origin_audio(project):
    project.settings.tts.mode = "gateway"
    with TTSClient(project.settings) as client:
        client._client.close()
        client._client = httpx.Client(
            base_url=client._base,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"status": "succeeded", "audio_url": "https://evil.example/audio"}
                )
            ),
        )
        with pytest.raises(TTSError, match="跨域"):
            client.wait_job("abc")


def test_gateway_accepts_full_sha256_job_id_and_rejects_path_traversal(project):
    project.settings.tts.mode = "gateway"
    with TTSClient(project.settings) as client:
        job_id = "j_" + "a" * 64
        client._client.close()
        client._client = httpx.Client(
            base_url=client._base,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"id": r.url.path.rsplit("/", 1)[-1], "status": "running"}
                )
            ),
        )
        assert client.poll(job_id)["id"] == job_id
        with pytest.raises(TTSError, match="任务 ID"):
            client.poll("../../secret")


def test_voice_factory_uses_upload_path_and_downloads_pt(project, tmp_path):
    project.settings.tts.mode = "direct"
    project.settings.tts.local_pt_root = project.settings.paths.voices
    reference = tmp_path / "ref.wav"
    reference.write_bytes(b"reference-bytes")
    paths = []
    with TTSClient(project.settings) as client:

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/api/pt/upload":
                assert b"reference-bytes" in request.content
                return httpx.Response(200, json={"path": "/worker/uploads/reference.wav"})
            if request.url.path == "/api/pt/create":
                body = json.loads(request.content)
                assert body["ref_audio"] == "/worker/uploads/reference.wav"
                assert body["ref_text"] == "你好。"
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/api/pt/download":
                return httpx.Response(200, content=b"pt-result")
            raise AssertionError(request.url.path)

        client._client.close()
        client._client = httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        output = VoiceStore(project.settings).create(
            client, reference, "示例角色", "平静", "你好。"
        )
        assert output.read_bytes() == b"pt-result"
        assert paths == ["/api/pt/upload", "/api/pt/create", "/api/pt/download"]
        with pytest.raises(ValueError, match="已存在"):
            VoiceStore(project.settings).create(client, reference, "示例角色", "平静", "你好。")
