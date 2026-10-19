"""Short subtitle display intervals, independent of indivisible speech audio."""

from __future__ import annotations

import re

SUBTITLE_TIMING_VERSION = 1


def text_chunks(text: str, limit: int = 32) -> list[str]:
    """Keep sentences separate; divide long clauses near punctuation."""
    chunks = []
    for sentence in re.findall(r"[^\n。！？!?；;]+[。！？!?；;]*|[。！？!?；;]+", text):
        rest = sentence.strip()
        while len(rest) > limit:
            choices = [i + 1 for i, char in enumerate(rest[:limit]) if char in "，,、：: "]
            cut = choices[-1] if choices and choices[-1] >= limit // 3 else limit
            chunks.append(rest[:cut].strip())
            rest = rest[cut:].lstrip()
        if rest:
            chunks.append(rest)
    return chunks


def timed_parts(start: float, end: float, parts: list) -> list[tuple[float, float, object]]:
    """Allocate display times only. These are not measured speech boundaries."""
    weights = [max(1, len("".join(part) if isinstance(part, list) else part)) for part in parts]
    total, consumed, result = sum(weights), 0, []
    for part, weight in zip(parts, weights):
        left = start + (end - start) * consumed / total
        consumed += weight
        right = end if consumed == total else start + (end - start) * consumed / total
        result.append((left, right, part))
    return result


def subtitle_cues(row: dict) -> list[tuple[float, float, str]]:
    parts = [
        chunk for text in row.get("subtitle_texts", [row["words"]]) for chunk in text_chunks(text)
    ]
    return timed_parts(row["start"], row["end"], parts)
