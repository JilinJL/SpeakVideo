"""Speech-only batching; script lines and their stable IDs remain unchanged."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

from ..script.schema import ScriptLine

MAX_BATCH_CHARS = 200


def joined_text(lines: list[ScriptLine]) -> str:
    # A missing sentence terminator should not glue two clauses into a new word.
    return "".join(
        line.words.strip()
        + (
            "。"
            if index < len(lines) - 1
            and line.words.strip()
            and line.words.rstrip()[-1] not in "，。！？；：、,.!?;:…\"'”’）)"
            else ""
        )
        for index, line in enumerate(lines)
    )


@dataclass
class SpeechGroup:
    lines: list[ScriptLine]

    @property
    def id(self) -> str:
        return self.lines[0].id

    @property
    def text(self) -> str:
        return self.lines[0].words if len(self.lines) == 1 else joined_text(self.lines)


def speech_groups(lines: list[ScriptLine], cast: dict) -> list[SpeechGroup]:
    groups: list[SpeechGroup] = []

    def voice(line):
        binding = cast.get(line.slot)
        return (
            (line.slot, binding.role, binding.mood_map.get(line.mood))
            if binding
            else (line.slot, None, line.mood)
        )

    for line in lines:
        if (
            groups
            and voice(groups[-1].lines[-1]) == voice(line)
            and groups[-1].lines[-1].pause_after_ms < 1000
            and len(joined_text([*groups[-1].lines, line])) <= MAX_BATCH_CHARS
        ):
            groups[-1].lines.append(line)
        else:
            groups.append(SpeechGroup([line]))
    return groups


def join_group_audio(parts: list[bytes]) -> bytes:
    """Recover legacy group PCM in script order, with no cuts or timing estimates."""
    if len(parts) == 1:
        return parts[0]
    frames = []
    params = None
    for part in parts:
        with wave.open(io.BytesIO(part), "rb") as source:
            current = source.getparams()
            if params and current[:3] != params[:3]:
                raise ValueError("旧语音组音频格式不一致，请重新生成本组")
            params = current
            frames.append(source.readframes(current.nframes))
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setparams(params)
        target.writeframes(b"".join(frames))
    return out.getvalue()
