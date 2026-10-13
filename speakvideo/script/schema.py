"""剧本 JSON 契约。

设计要点：

- script.json（剧本）与 cast.json（声音绑定）彻底分离。剧本不引用任何
  具体音色，只引用抽象槽位 slot 与封闭 mood。
- line.id 是稳定标识，绝不能用数组下标。否则插一句后面全错位，
  已生成音频全部作废，改顺序也无法零成本。
- 模板通用性只保留 cast + lines 两个原语：单人口播是 cast 长度 1，
  多人座谈是 cast 长度 N，lines 结构完全不变。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .moods import DEFAULT_MOOD, MOODS

SCHEMA_VERSION = "1.0"
TemplateName = Literal["duo_dialogue"]


class CastMember(BaseModel):
    """剧本中的一个说话人槽位。persona 供 LLM 写台词时保持一致人设。"""

    model_config = ConfigDict(extra="forbid")

    slot: str = Field(min_length=1, max_length=30, pattern=r"^[\w-]+$")
    persona: str = Field(default="", max_length=2000)


class ScriptLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=30, pattern=r"^[\w-]+$")
    slot: str = Field(min_length=1, max_length=30, pattern=r"^[\w-]+$")
    words: str = Field(min_length=1)
    mood: str = DEFAULT_MOOD
    pause_after_ms: int = Field(default=300, ge=0, le=10000)

    @field_validator("mood")
    @classmethod
    def _check_mood(cls, v: str) -> str:
        if v not in MOODS:
            allowed = "、".join(MOODS)
            raise ValueError(f"mood 必须是封闭词表中的值：{allowed}")
        return v


class ScriptPrompts(BaseModel):
    """Per-project writing overrides. Empty values use the current defaults."""

    model_config = ConfigDict(extra="forbid")

    brief: str = Field(default="", max_length=20000)
    structure: str = Field(default="", max_length=20000)


class ScriptMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    topic: str = ""
    keywords: list[str] = Field(default_factory=list)
    brief: str = ""
    guidance: str = ""
    prompts: ScriptPrompts = Field(default_factory=ScriptPrompts)


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    template: TemplateName = "duo_dialogue"
    meta: ScriptMeta = Field(default_factory=ScriptMeta)
    cast: list[CastMember] = Field(min_length=1)
    lines: list[ScriptLine] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if v != SCHEMA_VERSION:
            raise ValueError(f"仅支持 schema_version={SCHEMA_VERSION}，收到 {v}")
        return v


class GeneratedMeta(BaseModel):
    """The provider must leave metadata to the workbench."""

    model_config = ConfigDict(extra="forbid")


class GeneratedCastMember(CastMember):
    slot: Literal["p1", "p2"]
    persona: str = Field(max_length=2000)


class GeneratedLine(ScriptLine):
    """LLM output requires every field; saved scripts keep their legacy defaults."""

    id: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    slot: Literal["p1", "p2"]
    words: str = Field(min_length=3, max_length=200)

    @field_validator("words", mode="before")
    @classmethod
    def _strip_words(cls, value):
        return value.strip() if isinstance(value, str) else value

    mood: str = Field(json_schema_extra={"enum": list(MOODS)})
    pause_after_ms: int = Field(ge=0, le=10000)


class GeneratedScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    template: TemplateName
    meta: GeneratedMeta
    cast: list[GeneratedCastMember] = Field(min_length=2, max_length=2)
    lines: list[GeneratedLine] = Field(min_length=1)

    @classmethod
    def response_schema(cls, max_line_chars: int) -> dict[str, Any]:
        if not 3 <= max_line_chars <= 200:
            raise ValueError("单行台词上限必须为 3–200 字")
        schema = cls.model_json_schema()
        schema["$defs"]["GeneratedLine"]["properties"]["words"]["maxLength"] = max_line_chars
        return schema


class VoiceBinding(BaseModel):
    """一个槽位的声音绑定：角色名 + mood 到 pt 情感名的映射。"""

    model_config = ConfigDict(extra="forbid")

    role: str = ""
    mood_map: dict[str, str] = Field(default_factory=dict)
    default_emotion: str = "平静"


CastBinding = dict[str, VoiceBinding]


def slots(script: Script) -> list[str]:
    return [m.slot for m in script.cast]


def validate_ids(script: Script) -> list[str]:
    """返回 id 重复与 slot 未知的问题列表，空列表表示通过。"""
    problems: list[str] = []
    seen: set[str] = set()
    known = set(slots(script))
    if len(known) != len(script.cast):
        problems.append("cast.slot 重复")
    for line in script.lines:
        length = len(line.words.strip())
        if length < 3 or length > 200:
            problems.append(f"{line.id} 台词 {length} 字，必须为 3–200 字")
        if line.id in seen:
            problems.append(f"line.id 重复: {line.id}")
        seen.add(line.id)
        if line.slot not in known:
            problems.append(f"{line.id} 引用了未定义的 slot: {line.slot}")
    return problems


def to_jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


# IDs become filenames; reject traversal at the schema boundary.
