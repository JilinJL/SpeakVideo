from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


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
    timeout_seconds: float = Field(default=300, gt=0)
    job_timeout_seconds: float = Field(default=1800, gt=0)
    poll_interval_seconds: float = Field(default=1, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    generation_revision: str = "qwen3-base-1.7b-v1"


class LLMSettings(Strict):
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    model: str = ""
    json_mode: bool = True
    timeout_seconds: float = Field(default=180, gt=0)


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
    max_line_chars: int = Field(default=100, ge=1, le=10000)
    default_template: str = "duo_dialogue"


class VideoSettings(Strict):
    width: int = Field(default=1920, ge=320, multiple_of=2)
    height: int = Field(default=1080, ge=240, multiple_of=2)
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
    for section, mapping in {
        "tts": {"MODE": "mode", "BASE_URL": "base_url", "API_KEY": "api_key"},
        "llm": {"BASE_URL": "base_url", "API_KEY": "api_key", "MODEL": "model"},
    }.items():
        for suffix, key in mapping.items():
            name = f"SPEAKVIDEO_{section.upper()}_{suffix}"
            if name in os.environ:
                data.setdefault(section, {})[key] = os.environ[name]
    return Settings.model_validate(data)
