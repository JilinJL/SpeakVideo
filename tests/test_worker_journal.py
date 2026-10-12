import ast
import asyncio
from pathlib import Path

import pytest

from worker.journal import Journal
from worker.patch_qwen import patch
from worker.smoke import verify


def test_restart_preserves_idempotency_and_reports_result_loss(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    journal = Journal(path)
    payload = {"job_id": "j_123", "text": "同一句话", "voice_sha256": "a" * 64}
    journal.insert(payload)
    journal.update("j_123", "succeeded")
    journal.close()
    journal = Journal(path)
    record = journal.get("j_123")
    assert record["payload"] == payload
    assert record["status"] == "failed"
    assert record["error"] == "worker_restarted_result_lost"
    assert record["digest"] != journal.digest({**payload, "text": "换了台词"})
    assert "audio" not in record
    journal.close()


def test_memory_adapter_is_valid_and_repeatable():
    fixture = """class VoiceDesignRequest:
    allow_download: bool = False
class PromptCreateRequest:
    ref_audio: str
def api_pt_create(req):
    if not os.path.exists(req.ref_audio):
        raise ValueError()
    kwargs = {"ref_audio": req.ref_audio}
def save_generation(req, wavs, sr, generated):
    return "legacy-persisted"
"""
    result = patch(fixture)
    ast.parse(result)
    assert "ref_audio_base64" in result and "persist: bool" in result
    assert patch(result) == result
    with pytest.raises(ValueError, match="layout changed"):
        patch("unknown upstream")


def test_worker_source_compiles_without_inference_dependencies():
    ast.parse((Path(__file__).parents[1] / "worker" / "speakvideo_agent.py").read_text())


def test_full_worker_dedupe_conflict_and_restart_with_fake_qwen(tmp_path):
    asyncio.run(verify(tmp_path))
