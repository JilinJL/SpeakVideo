# SpeakVideo

**A standalone workbench for dialogue, voice cloning and video creation.**

[中文](README.md) · [Deployment and recovery](docs/deployment.md) · [Verification status](docs/acceptance.md)

Write or generate a script, clone authorized reference voices, synthesize dialogue, arrange images and video clips, add Chinese subtitles and speaker glow, then export MP4, WAV/MP3, SRT and cover images.

## Branches

- **main**: standalone local inference. No separate Windows machine, gateway or second Qwen repository required.
- **divide**: workstation plus a separate GPU worker connected through a gateway.

Neither branch distributes personal projects, voice prompts, credentials or model weights.

## Requirements

Python 3.11 and uv are prepared by the installer. Install Git and FFmpeg/ffprobe first.

Supported inference targets: native Apple Silicon macOS with MPS (float32; 24 GB unified memory recommended), or x86_64 Windows/Linux with NVIDIA CUDA (at least 4 GiB VRAM for Base 0.6B, 6 GiB for Base 1.7B/VoiceDesign). Allow at least 15 GB disk space for initial setup, plus media and optional models. Actual hardware verification is reported separately in [acceptance](docs/acceptance.md). CPU, Intel Macs, AMD and MLX are outside v1 support. The editor still opens without a supported GPU.

## Install and start

```bash
git clone https://github.com/JilinJL/SpeakVideo.git
cd SpeakVideo
# macOS / Linux
bash scripts/install.sh
# Later
bash scripts/start.sh
```

Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

Open [127.0.0.1:8765](http://127.0.0.1:8765). The first-run panel guides you through hardware checks, runtime installation, model download and service startup. The UI currently uses Chinese labels.

Default: `Qwen/Qwen3-TTS-12Hz-0.6B-Base`. Base 1.7B is optional. Voice design requires the separately downloaded 1.7B VoiceDesign model. Pick Hugging Face or ModelScope explicitly. Downloads can resume; complete models are reused. Only one model stays on the device at a time.

Create a voice from an authorized recording and its transcript before running the real speech test. Preparation and service logs remain available after a browser reload. Routine startup reuses the installed environment and complete model.

## Language models

Use an OpenAI-compatible cloud API with your own key, or connect an existing local service:

| Service | Base URL |
|---|---|
| Ollama | `http://127.0.0.1:11434/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| vLLM / custom | e.g. `http://127.0.0.1:8000/v1` |

Keys can be empty for unauthenticated local services. The configuration panel offers model discovery and separate text/JSON tests. If discovery is unsupported, enter the model ID manually. SpeakVideo does not install or manage the LLM itself. Scripts can also be written/imported without an LLM. After models and dependencies are prepared, local LLM plus local TTS works offline.

## Data and updates

Back up `projects/`, `voices/`, `voice-previews/`, `.cache/`, `runtime/config.json`, local configuration and global script prompts. Model weights live in `models/` or an external directory you select. These are ignored by Git. `.cache/` includes private LLM configuration and must not be treated as entirely disposable.

Stop tasks and inference before updating with `git pull --ff-only`, then run the installer again. Old gateway configurations require the divide branch. Legacy pickle-based PT files require explicit trusted conversion; they are never automatically unpickled.

## Development

```bash
uv sync --locked --python 3.11
uv run pytest -q
uv run ruff check speakvideo tests scripts
uv run ruff format --check speakvideo tests scripts
uv build
```

CI tests the workbench on macOS, Windows and Linux. Real GPU validation is a separate release requirement. The demo command produces synthetic test tones, not real TTS.

Apache-2.0 for SpeakVideo code. See [third-party notices](THIRD_PARTY_NOTICES.md) for model, font and adapted-code licenses.

Voice prompts are specific to their Base size. After switching between 0.6B and 1.7B, clone from the original reference into a new role/emotion. Incompatible prompts are rejected with recovery instructions.
