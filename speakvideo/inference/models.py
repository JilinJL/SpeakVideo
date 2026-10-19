"""Complete local snapshots. Validation does not import Torch.

Adapted from qwen3-tts-web models.py (MIT); see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

from ..storage import lock, read_json, sha256, write_json

MODELS = {
    "base-small": "Qwen3-TTS-12Hz-0.6B-Base",
    "base": "Qwen3-TTS-12Hz-1.7B-Base",
    "voicedesign": "Qwen3-TTS-12Hz-1.7B-VoiceDesign",
}


def model_path(settings, key=None):
    key = key or settings.runtime.model
    if key not in MODELS:
        raise ValueError("未知模型")
    return (settings.runtime.models_dir or settings.paths.root / "models") / MODELS[key]


def validate_model(path: Path, *, verify_hashes=False):
    errors = []

    def check(relative, validator):
        try:
            validator(path / relative)
        except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
            errors.append(f"{relative.as_posix()}: {exc}")

    def json_file(p):
        result = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not result:
            raise ValueError("JSON 为空或损坏")
        return result

    def tensors(p):
        with p.open("rb") as f:
            size = struct.unpack("<Q", f.read(8))[0]
            if not 0 < size <= 100_000_000:
                raise ValueError("权重头损坏")
            header = json.loads(f.read(size))
        offsets = [v["data_offsets"] for k, v in header.items() if k != "__metadata__"]
        if (
            not offsets
            or any(a < 0 or b <= a for a, b in offsets)
            or p.stat().st_size != 8 + size + max(b for _, b in offsets)
        ):
            raise ValueError("权重数据不完整")

    def weights(folder):
        index = folder / "model.safetensors.index.json"
        if not (path / index).exists():
            check(folder / "model.safetensors", tensors)
            return
        try:
            mapping = json_file(path / index)["weight_map"]
            if not mapping:
                raise ValueError("分片索引为空")
            for name in set(mapping.values()):
                if not (path / folder / name).resolve().is_relative_to((path / folder).resolve()):
                    raise ValueError("分片路径越界")
                check(folder / name, tensors)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{index.as_posix()}: {exc}")

    for name in (
        "config.json",
        "preprocessor_config.json",
        "generation_config.json",
        "tokenizer_config.json",
        "vocab.json",
        "speech_tokenizer/config.json",
        "speech_tokenizer/preprocessor_config.json",
    ):
        check(Path(name), json_file)

    def nonempty(p):
        if not p.stat().st_size:
            raise ValueError("文件为空")

    check(Path("merges.txt"), nonempty)
    weights(Path("."))
    weights(Path("speech_tokenizer"))
    if verify_hashes and not errors:
        try:
            for name, expected in (
                read_json(path / ".speakvideo-model.json", {}).get("sha256", {}).items()
            ):
                target = (path / name).resolve()
                if not target.is_relative_to(path.resolve()) or sha256(target) != expected:
                    errors.append(f"{name}: SHA256 校验失败")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(f"校验记录无效: {exc}")
    return errors


def identity(settings):
    path = model_path(settings)
    # Changing files, model directory or runtime version invalidates voice/audio caches.
    stat = (
        [
            (p.relative_to(path).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(path.rglob("*.safetensors"))
        ]
        if path.is_dir()
        else []
    )
    receipt = read_json(path / ".speakvideo-model.json", {})
    return hashlib.sha256(
        json.dumps([str(path.resolve()), stat, receipt, "runtime-v1"], sort_keys=True).encode()
    ).hexdigest()


def download(settings, key, log=print):
    path = model_path(settings, key)
    initial_errors = validate_model(path, verify_hashes=True)
    if not initial_errors:
        log(f"模型完整，复用 {path}")
        return
    if settings.runtime.offline:
        raise ValueError("离线模式下模型缺失或损坏；请导入完整模型目录后重新校验")
    with lock(path.parent / f".{path.name}.download.lock"):
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ["TRANSFORMERS_OFFLINE"] = "0"
        path.mkdir(parents=True, exist_ok=True)
        repo = f"Qwen/{MODELS[key]}"
        log(f"下载 {repo} via {settings.runtime.source}；可重新提交以续传")
        if settings.runtime.source == "modelscope":
            from modelscope.hub.snapshot_download import snapshot_download

            snapshot_download(repo, local_dir=str(path))
        else:
            from huggingface_hub import snapshot_download

            snapshot_download(
                repo,
                local_dir=str(path),
                max_workers=4,
                force_download=any("SHA256" in error for error in initial_errors),
            )
        errors = validate_model(path)
        if errors:
            raise ValueError("模型校验失败：" + "; ".join(errors))
        hashes = {
            p.relative_to(path).as_posix(): sha256(p)
            for p in sorted(path.rglob("*"))
            if p.is_file()
            and p.suffix in {".safetensors", ".json", ".txt"}
            and not any(part.startswith(".") for part in p.relative_to(path).parts)
        }
        write_json(
            path / ".speakvideo-model.json",
            {"repo": repo, "source": settings.runtime.source, "sha256": hashes},
        )
        log("模型下载并通过完整性检查")


if __name__ == "__main__":
    import argparse

    from ..config import load_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["download", "validate"])
    parser.add_argument("key", choices=MODELS)
    args = parser.parse_args()
    settings = load_settings()
    if args.action == "download":
        download(settings, args.key)
    else:
        errors = validate_model(model_path(settings, args.key), verify_hashes=True)
        if errors:
            raise SystemExit("\n".join(errors))
        print("模型完整")
