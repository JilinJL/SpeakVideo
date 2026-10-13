from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paths(Strict):
    root: Path = Field(default_factory=lambda: Path.cwd().resolve())

    @property
    def voices(self) -> Path:
        return self.root / "voices"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def cache(self) -> Path:
        return self.root / ".cache"


class TTSSettings(Strict):
    mode: Literal["direct", "gateway", "demo"] = "direct"
    base_url: str = "http://127.0.0.1:8001"
    api_key: SecretStr = SecretStr("")
    worker_id: str = "example-worker"
    timeout_seconds: float = Field(default=300, gt=0)
    job_timeout_seconds: float = Field(default=1800, gt=0)
    poll_interval_seconds: float = Field(default=1, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    generation_revision: str = "qwen3-base-1.7b-v1"
    local_pt_root: Path | None = None


class LLMEndpoint(Strict):
    id: str
    name: str = ""
    base_url: str
    model: str
    api_keys: list[SecretStr] = Field(default_factory=list)
    enabled: bool = True
    json_mode: bool = True
    timeout_seconds: float = Field(default=180, gt=0, le=3600)
    max_tokens: int = Field(default=8192, ge=32, le=131072)

    @model_validator(mode="after")
    def valid_url(self):
        from urllib.parse import urlparse

        from .storage import safe_name

        safe_name(self.id)
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LLM 地址必须为不含凭据或查询参数的 HTTP(S) URL")
        if not self.model.strip():
            raise ValueError("请填写模型名称")
        return self


class LLMSettings(Strict):
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    model: str = ""
    json_mode: bool = True
    timeout_seconds: float = Field(default=180, gt=0)
    providers: list[LLMEndpoint] = Field(default_factory=list)
    active_provider: str = ""
    failover: bool = True

    @model_validator(mode="after")
    def unique_providers(self):
        ids = [p.id for p in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("供应商 ID 重复")
        if self.providers and self.active_provider not in ids:
            raise ValueError("请选择有效的默认供应商")
        return self

    def endpoints(self) -> list[LLMEndpoint]:
        if not self.providers:
            return (
                [
                    LLMEndpoint(
                        id="legacy",
                        name="原有配置",
                        base_url=self.base_url,
                        model=self.model,
                        api_keys=[self.api_key],
                        json_mode=self.json_mode,
                        timeout_seconds=self.timeout_seconds,
                    )
                ]
                if self.model
                else []
            )
        active = [p for p in self.providers if p.enabled and p.id == self.active_provider]
        return active + (
            [p for p in self.providers if p.enabled and p.id != self.active_provider]
            if self.failover
            else []
        )


class AudioSettings(Strict):
    model_sample_rate: int = 24000
    master_sample_rate: int = 48000
    narrative_lufs: float = -16
    narrative_true_peak: float = -1.5
    final_lufs: float = -14
    final_true_peak: float = -1
    channels: int = 1
    master_lufs: float = Field(default=-16, ge=-30, le=-5)
    master_true_peak: float = Field(default=-1.5, ge=-9, le=-0.1)
    bgm_volume: float = Field(default=0.18, ge=0, le=1)


class ScriptSettings(Strict):
    max_line_chars: int = Field(default=200, ge=3, le=200)
    default_template: str = "duo_dialogue"


class VideoSettings(Strict):
    width: int = Field(default=1080, ge=320, multiple_of=2)
    height: int = Field(default=1920, ge=240, multiple_of=2)
    fps: int = Field(default=30, ge=1, le=60)
    bgm_volume: float = Field(default=0.18, ge=0, le=1)


class Settings(Strict):
    paths: Paths = Field(default_factory=Paths)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    script: ScriptSettings = Field(default_factory=ScriptSettings)
    video: VideoSettings = Field(default_factory=VideoSettings)

    def ensure_dirs(self) -> None:
        for directory in (self.paths.voices, self.paths.projects, self.paths.cache):
            directory.mkdir(parents=True, exist_ok=True)


def load_settings(root: Path | None = None) -> Settings:
    root = Path(root or os.environ.get("SPEAKVIDEO_ROOT", Path.cwd())).expanduser().resolve()
    path = root / "config.local.toml"
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data["paths"] = {"root": root}
    mode_path = root / ".cache" / "tts-mode.json"
    if mode_path.exists():
        import json

        modes = json.loads(mode_path.read_text())
        data["tts"] = modes[modes["active"]]
    llm_path = root / ".cache" / "llm.local.json"
    if llm_path.exists():
        import json

        data["llm"] = json.loads(llm_path.read_text())
    for section, mapping in {
        "tts": {
            "MODE": "mode",
            "BASE_URL": "base_url",
            "API_KEY": "api_key",
            "WORKER_ID": "worker_id",
        },
        "llm": {"BASE_URL": "base_url", "API_KEY": "api_key", "MODEL": "model"},
    }.items():
        for suffix, key in mapping.items():
            name = f"SPEAKVIDEO_{section.upper()}_{suffix}"
            if name in os.environ:
                data.setdefault(section, {})[key] = os.environ[name]
                if section == "llm" and data[section].get("providers"):
                    active = data[section].get("active_provider")
                    for provider in data[section]["providers"]:
                        if provider["id"] == active:
                            provider["api_keys" if key == "api_key" else key] = (
                                [os.environ[name]] if key == "api_key" else os.environ[name]
                            )
    return Settings.model_validate(data)
