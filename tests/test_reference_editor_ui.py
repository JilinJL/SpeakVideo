import shutil
import subprocess
from pathlib import Path

import pytest


def test_reference_editor_selection_playback_and_lazy_crop():
    if not shutil.which("node"):
        pytest.skip("Node.js not installed")
    result = subprocess.run(
        ["node", "tests/reference-editor-check.mjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
