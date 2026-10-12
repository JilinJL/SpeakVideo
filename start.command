#!/bin/zsh
set -e
cd "$(dirname "$0")"
uv sync --python 3.11
exec uv run speakvideo serve
