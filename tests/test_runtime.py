import json
import os
import struct
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from speakvideo.config import Paths, RuntimeSettings, Settings, load_settings
from speakvideo.inference.environment import profile
from speakvideo.inference.manager import RuntimeManager, service_token
from speakvideo.inference.models import download, identity, model_path, validate_model
from speakvideo.jobs import parameters
from speakvideo.storage import atomic_bytes, lock, read_json, write_json
from speakvideo.web import create_app


def complete(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in (
        "config.json",
        "preprocessor_config.json",
        "generation_config.json",
        "tokenizer_config.json",
        "vocab.json",
        "speech_tokenizer/config.json",
        "speech_tokenizer/preprocessor_config.json",
    ):
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"test":true}')
    (path / "merges.txt").write_text("a b")
    header = json.dumps({"a": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    for name in ("model.safetensors", "speech_tokenizer/model.safetensors"):
        (path / name).write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)


def wait(client, key):
    for _ in range(100):
        result = next(t for t in client.get("/api/tasks").json() if t["id"] == key)
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.02)
    raise AssertionError("task timeout")


def test_fresh_workbench_without_inference_or_llm(tmp_path):
    s = load_settings(tmp_path)
    assert s.tts.mode == "managed" and s.runtime.model == "base-small" and not s.llm.endpoints()
    with TestClient(create_app(s)) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/voices").json() == {
            role: ["示例06B"] for role in ("熊大", "熊二", "喜羊羊", "懒羊羊")
        }
        assert c.get("/api/projects").json() == []
        state = c.get("/api/runtime").json()
        assert not state["installed"] and not state["process"]["health"]["ready"]
        assert not any(m["complete"] for m in state["models"])
        assert c.get("/api/worker/capabilities").status_code == 404
        assert c.post("/api/runtime/actions", json={"action": "bogus"}).status_code == 400


def test_core_import_does_not_import_torch():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from speakvideo.web import create_app; assert 'torch' not in sys.modules; assert 'qwen_tts' not in sys.modules",
        ],
        check=True,
    )


@pytest.mark.parametrize(
    "device,model,system,machine,rows,expected",
    [
        ("auto", "base-small", "Darwin", "arm64", [], "macos"),
        ("cuda:0", "base-small", "Windows", "AMD64", [["0", "id", "GPU", "8192", "6.1"]], "cu118"),
        ("auto", "base", "Linux", "x86_64", [["0", "id", "GPU", "8192", "8.6"]], "cu128"),
        ("auto", "base", "Windows", "AMD64", [["0", "id", "GPU", "8192", "12.0"]], "cu128"),
    ],
)
def test_platform_profiles(device, model, system, machine, rows, expected, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    assert profile(device, model, system=system, machine=machine, gpu_rows=rows) == expected


@pytest.mark.parametrize(
    "system,machine,device,rows",
    [
        ("Darwin", "x86_64", "auto", []),
        ("Linux", "aarch64", "auto", []),
        ("Darwin", "arm64", "cuda:0", []),
        ("Windows", "AMD64", "mps", []),
        ("Linux", "x86_64", "auto", []),
        ("Linux", "x86_64", "auto", [["0", "id", "GPU", "2048", "8.6"]]),
        ("Linux", "x86_64", "auto", [["0", "id", "GPU", "8192", "5.2"]]),
    ],
)
def test_unsupported_hardware_never_falls_back_cpu(system, machine, device, rows, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    with pytest.raises(ValueError):
        profile(device, system=system, machine=machine, gpu_rows=rows)


def test_models_validate_nested_weights_and_corruption(tmp_path):
    complete(tmp_path)
    assert validate_model(tmp_path) == []
    p = tmp_path / "speech_tokenizer/model.safetensors"
    p.write_bytes(p.read_bytes()[:-1])
    assert any("speech_tokenizer/model.safetensors" in x for x in validate_model(tmp_path))


def test_shard_index_rejects_escape(tmp_path):
    complete(tmp_path)
    (tmp_path / "model.safetensors.index.json").write_text(
        '{"weight_map":{"a":"../outside.safetensors"}}'
    )
    assert any("越界" in x for x in validate_model(tmp_path))


def test_offline_missing_model_does_not_attempt_download(tmp_path):
    s = Settings(paths=Paths(root=tmp_path), runtime=RuntimeSettings(offline=True))
    with pytest.raises(ValueError, match="离线"):
        download(s, "base-small")
    assert not model_path(s).exists()
    complete(model_path(s))
    download(s, "base-small")


def test_runtime_identity_invalidates_audio_after_model_change(project):
    project.settings.tts.mode = "managed"
    path = model_path(project.settings)
    complete(path)
    line = project.script().lines[0]
    first = parameters(project, line, project.cast())
    (path / "model.safetensors").write_bytes((path / "model.safetensors").read_bytes() + b"x")
    second = parameters(project, line, project.cast())
    assert first["runtime_identity"] != second["runtime_identity"]
    project.settings.runtime.model = "base"
    assert identity(project.settings) != first["runtime_identity"]


def test_token_private_and_stable(tmp_path):
    s = Settings(paths=Paths(root=tmp_path))
    token = service_token(s)
    assert len(token) > 32 and service_token(s) == token
    if os.name != "nt":
        assert (tmp_path / "runtime/service-token").stat().st_mode & 0o777 == 0o600


def test_runtime_refuses_to_own_or_stop_unrelated_process(tmp_path):
    import psutil

    s = Settings(paths=Paths(root=tmp_path))
    manager = RuntimeManager(s)
    write_json(
        manager.receipt,
        {"pid": os.getpid(), "created": psutil.Process().create_time(), "instance": "fake"},
    )
    assert manager.owned_process() is None
    manager.stop()
    assert not manager.receipt.exists()


def test_port_conflict_does_not_terminate_occupant(tmp_path, monkeypatch):
    from speakvideo.inference import manager as module

    s = Settings(paths=Paths(root=tmp_path))
    manager = RuntimeManager(s)
    manager.python.parent.mkdir(parents=True)
    manager.python.touch()
    complete(model_path(s))
    monkeypatch.setattr(module, "port_available", lambda port: False)
    with pytest.raises(ValueError, match="端口"):
        manager.start()
    assert not manager.receipt.exists()


def test_external_shared_model_removal_refused(tmp_path):
    directory = tmp_path / "shared"
    s = Settings(paths=Paths(root=tmp_path), runtime=RuntimeSettings(models_dir=directory))
    complete(model_path(s))
    with pytest.raises(ValueError, match="外部模型"):
        RuntimeManager(s).action("remove", key="base-small")
    assert model_path(s).exists()


def test_gateway_config_requires_explicit_branch_migration(tmp_path):
    (tmp_path / "config.local.toml").write_text('[tts]\nmode="gateway"\n')
    with pytest.raises(ValueError, match="divide"):
        load_settings(tmp_path)
    assert "gateway" in (tmp_path / "config.local.toml").read_text()


def test_save_runtime_persists_and_rejects_running_process(tmp_path, monkeypatch):
    from speakvideo.inference import manager as module

    monkeypatch.setattr(module, "profile", lambda *a: "macos")
    s = Settings(paths=Paths(root=tmp_path))
    manager = RuntimeManager(s)
    manager.save_config(
        {"model": "base", "port": 8123, "models_dir": str(tmp_path / "shared"), "offline": True}
    )
    loaded = load_settings(tmp_path)
    assert loaded.runtime.model == "base" and loaded.tts.base_url == "http://127.0.0.1:8123"
    assert loaded.runtime.offline
    monkeypatch.setattr(manager, "owned_process", lambda: object())
    with pytest.raises(ValueError, match="停止"):
        manager.save_config({"model": "base-small"})
    assert read_json(manager.root / "config.json")["model"] == "base"


def test_runtime_tasks_run_separately_persist_and_guard_gpu_actions(project, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def action(self, action, **kwargs):
        kwargs["log"]("可恢复的准备过程")
        entered.set()
        assert release.wait(3)
        return "done"

    monkeypatch.setattr(RuntimeManager, "action", action)
    with TestClient(create_app(project.settings)) as c:
        task = c.post("/api/runtime/actions", json={"action": "install"}).json()
        assert entered.wait(2)
        try:
            assert c.get("/api/projects").status_code == 200
            assert c.post("/api/runtime/actions", json={"action": "start"}).status_code == 409
            assert c.post("/api/projects/test/actions", json={"action": "synth"}).status_code == 409
            assert c.put("/api/runtime/config", json={}).status_code == 409
        finally:
            release.set()
        result = wait(c, task["id"])
        assert result["status"] == "succeeded"
        assert result["log"][0]["message"] == "可恢复的准备过程"
    assert (
        read_json(project.settings.paths.cache / f"task-{task['id']}.json")["status"] == "succeeded"
    )


def test_cross_process_lock_is_nonblocking_and_atomic_write(tmp_path):
    path = tmp_path / "project.lock"
    with lock(path):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; from speakvideo.storage import lock;\nwith lock(Path(__import__('sys').argv[1])): pass",
                str(path),
            ],
            capture_output=True,
        )
        assert result.returncode and "RuntimeError" in result.stderr.decode()
    atomic_bytes(tmp_path / "state.json", b"first")
    atomic_bytes(tmp_path / "state.json", b"second")
    assert (tmp_path / "state.json").read_bytes() == b"second"


def test_runtime_validates_model_keys_and_cross_site_requests(tmp_path):
    s = Settings(paths=Paths(root=tmp_path))
    with TestClient(create_app(s)) as c:
        assert (
            c.post(
                "/api/runtime/models/actions", json={"action": "download", "key": "../escape"}
            ).status_code
            == 400
        )
        assert (
            c.post(
                "/api/runtime/actions",
                json={"action": "install"},
                headers={"Origin": "https://elsewhere.example"},
            ).status_code
            == 403
        )
        assert c.post("/api/tts/mode", json={"mode": "gateway"}).status_code == 422


def test_same_size_weight_corruption_fails_hash_validation(tmp_path):
    from speakvideo.storage import sha256

    complete(tmp_path)
    weight = tmp_path / "model.safetensors"
    write_json(
        tmp_path / ".speakvideo-model.json", {"sha256": {"model.safetensors": sha256(weight)}}
    )
    weight.write_bytes(weight.read_bytes()[:-1] + b"x")
    assert validate_model(tmp_path) == []
    assert any("SHA256" in e for e in validate_model(tmp_path, verify_hashes=True))


def test_partial_environment_is_not_reported_installed(tmp_path):
    s = Settings(paths=Paths(root=tmp_path))
    m = RuntimeManager(s)
    m.python.parent.mkdir(parents=True)
    m.python.touch()
    assert not m.status()["installed"]


@pytest.mark.parametrize(
    "url",
    [
        "file://localhost/model",
        "http://user:secret@localhost:8001",
        "http://localhost:8001?token=secret",
    ],
)
def test_direct_requires_plain_loopback_http_url(tmp_path, url):
    with TestClient(create_app(load_settings(tmp_path))) as c:
        assert c.post("/api/tts/mode", json={"mode": "direct", "local_url": url}).status_code == 400
