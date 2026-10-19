"""Own only SpeakVideo child processes; persist installation and lifecycle state."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from importlib.metadata import requires
from pathlib import Path

import httpx
import psutil
from filelock import FileLock

from ..config import RuntimeSettings, Settings
from ..storage import atomic_bytes, lock, read_json, write_json
from .environment import inspect, port_available, profile
from .models import MODELS, identity, model_path, validate_model

CODE_ROOT = Path(__file__).resolve().parents[2]


def service_token(settings):
    path = settings.paths.root / "runtime" / "service-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path.with_suffix(".lock")), timeout=5):
        if not path.exists():
            atomic_bytes(path, secrets.token_urlsafe(32).encode())
            path.chmod(0o600)
        return path.read_text(encoding="utf-8").strip()


class RuntimeManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.paths.root / "runtime"
        self.python = (
            self.root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        self.receipt = self.root / "service.json"

    def environment(self):
        env = os.environ.copy()
        env.update(
            SPEAKVIDEO_ROOT=str(self.settings.paths.root),
            PYTHONUTF8="1",
            PYTHONUNBUFFERED="1",
            PYTHONPATH=str(CODE_ROOT),
            HF_HUB_DISABLE_TELEMETRY="1",
            HF_HOME=str(self.root / "hf-cache"),
        )
        return env

    def owned_process(self):
        state = read_json(self.receipt, {})
        try:
            proc = psutil.Process(state["pid"])
            args = proc.cmdline()
            if (
                abs(proc.create_time() - state["created"]) < 0.1
                and "speakvideo.inference.worker" in args
                and state["instance"] in args
                and proc.status() != psutil.STATUS_ZOMBIE
            ):
                return proc
        except (KeyError, psutil.Error):
            pass
        return None

    def health(self):
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.get(
                    f"http://127.0.0.1:{self.settings.runtime.port}/api/health",
                    headers={"Authorization": "Bearer " + service_token(self.settings)},
                )
                if response.is_success:
                    return response.json()
        except (httpx.HTTPError, ValueError):
            pass
        return {"ready": False}

    def status(self):
        env = inspect(self.settings)
        proc = self.owned_process()
        models = []
        for key, name in MODELS.items():
            path = model_path(self.settings, key)
            errors = validate_model(path)
            models.append(
                {
                    "key": key,
                    "repo": "Qwen/" + name,
                    "path": str(path),
                    "complete": not errors,
                    "errors": errors[:12],
                    "bytes": sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
                    if path.exists()
                    else 0,
                }
            )
        log_path = self.root / "service.log"
        logs = []
        if log_path.exists():
            with log_path.open("rb") as handle:
                handle.seek(max(0, log_path.stat().st_size - 16000))
                logs = handle.read().decode("utf-8", errors="replace").splitlines()[-60:]
        return {
            "config": self.settings.runtime.model_dump(mode="json"),
            "environment": env,
            "installed": self.python.exists() and (self.root / "installed.json").is_file(),
            "installation": read_json(self.root / "installed.json", {}),
            "models": models,
            "process": {
                "owned": proc is not None,
                "pid": proc.pid if proc else None,
                "health": self.health() if proc else {"ready": False},
            },
            "logs": logs,
            "model_identity": identity(self.settings),
        }

    def run_command(self, command, log=print, cancelled=lambda: False):
        self.root.mkdir(parents=True, exist_ok=True)
        # File tailing keeps cancellation responsive even while an installer emits no lines.
        output = self.root / "operation.log"
        with output.open("wb") as target:
            proc = subprocess.Popen(
                list(map(str, command)),
                cwd=CODE_ROOT,
                env=self.environment(),
                stdout=target,
                stderr=subprocess.STDOUT,
            )
            with output.open("rb") as reader:
                while True:
                    chunk = reader.readline()
                    if chunk:
                        log(chunk.decode("utf-8", errors="replace").rstrip())
                    elif proc.poll() is not None:
                        break
                    elif cancelled():
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        raise RuntimeError("操作已停止；可重新提交恢复")
                    else:
                        time.sleep(0.2)
        if proc.returncode:
            raise RuntimeError(f"操作失败（退出码 {proc.returncode}），请查看安装日志并重试")

    def uv(self):
        for path in [
            self.settings.paths.root / ".uv" / ("uv.exe" if os.name == "nt" else "uv"),
            CODE_ROOT / ".uv" / ("uv.exe" if os.name == "nt" else "uv"),
        ]:
            if path.exists():
                return str(path)
        found = shutil.which("uv")
        if not found:
            raise ValueError("未找到 uv，请从项目安装脚本启动")
        return found

    def install(self, log=print, cancelled=lambda: False):
        if self.owned_process():
            raise ValueError("请先停止本机推理服务再修复环境")
        selected = profile(self.settings.runtime.device, self.settings.runtime.model)
        (self.root / "installed.json").unlink(missing_ok=True)
        uv = self.uv()
        if not self.python.exists():
            self.run_command(
                [uv, "venv", "--python", "3.11", self.python.parent.parent], log, cancelled
            )
        constraints = Path(__file__).parent / "constraints" / f"{selected}.txt"
        torch_index = (
            "https://download.pytorch.org/whl/" + selected
            if selected.startswith("cu")
            else "https://pypi.org/simple"
        )
        self.run_command(
            [
                uv,
                "pip",
                "install",
                "--python",
                self.python,
                "--index-url",
                torch_index,
                "-c",
                constraints,
                "torch",
                "torchaudio",
                "torchvision",
            ],
            log,
            cancelled,
        )
        self.run_command(
            [
                uv,
                "pip",
                "install",
                "--python",
                self.python,
                "-c",
                constraints,
                *(
                    [CODE_ROOT]
                    if (CODE_ROOT / "pyproject.toml").is_file()
                    else [
                        requirement
                        for requirement in (requires("speakvideo") or [])
                        if "extra ==" not in requirement
                    ]
                ),
                "qwen-tts==0.1.1",
                "transformers==4.57.3",
                "accelerate==1.12.0",
                "huggingface-hub>=0.34,<1",
                "modelscope>=1.22,<2",
                "soundfile>=0.13,<1",
                "numpy>=1.26,<3",
            ],
            log,
            cancelled,
        )
        self.run_command(
            [self.python, "-m", "speakvideo.inference.worker", "--probe"], log, cancelled
        )
        write_json(
            self.root / "installed.json",
            {
                "profile": selected,
                "python": "3.11",
                "runtime_version": 1,
                "installed_at": time.time(),
            },
        )
        log("推理环境安装并通过设备检查")

    def save_config(self, values):
        if self.owned_process():
            raise ValueError("请先停止本机推理服务再修改运行配置")
        updated = RuntimeSettings.model_validate(values)
        # Check the proposed platform/device before replacing a working configuration.
        profile(updated.device, updated.model)
        write_json(self.root / "config.json", updated.model_dump(mode="json"))
        self.settings.runtime = updated
        if self.settings.tts.mode == "managed":
            self.settings.tts.base_url = f"http://127.0.0.1:{updated.port}"
            self.settings.tts.local_pt_root = self.settings.paths.voices
        return updated.model_dump(mode="json")

    def start(self, log=print, cancelled=lambda: False):
        if self.settings.tts.mode != "managed":
            raise ValueError("请先切换至内置托管模式")
        if self.owned_process():
            if self.health().get("ready"):
                return
            raise ValueError("推理进程存在但尚未就绪；查看日志或先停止服务")
        if not self.python.exists():
            raise ValueError("请先安装推理环境")
        errors = validate_model(model_path(self.settings))
        if errors:
            raise ValueError("请先下载或导入完整 Base 模型：" + "; ".join(errors[:4]))
        if not port_available(self.settings.runtime.port):
            raise ValueError("推理端口被其他程序占用，请修改端口；不会终止其他程序")
        self.run_command(
            [self.python, "-m", "speakvideo.inference.worker", "--probe"], log, cancelled
        )
        instance = secrets.token_hex(16)
        env = self.environment()
        env["SPEAKVIDEO_SERVICE_TOKEN"] = service_token(self.settings)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "service.log").open("ab") as target:
            proc = subprocess.Popen(
                [str(self.python), "-m", "speakvideo.inference.worker", "--instance", instance],
                cwd=CODE_ROOT,
                env=env,
                stdout=target,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        write_json(
            self.receipt,
            {
                "pid": proc.pid,
                "created": psutil.Process(proc.pid).create_time(),
                "instance": instance,
            },
        )
        log("正在加载模型，可继续使用工作台；详细日志见运行环境页")
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("推理服务启动失败，请查看服务日志；配置和模型已保留")
            health = self.health()
            if health.get("ready") and health.get("instance") == instance:
                log("本机推理已就绪")
                return
            if cancelled():
                self.stop(log)
                raise RuntimeError("启动已停止")
            time.sleep(1)
        self.stop(log)
        raise RuntimeError("加载超时，模型与配置保留，可查看日志后重试")

    def stop(self, log=print):
        proc = self.owned_process()
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            log("已停止本项目的推理进程")
        self.receipt.unlink(missing_ok=True)

    def action(self, action, *, key=None, log=print, cancelled=lambda: False):
        with lock(self.root / "lifecycle.lock"), lock(self.settings.paths.cache / "gpu.lock"):
            if action == "install":
                self.install(log, cancelled)
            elif action == "start":
                self.start(log, cancelled)
            elif action == "stop":
                self.stop(log)
            elif action == "restart":
                self.stop(log)
                self.start(log, cancelled)
            elif action == "download":
                if self.settings.runtime.offline:
                    raise ValueError("离线模式禁止下载")
                if not self.python.exists():
                    raise ValueError("请先安装推理环境")
                model_path(self.settings, key)
                self.run_command(
                    [self.python, "-m", "speakvideo.inference.models", "download", key],
                    log,
                    cancelled,
                )
            elif action == "validate":
                errors = validate_model(model_path(self.settings, key), verify_hashes=True)
                if errors:
                    raise ValueError("; ".join(errors))
                log("模型完整性检查通过")
            elif action == "remove":
                if self.owned_process():
                    raise ValueError("请先停止推理服务再移除模型")
                path = model_path(self.settings, key)
                # Imported/shared directories are never deleted by the workbench.
                if self.settings.runtime.models_dir:
                    raise ValueError("外部模型目录请手动管理，不会删除共享模型")
                shutil.rmtree(path, ignore_errors=False)
                log("已移除模型；音色和工程保留")
            elif action == "test":
                from ..tts.client import TTSClient
                from ..voice_previews import VoicePreviews

                with TTSClient(self.settings) as client:
                    result = client._request("POST", "/api/test").content
                saved = VoicePreviews(self.settings.paths.root).save(
                    result,
                    kind="design",
                    role="运行环境测试",
                    emotion="平静",
                    text="你好，这是本机语音测试。",
                )
                log("真实模型试音已保存到试听历史")
                return saved["id"] + ".wav"
            else:
                raise ValueError("未知运行环境操作")
        return "操作完成"
