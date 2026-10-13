import asyncio
import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from speakvideo.connections import connection_status
from speakvideo.tts.client import TTSClient, TTSError
from speakvideo.voices import VoiceStore
from speakvideo.web import create_app
from worker.base_agent import AgentSettings
from worker.interface_docs import build_interface_document
from worker.speakvideo_agent import SpeakVideoAgent


def gateway(project):
    project.settings.tts.mode = "gateway"
    project.settings.tts.worker_id = "example-worker"
    return project.settings


def client(settings, handler):
    value = TTSClient(settings)
    value._client.close()
    value._client = httpx.Client(
        base_url="https://gateway.test", transport=httpx.MockTransport(handler)
    )
    return value


def test_capabilities_reads_envelope_and_falls_back_to_old_gateway(project):
    document = build_interface_document("example-worker")
    calls = []

    def old(request):
        calls.append(request.url.path)
        if request.url.path.endswith("capabilities"):
            return httpx.Response(404)
        assert request.url.params["include_docs"] == "true"
        return httpx.Response(200, json={"ready": True, "interface_docs": document})

    with client(gateway(project), old) as value:
        result = value.capabilities()
        assert result["source"] == "worker.health"
        assert not result["gateway_capabilities"]
        assert "voice.manage" in result["rpc"] and "delete_permanent" in result["voice_actions"]
        assert result["limits"]["pt_bytes"] == 32 * 1024 * 1024
    assert calls == ["/v1/worker/capabilities", "/v1/health"]
    with client(
        project.settings,
        lambda r: httpx.Response(
            200,
            json={
                "type": "rpc.result",
                "request_id": "unique",
                "result": document,
            },
        ),
    ) as value:
        assert value.capabilities()["gateway_capabilities"]


@pytest.mark.parametrize(
    "key,value", [("protocol", "2"), ("schema_version", "2"), ("worker_id", "other"), ("rpc", None)]
)
def test_incompatible_capabilities_rejected(project, key, value):
    document = {**build_interface_document("example-worker"), key: value}
    with client(gateway(project), lambda r: httpx.Response(200, json={"result": document})) as c:
        with pytest.raises(TTSError):
            c.capabilities()


def test_auth_failure_does_not_fall_back_or_claim_unsupported(project):
    paths = []

    def denied(request):
        paths.append(request.url.path)
        return httpx.Response(401, json={"detail": "unauthorized"})

    with client(gateway(project), denied) as value:
        with pytest.raises(TTSError) as error:
            value.capabilities()
        assert error.value.status_code == 401
    assert paths == ["/v1/worker/capabilities"]


def test_loading_model_health_is_connected_and_metrics_still_available(project, monkeypatch):
    settings = gateway(project)
    real = httpx.Client

    def handle(request):
        if request.url.path == "/v1/health":
            return httpx.Response(
                503,
                json={
                    "ready": False,
                    "worker_connected": True,
                    "model": "loading",
                    "capabilities": {"voice_manage": True},
                },
            )
        assert request.url.path == "/v1/worker/metrics"
        return httpx.Response(200, json={"queue_depth": 0})

    monkeypatch.setattr(
        "speakvideo.tts.client.httpx.Client",
        lambda **kw: real(base_url="https://gateway.test", transport=httpx.MockTransport(handle)),
    )
    status = connection_status(settings)
    assert not status["ready"] and status["worker"] == "connected"
    with TestClient(create_app(settings)) as web:
        result = web.get("/api/runtime").json()
        assert result["metrics_status"] == "available"
        assert result["metrics"]["queue_depth"] == 0


def test_permanent_delete_preserves_worker_acknowledgement(project):
    settings = gateway(project)

    def reply(request):
        body = __import__("json").loads(request.content)
        assert body["action"] == "delete_permanent"
        return httpx.Response(200, json={"deleted": True, "permanent": True})

    with client(settings, reply) as value:
        assert value.manage_voices("delete_permanent", role="角色", emotion="平静") == {
            "deleted": True,
            "permanent": True,
        }


def pt():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        bundle.writestr("archive/data.pkl", b"data")
        bundle.writestr("archive/version", "3")
    return out.getvalue()


def test_remote_inventory_hash_mismatch_keeps_previous_cache(project):
    store = VoiceStore(gateway(project))
    target = store.path("示例角色", "平静")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-asset")

    class Remote:
        def manage_voices(self, action):
            return {"assets": [{"role": "示例角色", "emotion": "平静", "sha256": "a" * 64}]}

        def download_pt(self, role, emotion):
            return pt()

    with pytest.raises(ValueError, match="哈希不一致"):
        store.remote_inventory(Remote())
    assert target.read_bytes() == b"old-asset"


def test_empty_remote_inventory_is_editable(project, monkeypatch):
    gateway(project)
    monkeypatch.setattr(TTSClient, "manage_voices", lambda self, action, **kw: {"assets": []})
    with TestClient(create_app(project.settings)) as web:
        assert web.get("/api/voices/assets").json() == []
        assert web.get("/api/voices/status").json()["editable"] is True


def test_worker_capabilities_do_not_require_model_or_expose_credentials(tmp_path):
    settings = AgentSettings(root=tmp_path, worker_id="example-worker", token="PRIVATE_TOKEN")
    agent = SpeakVideoAgent(settings)
    agent.rpc_tasks = set()

    async def exercise():
        await agent.handle_message({"type": "worker.capabilities", "request_id": "cap-unique"})
        await asyncio.gather(*agent.rpc_tasks)
        result = agent._outbox.popleft()
        assert result["request_id"] == "cap-unique" and result["type"] == "rpc.result"
        assert result["result"] == build_interface_document("example-worker")
        assert "PRIVATE_TOKEN" not in str(result)
        await agent.qwen.close()

    try:
        asyncio.run(exercise())
    finally:
        agent.journal.close()
