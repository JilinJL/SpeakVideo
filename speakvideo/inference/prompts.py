"""Tensor/plain-data voice prompts. No automatic arbitrary pickle loading."""

from __future__ import annotations

import dataclasses
import io

FIELDS = {"ref_code", "ref_spk_embedding", "x_vector_only_mode", "icl_mode", "ref_text"}


def save_prompt(items, *, model_key=None):
    import torch

    rows = []
    for item in items:
        row = dataclasses.asdict(item) if dataclasses.is_dataclass(item) else dict(item)
        if set(row) - FIELDS:
            raise ValueError("音色字段不兼容")
        rows.append(
            {k: v.detach().cpu() if isinstance(v, torch.Tensor) else v for k, v in row.items()}
        )
    out = io.BytesIO()
    torch.save({"schema": "speakvideo.voice.v1", "model": model_key, "items": rows}, out)
    return out.getvalue()


def load_prompt(content, *, expected_model=None):
    import torch
    from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem

    try:
        value = torch.load(io.BytesIO(content), map_location="cpu", weights_only=True)
    except Exception as exc:
        raise ValueError("此 PT 不能安全加载。请使用可信旧音色转换命令或从原声重新克隆") from exc
    if not isinstance(value, dict) or value.get("schema") != "speakvideo.voice.v1":
        raise ValueError("请使用独立版音色格式；可信旧 PT 请显式转换")
    if expected_model and value.get("model") and value["model"] != expected_model:
        raise ValueError(
            "音色所属 Base 档位与当前模型不同，请切回原模型，或用原声在当前档位重新克隆"
        )
    rows = value.get("items")
    if not isinstance(rows, list) or not 0 < len(rows) <= 16:
        raise ValueError("音色条目无效")
    items = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) - FIELDS
            or not {"ref_spk_embedding", "ref_code", "icl_mode", "x_vector_only_mode"} <= row.keys()
        ):
            raise ValueError("音色字段无效")
        if (
            not isinstance(row["ref_spk_embedding"], torch.Tensor)
            or row["ref_spk_embedding"].ndim != 1
            or row["ref_spk_embedding"].numel() > 65536
        ):
            raise ValueError("音色向量无效")
        if row["ref_code"] is not None and (
            not isinstance(row["ref_code"], torch.Tensor) or row["ref_code"].ndim not in (1, 2)
        ):
            raise ValueError("音色编码无效")
        if expected_model and row["ref_spk_embedding"].numel() != (
            1024 if expected_model == "base-small" else 2048
        ):
            raise ValueError("音色向量与当前 Base 档位不兼容，请切回原模型或用原声重新克隆")
        if any(type(row[k]) is not bool for k in ("icl_mode", "x_vector_only_mode")) or (
            row.get("ref_text") is not None and not isinstance(row["ref_text"], str)
        ):
            raise ValueError("音色元数据无效")
        items.append(VoiceClonePromptItem(**row))
    return items


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="仅将本人信任的旧 PT 转为安全数据；旧格式加载可以执行代码"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--trust-legacy-pickle", action="store_true", required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve() or args.output.exists():
        raise SystemExit("请选择不存在的新输出路径，原件不会被覆盖")
    import torch

    value = torch.load(args.source, map_location="cpu", weights_only=False)
    rows = value.get("items") if isinstance(value, dict) and "items" in value else value
    if not isinstance(rows, (list, tuple)):
        rows = [rows]
    content = save_prompt(rows)
    load_prompt(content)
    from ..storage import atomic_bytes

    atomic_bytes(args.output, content)
    print("已转换，原件保留")
