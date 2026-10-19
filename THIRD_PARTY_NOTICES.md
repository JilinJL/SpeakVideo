# Third-party notices

## Adapted deployment/model utilities

Parts of `speakvideo/inference/models.py`, platform policy and the runtime constraints were adapted from [NOTF-API/qwen3-tts-base-web](https://github.com/NOTF-API/qwen3-tts-base-web), using the local JilinJL fork at commit `506c777304df726c335dce3f534b4853147c588a` as the reviewed reference. Original copyright: Copyright (c) 2026 NOTF-API. Original license: MIT, preserved in [licenses/qwen3-tts-web-MIT.txt](licenses/qwen3-tts-web-MIT.txt). SpeakVideo's modifications and integration are distributed under Apache-2.0; the original MIT notice remains applicable to the adapted portions.

The reference was clean but behind its remote at inspection time. SpeakVideo does not download or run that repository and does not depend on a neighboring checkout.

## Qwen3-TTS

[QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) and the selected Qwen model cards provide the upstream code/model licenses. The supported Base and VoiceDesign model cards declare Apache-2.0. Models are fetched separately by the user and are not distributed in this source repository. Keep the downloaded model notices with any redistributed weights. `qwen-tts` and its dependencies retain their own licenses.

## Chinese font

Noto Sans CJK SC Regular from [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk), distributed under the SIL Open Font License 1.1. The bundled unmodified font is `speakvideo/assets/fonts/NotoSansCJKsc-Regular.otf`; its copyright and license are preserved in the adjacent `OFL.txt`.

## System tools

FFmpeg/ffprobe and NVIDIA drivers are installed separately and are not bundled. Their terms depend on the distribution the user chooses. Other Python and JavaScript dependencies retain their respective licenses.
