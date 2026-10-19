# SpeakVideo

**Self-hosted AI video generator with Qwen3-TTS voice cloning and local text-to-speech.**

[中文](README.md) · [Deployment and recovery](docs/deployment.md) · [Verification status](docs/acceptance.md)

Write or generate a script, clone authorized reference voices, synthesize dialogue, arrange images and video clips, add Chinese subtitles and speaker glow, then export MP4, WAV/MP3, SRT and cover images.

## Finished example

**Yangcun Talk: Why does progress make us more anxious?** A complete 2:49 dialogue video from the author's existing local project.

<a href="docs/examples/yangcun-talk.mp4"><img src="docs/examples/yangcun-talk.webp" width="280" alt="Watch the complete Yangcun Talk video" /></a>

[Watch / download MP4](docs/examples/yangcun-talk.mp4) · [Follow the illustrated walkthrough](README.md#跟着做完成第一个作品)

The repository contains a compressed 540 × 960 showcase copy (~27 MB). The original 1080 × 1920 video stays on the author's machine.

## Requirements

Open-source preview: real inference and video export have been verified on Apple Silicon. Windows/Linux NVIDIA end-to-end GPU validation remains pending.

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

Four bundled example voices (Briar / 熊大, Bramble / 熊二, Pleasant Goat / 喜羊羊, Lazy Goat / 懒羊羊) are copied into the local library on first launch. Choose `示例06B` for Base 0.6B or `示例17B` for Base 1.7B. Use “导入内置音色” in the voice library, or `uv run speakvideo voices examples`, to restore missing examples without replacing existing files. You can also clone a voice from an authorized recording and its transcript. Preparation and service logs remain available after a browser reload. Routine startup reuses the installed environment and complete model.

## Language models

Use an OpenAI-compatible cloud API with your own key, or connect an existing local service:

| Service | Base URL |
|---|---|
| Ollama | `http://127.0.0.1:11434/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| vLLM / custom | e.g. `http://127.0.0.1:8000/v1` |

Keys can be empty for unauthenticated local services. The configuration panel offers model discovery and separate text/JSON tests. If discovery is unsupported, enter the model ID manually. SpeakVideo does not install or manage the LLM itself. Scripts can also be written/imported without an LLM. After models and dependencies are prepared, local LLM plus local TTS works offline.

## Make your first video

1. Create a project with one clear topic. Generate a draft and structured lines using your LLM, or manually add the [four example lines](docs/examples/first-dialogue/script.json).
2. Finalize the script. Bind P1 to 懒羊羊 and P2 to 喜羊羊, mapping every used mood to the matching Base-size example prompt. P1 is orange; P2 is blue.
3. Confirm bindings, synthesize speech, listen to each group and retry only groups that need improvement. Merge and review the complete audio.
4. Upload a background image/video, choose clip ranges and arrange them. Start with 720 × 1280 / 24 fps. Enable burned-in white subtitles with speaker-color outlines.
5. Optionally add transparent character stickers, bind them to P1/P2, and enable speaking glow. Provide your own character images.
6. Save video settings, render and review the MP4, then download it. WAV, MP3, SRT and optional covers are available separately.

![Script editor](docs/tutorial/script.webp)
![Voice bindings](docs/tutorial/binding.webp)
![Video editing](docs/tutorial/video.webp)

The illustrated Chinese tutorial includes screenshots of all stages. Bundled PTs use the restricted data format and have separate 0.6B / 1.7B variants; the 0.6B examples were re-cloned from synthetic 1.7B references. See [voice provenance](speakvideo/assets/example-voices/NOTICE.md) and [media notices](docs/examples/NOTICE.md). Third-party characters, voices and background footage are not relicensed under Apache-2.0.

## Data and updates

Back up `projects/`, `voices/`, `voice-previews/`, `.cache/`, `runtime/config.json`, local configuration and global script prompts. Model weights live in `models/` or an external directory you select. These are ignored by Git. `.cache/` includes private LLM configuration and must not be treated as entirely disposable.

Stop tasks and inference before updating with `git pull --ff-only`, then run the installer again. Legacy pickle-based PT files require explicit trusted conversion; they are never automatically unpickled.

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
