"""Speech-only batching; script lines and their stable IDs remain unchanged."""

from __future__ import annotations

import io
import re
import wave
from array import array
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


def split_group_audio(data: bytes, lines: list[ScriptLine]) -> list[tuple[bytes, int, int]]:
    """Estimate subtitle cuts near silence; slices rejoin without any lost PCM frames.

    No forced aligner is available: these are approximate line boundaries, not
    word timestamps. The master keeps the group's original continuous speech.
    """
    with wave.open(io.BytesIO(data), "rb") as source:
        params = source.getparams()
        pcm = source.readframes(params.nframes)
    if params.nframes < len(lines):
        raise ValueError("合并配音过短，无法对应台词行")
    stride = params.sampwidth * params.nchannels
    weights = [max(1, len(re.sub(r"\s", "", line.words))) for line in lines]
    cuts, consumed = [0], 0
    quiet = []
    if params.sampwidth == 2:
        samples = array("h", pcm)
        import sys

        if sys.byteorder != "little":
            samples.byteswap()
        window = max(1, params.framerate // 100)
        for start in range(0, params.nframes, window):
            chunk = samples[start * params.nchannels : (start + window) * params.nchannels]
            if chunk and max(abs(value) for value in chunk) < 250:
                quiet.append(start + min(window, params.nframes - start) // 2)
    for index, weight in enumerate(weights[:-1]):
        consumed += weight
        target = round(params.nframes * consumed / sum(weights))
        low = cuts[-1] + 1
        high = params.nframes - (len(lines) - index - 1)
        radius = min(round(params.framerate * 0.4), round(params.nframes / len(lines) * 0.2))
        candidates = [
            frame for frame in quiet if low <= frame <= high and abs(frame - target) <= radius
        ]
        cuts.append(
            min(candidates, key=lambda frame: abs(frame - target))
            if candidates
            else max(low, min(high, target))
        )
    cuts.append(params.nframes)
    slices = []
    for start, end in zip(cuts, cuts[1:]):
        out = io.BytesIO()
        with wave.open(out, "wb") as target:
            target.setparams(params)
            target.writeframes(pcm[start * stride : end * stride])
        slices.append((out.getvalue(), start, end))
    return slices
