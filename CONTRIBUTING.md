# Contributing

Use main for standalone work and divide for gateway/worker changes. Create a feature branch and describe the trigger, behavior change and relevant verification in your PR. Shared editor changes should be selected explicitly across branches.

Run `uv sync --locked --python 3.11`, pytest, Ruff checks and formatting checks. Check modified JavaScript with Node. Changes to model/runtime behavior need a real-device record as well as mock tests; name the OS, hardware, model and tested operation. Do not label simulated audio as GPU validation.

Keep projects, recordings, voice prompts, models, credentials and local endpoints out of commits. Use neutral example speakers and synthetic fixtures. New UI work should be checked at desktop and mobile sizes. Preserve P1 orange/P2 blue, speaker glow and white subtitles with speaker-colored outlines.

By contributing, you agree that your contribution may be distributed under the project's Apache-2.0 license. Retain upstream attribution when adapting third-party code.
