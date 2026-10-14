"""Direct Qwen API and future P6 gateway client; no inference dependencies."""

from __future__ import annotations

import hashlib
import io
import math
import re
import struct
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Settings


class TTSError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status_code, self.detail = status_code, detail

    def __str__(self):
        return super().__str__() + (f": {self.detail}" if self.detail else "")


@dataclass
class PTVoice:
    role: str
    emotion: str

    @property
    def rel(self) -> str:
        return f"{self.role}/{self.role}_{self.emotion}.pt"


@dataclass
class TTSResult:
    wav_bytes: bytes
    sample_rate: int
    filename: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def wav_info(data: bytes) -> tuple[int, float]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            sr = w.getframerate()
            expected = w.getnframes() * w.getnchannels() * w.getsampwidth()
            decoded = w.readframes(w.getnframes())
            duration = w.getnframes() / sr
            if duration <= 0 or len(decoded) != expected:
                raise TTSError("服务返回空或截断的 PCM WAV")
            return sr, duration
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        raise TTSError("服务返回无效 PCM WAV，不接受 JSON/HTML 作为音频") from exc


class TTSClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = settings.tts.mode
        self._base = settings.tts.base_url.rstrip("/")
        token = settings.tts.api_key.get_secret_value()
        self._client = httpx.Client(
            base_url=self._base,
            timeout=settings.tts.timeout_seconds,
            headers={
                **({"Authorization": f"Bearer {token}"} if token else {}),
                **({"X-Worker-ID": settings.tts.worker_id} if self.mode == "gateway" else {}),
            },
            trust_env=False,
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TTSError(f"TTS 请求失败 {method} {path} ({type(exc).__name__})") from exc
        if response.is_error:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            raise TTSError(
                f"{method} {path} 返回 {response.status_code}", response.status_code, detail
            )
        return response

    def health(self, include_docs: bool = False) -> dict:
        if self.mode == "demo":
            return {"ready": True, "mode": "demo", "notice": "测试音，不是真实配音"}
        path = "/api/health" if self.mode == "direct" else "/v1/health"
        params = {"include_docs": str(include_docs).lower()} if self.mode == "gateway" else {}
        try:
            return self._request("GET", path, params=params).json()
        except TTSError as exc:
            # A connected worker with a loading model is different from an offline agent.
            detail = exc.detail
            if (
                exc.status_code == 503
                and isinstance(detail, dict)
                and (detail.get("worker_connected") is True or "capabilities" in detail)
            ):
                return detail
            raise

    def capabilities(self) -> dict:
        if self.mode != "gateway":
            return {
                "available": False,
                "source": self.mode,
                "message": "当前模式不使用 Windows Agent",
            }
        source = "worker.capabilities"
        try:
            envelope = self._request("GET", "/v1/worker/capabilities", timeout=12).json()
            document = envelope.get("result", envelope)
        except TTSError as exc:
            if exc.status_code not in {404, 405, 501}:
                raise
            source = "worker.health"
            document = self.health(include_docs=True).get("interface_docs")
        if document is None:
            return {
                "available": False,
                "source": source,
                "message": "Windows 未提供接口文档，请升级 Agent",
            }
        if not isinstance(document, dict) or document.get("schema") != (
            "speakvideo.windows-agent.interface-document"
        ):
            raise TTSError("Windows 接口文档格式无效")
        if document.get("protocol") != "1" or document.get("schema_version") != "1.0":
            raise TTSError("Windows 接口文档版本不兼容，请更新 Mac 与 Agent")
        if document.get("worker_id") != self.settings.tts.worker_id:
            raise TTSError("Windows 接口文档的 Worker ID 与配置不一致")
        rpc, actions, limits = (document.get(k) for k in ("rpc", "voice_manage_actions", "limits"))
        if (
            not isinstance(rpc, list)
            or not all(isinstance(v, dict) for v in rpc)
            or not (isinstance(actions, dict) and isinstance(limits, dict))
        ):
            raise TTSError("Windows 接口文档缺少 RPC、音色操作或限制")
        return {
            "available": True,
            "source": source,
            "document": document,
            "rpc": [
                v["type"]
                for v in rpc
                if v.get("implemented") is True and isinstance(v.get("type"), str)
            ],
            "voice_actions": list(actions),
            "limits": limits,
            "gateway_capabilities": source == "worker.capabilities",
        }

    def list_roles(self) -> dict[str, list[str]]:
        if self.mode == "demo":
            from ..voices import VoiceStore

            return VoiceStore(self.settings).list_roles()
        path = "/api/pt/list" if self.mode == "direct" else "/v1/voices"
        return self._request("GET", path).json()["roles"]

    def download_pt(self, role: str, emotion: str) -> bytes:
        path = "/api/pt/download" if self.mode == "direct" else "/v1/voices/download"
        return self._request("GET", path, params={"role": role, "emotion": emotion}).content

    def delete_pt(self, role: str, emotion: str, digest: str) -> dict:
        if self.mode == "demo":
            return {"deleted": True, "mode": "demo"}
        if self.mode != "gateway":
            raise TTSError("远端删除需要 SpeakVideo Windows Agent；Qwen 原生接口没有此功能")
        result = self._request(
            "DELETE", "/v1/voices", json={"role": role, "emotion": emotion, "sha256": digest}
        ).json()
        if result.get("deleted") is not True:
            raise TTSError("Windows 未确认 PT 删除")
        return result

    def manage_voices(self, action: str, **values) -> dict:
        if self.mode != "gateway":
            raise TTSError("远端资产接口仅适用于 Windows 网关模式")
        try:
            result = self._request(
                "POST", "/v1/voices/manage", json={"action": action, **values}
            ).json()
            return result
        except TTSError as exc:
            if exc.status_code == 404:
                raise TTSError(
                    "Gateway 缺少 PT 管理路由，请升级网关；Windows 是否支持编辑请查看接口文档", 404
                ) from None
            raise

    def sync_voices(self, assets: list[tuple[str, str, bytes]]) -> dict:
        if self.mode != "gateway":
            raise TTSError("同步仅用于 gateway；现有 Qwen 服务没有 pt 同步接收接口")
        import json
        import zipfile

        buffer = io.BytesIO()
        manifest = []
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            for role, emotion, content in assets:
                rel = PTVoice(role, emotion).rel
                manifest.append(
                    {
                        "role": role,
                        "emotion": emotion,
                        "path": rel,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
                z.writestr(rel, content)
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        return self._request(
            "POST",
            "/v1/voices/sync",
            files={"bundle": ("voices.zip", buffer.getvalue(), "application/zip")},
        ).json()

    def upload_reference(self, path: Path) -> str:
        with path.open("rb") as f:
            return self._request(
                "POST", "/api/pt/upload", files={"file": (path.name, f, "application/octet-stream")}
            ).json()["path"]

    def create_pt(
        self, *, reference: Path, role: str, emotion: str, ref_text: str, overwrite: bool = False
    ) -> dict:
        ref_text = ref_text.strip()
        if not ref_text:
            raise ValueError("请填写角色原声的对应台词")
        payload = dict(role=role, emotion=emotion, ref_text=ref_text, overwrite=overwrite)
        if self.mode == "gateway":
            import json

            return self._request(
                "POST",
                "/v1/voices/create",
                data={"metadata": json.dumps(payload, ensure_ascii=False)},
                files={"file": (reference.name, reference.read_bytes(), "audio/wav")},
            ).json()
        if self.mode == "demo":
            raise TTSError("测试模式不能创建真实 pt")
        payload["ref_audio"] = self.upload_reference(reference)
        return self._request("POST", "/api/pt/create", json=payload).json()

    def synthesize_design(
        self, *, text: str, instruct: str, language: str = "Chinese"
    ) -> TTSResult:
        if self.mode == "demo":
            raise TTSError("测试模式不能设计真实音色")
        path = "/api/voice-design" if self.mode == "direct" else "/v1/voices/design"
        r = self._request(
            "POST",
            path,
            json=dict(
                text=text, instruct=instruct, language=language, mode="file", allow_download=False
            ),
        )
        sr, _ = wav_info(r.content)
        return TTSResult(r.content, sr)

    def submit(self, payload: dict, key: str) -> str:
        data = self._request(
            "POST", "/v1/tts/jobs", json=payload, headers={"Idempotency-Key": key}
        ).json()
        return str(data["id"])

    def poll(self, job_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise TTSError("无效的远端任务 ID")
        return self._request("GET", f"/v1/tts/jobs/{job_id}").json()

    def wait_job(self, job_id: str, cancelled=lambda: False) -> TTSResult:
        deadline = time.monotonic() + self.settings.tts.job_timeout_seconds
        while time.monotonic() < deadline:
            if cancelled():
                raise TTSError("已暂停；远端任务 ID 已保存，可继续轮询")
            job = self.poll(job_id)
            if job["status"] in {"failed", "cancelled"}:
                raise TTSError("远端任务失败", detail=job.get("error", job["status"]))
            if job["status"] == "succeeded":
                url = job["audio_url"]
                absolute = str(httpx.URL(self._base + "/").join(url))
                a, b = urlparse(absolute), urlparse(self._base)
                if (a.scheme, a.netloc) != (b.scheme, b.netloc):
                    raise TTSError("拒绝向跨域音频地址发送 Bearer token")
                r = self._request("GET", absolute)
                sr, _ = wav_info(r.content)
                return TTSResult(r.content, sr, raw={"job_id": job_id})
            if job["status"] not in {"queued", "running"}:
                raise TTSError("未知作业状态", detail=job)
            time.sleep(self.settings.tts.poll_interval_seconds)
        raise TTSError("轮询超时；已保存远端任务 ID，继续时不会重复提交")

    def synthesize(
        self, *, text: str, role: str, emotion: str = "平静", language: str = "Chinese"
    ) -> TTSResult:
        if self.mode == "gateway":
            raise TTSError("gateway 必须使用持久化 submit/poll 编排")
        if self.mode == "demo":
            out = io.BytesIO()
            frequency = 260 + sum(role.encode()) % 200
            length = max(0.4, min(len(text) * 0.045, 2))
            with wave.open(out, "wb") as w:
                w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                w.writeframes(
                    b"".join(
                        struct.pack("<h", int(2200 * math.sin(2 * math.pi * frequency * i / 24000)))
                        for i in range(int(length * 24000))
                    )
                )
            return TTSResult(out.getvalue(), 24000, raw={"demo": True})
        r = self._request(
            "POST",
            "/api/tts",
            json=dict(text=text, role=role, emotion=emotion, language=language, mode="file"),
        )
        sr, _ = wav_info(r.content)
        return TTSResult(r.content, sr)
