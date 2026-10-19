#!/usr/bin/env bash
set -euo pipefail
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p .uv
if [[ -x .uv/uv ]]; then uv_bin="$PWD/.uv/uv"; elif command -v uv >/dev/null 2>&1; then uv_bin="$(command -v uv)"; else
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o .uv/install.sh
  UV_INSTALL_DIR="$PWD/.uv" UV_NO_MODIFY_PATH=1 sh .uv/install.sh
  uv_bin="$PWD/.uv/uv"
fi
"$uv_bin" sync --locked --no-dev --python 3.11
"$uv_bin" run --no-sync speakvideo doctor
if [[ "${1:-}" != "--no-start" ]]; then exec "$uv_bin" run --no-sync speakvideo serve; fi
