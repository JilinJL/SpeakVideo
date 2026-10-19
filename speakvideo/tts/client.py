"""Managed and existing local Qwen APIs; no inference dependencies."""

from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings


class TTSError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        (self.status_code, self.detail) = (status_code, detail)

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
            return (sr, duration)
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        raise TTSError("服务返回无效 PCM WAV，不接受 JSON/HTML 作为音频") from exc


class TTSClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.mode = settings.tts.mode
        self._base = settings.tts.base_url.rstrip("/")
        token = settings.tts.api_key.get_secret_value()
        if self.mode == "managed":
            from ..inference.manager import service_token

            self._base = f"http://127.0.0.1:{settings.runtime.port}"
            token = service_token(settings)
        self._client = httpx.Client(
            base_url=self._base,
            timeout=settings.tts.timeout_seconds,
            headers={"Authorization": f"Bearer {token}"} if token else {},
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
        if self.mode == "demo":
            raise TTSError("测试模式不能创建真实 pt")
        payload["ref_audio"] = self.upload_reference(reference)
        return self._request("POST", "/api/pt/create", json=payload).json()

    def synthesize_design(
        self, *, text: str, instruct: str, language: str = "Chinese"
    ) -> TTSResult:
        if self.mode == "demo":
            raise TTSError("测试模式不能设计真实音色")
        path = "/api/voice-design"
        r = self._request(
            "POST",
            path,
            json=dict(
                text=text, instruct=instruct, language=language, mode="file", allow_download=False
            ),
        )
        (sr, _) = wav_info(r.content)
        return TTSResult(r.content, sr)

    def synthesize(
        self, *, text: str, role: str, emotion: str = "平静", language: str = "Chinese"
    ) -> TTSResult:
        if self.mode == "demo":
            out = io.BytesIO()
            frequency = 260 + sum(role.encode()) % 200
            length = max(0.4, min(len(text) * 0.045, 2))
            with wave.open(out, "wb") as w:
                w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                w.writeframes(
                    b"".join(
                        (
                            struct.pack(
                                "<h", int(2200 * math.sin(2 * math.pi * frequency * i / 24000))
                            )
                            for i in range(int(length * 24000))
                        )
                    )
                )
            return TTSResult(out.getvalue(), 24000, raw={"demo": True})
        r = self._request(
            "POST",
            "/api/tts",
            json=dict(text=text, role=role, emotion=emotion, language=language, mode="file"),
        )
        (sr, _) = wav_info(r.content)
        return TTSResult(r.content, sr)

    def health(self, include_docs=False):
        if self.mode == "demo":
            return {"ready": True, "mode": "demo", "notice": "测试音，不是真实配音"}
        return self._request("GET", "/api/health").json()

    def list_roles(self):
        if self.mode == "demo":
            from ..voices import VoiceStore

            return VoiceStore(self.settings).list_roles()
        return self._request("GET", "/api/pt/list").json()["roles"]

    def download_pt(self, role, emotion):
        return self._request(
            "GET", "/api/pt/download", params={"role": role, "emotion": emotion}
        ).content

    def capabilities(self):
        return {
            "available": True,
            "source": self.mode,
            "capabilities": ["clone", "tts", "voice-design"],
        }
