#!/usr/bin/env bash
set -euo pipefail
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x .uv/uv ]]; then uv_bin="$PWD/.uv/uv"; elif command -v uv >/dev/null 2>&1; then uv_bin="$(command -v uv)"; else echo '请先运行 bash scripts/install.sh'; exit 1; fi
if [[ ! -x .venv/bin/python ]]; then exec bash scripts/install.sh; fi
exec "$uv_bin" run --no-sync speakvideo serve
