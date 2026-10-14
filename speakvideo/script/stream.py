"""Read finished line objects out of a JSON script that is still being generated."""

from __future__ import annotations

import json

LINE_FIELDS = ("id", "slot", "words", "mood", "pause_after_ms")


def partial_lines(text: str) -> list[dict]:
    """Return every complete object in the top-level ``lines`` array so far.

    The model streams one JSON document; a line is shown as soon as its closing
    brace arrives. Anything that does not parse yet is simply not returned.
    """
    key = text.find('"lines"')
    if key < 0:
        return []
    start = text.find("[", key)
    if start < 0:
        return []
    lines, depth, begin, in_string, escaped = [], 0, -1, False, False
    for index in range(start + 1, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                begin = index
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin >= 0:
                try:
                    item = json.loads(text[begin : index + 1])
                except json.JSONDecodeError:
                    item = None
                if isinstance(item, dict):
                    lines.append({k: item.get(k) for k in LINE_FIELDS})
                begin = -1
        elif ch == "]" and depth == 0:
            break
    return lines
