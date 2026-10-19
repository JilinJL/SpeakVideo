import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from speakvideo.config import Paths, Settings
from speakvideo.storage import read_json
from speakvideo.voices import EXAMPLES, VoiceStore
from speakvideo.web import create_app


def test_bundled_assets_match_manifest_and_release_allowlist():
    from scripts.check_release import PUBLIC_ASSETS, check

    manifest = read_json(EXAMPLES / "manifest.json")
    assert len(manifest["voices"]) == 8
    for item in manifest["voices"]:
        path = EXAMPLES / item["file"]
        content = path.read_bytes()
        assert len(content) == item["bytes"]
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
        name = "speakvideo/assets/example-voices/" + item["file"]
        assert PUBLIC_ASSETS[name] == item["sha256"]
        assert not check(name, content)
        assert check(name, content + b"changed")
        assert check("voices/private.pt", content)


def test_first_launch_model_switch_and_restore_preserve_user_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("speakvideo.inference.manager.profile", lambda *args: "test")
    s = Settings(paths=Paths(root=tmp_path))
    s.ensure_dirs()
    store = VoiceStore(s)
    with TestClient(create_app(s)) as client:
        assets = client.get("/api/voices/assets").json()
        assert {a["role"] for a in assets} == {"熊大", "熊二", "喜羊羊", "懒羊羊"}
        assert {a["emotion"] for a in assets} == {"示例06B"}
        target = store.path("懒羊羊", "示例06B")
        target.write_bytes(b"user replacement")
        other = store.path("喜羊羊", "示例06B")
        other.unlink()
        store.import_examples(first_run=True)
        assert not other.exists()  # Deliberate deletions survive normal restarts.
        restored = client.post("/api/voices/examples").json()
        assert restored["imported"] == 1 and restored["skipped"] == 3
        assert target.read_bytes() == b"user replacement"
        result = client.put("/api/runtime/config", json={"model": "base"})
        assert result.status_code == 200
        assets = client.get("/api/voices/assets").json()
        assert len(assets) == 8
        assert store.path("懒羊羊", "示例17B").is_file()


def test_bundled_corruption_rejected(tmp_path, monkeypatch):
    import speakvideo.voices as module

    data = read_json(EXAMPLES / "manifest.json")
    entry = data["voices"][0]
    entry["model"] = "base-small"
    (tmp_path / "manifest.json").write_text(json.dumps(data))
    file = tmp_path / entry["file"]
    file.parent.mkdir(parents=True)
    file.write_bytes(b"corrupted")
    monkeypatch.setattr(module, "EXAMPLES", tmp_path)
    s = Settings(paths=Paths(root=tmp_path / "data"))
    s.ensure_dirs()
    with pytest.raises(ValueError, match="校验失败"):
        VoiceStore(s).import_examples()
