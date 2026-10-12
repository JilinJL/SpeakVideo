"""封闭 mood 词表。

mood 是剧本层的语义标签，与 pt 文件名解耦：写剧本时不需要知道
某个角色有哪些音色情感，那是 cast.json 的 mood_map 负责的事。

词表封闭的原因：LLM 自由发挥会发明新词导致漏映射，集中在此处
便于 UI 做下拉、便于预检报错。
"""

from __future__ import annotations

MOODS: tuple[str, ...] = (
    "normal",
    "serious",
    "sad",
    "playful",
    "excited",
    "curious",
    "angry",
    "sarcastic",
)

MOOD_LABELS: dict[str, str] = {
    "normal": "平静",
    "serious": "严肃",
    "sad": "低落",
    "playful": "调侃",
    "excited": "兴奋",
    "curious": "追问",
    "angry": "愤怒",
    "sarcastic": "反讽",
}

DEFAULT_MOOD = "normal"


def is_valid_mood(mood: str) -> bool:
    return mood in MOODS


def mood_label(mood: str) -> str:
    return MOOD_LABELS.get(mood, mood)


def mood_vocab_for_prompt() -> str:
    """生成给 LLM 的词表说明，一次列出全部合法值。"""
    return "、".join(f"{k}（{v}）" for k, v in MOOD_LABELS.items())
