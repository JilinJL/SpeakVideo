from __future__ import annotations

import json
from typing import Protocol

import httpx

from ..config import LLMSettings
from .prompts import (
    DEFAULT_BRIEF_PROMPT,
    DEFAULT_PERSONAS,
    DEFAULT_STRUCTURE_PROMPT,
    DIALOGUE_QUALITY_CONTRACT,
    structure_contract,
)
from .quality import question_particles
from .schema import CastMember, GeneratedScript, Script, ScriptPrompts, validate_ids


class LLMProvider(Protocol):
    def complete(self, system: str, user: str, *, json_output: bool = False) -> str: ...


def unique_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"模型 JSON 字段重复：{key}")
        result[key] = value
    return result


class OpenAICompatibleProvider:
    def __init__(self, settings: LLMSettings):
        self.settings = settings.model_copy(deep=True)
        self.last_endpoint = ""

    def complete(self, system: str, user: str, *, json_output: bool = False) -> str:
        endpoints = self.settings.endpoints()
        if not endpoints:
            raise ValueError("请在 LLM 配置中启用一个模型供应商")
        errors = []
        response = None
        for endpoint in endpoints:
            payload = {
                "model": endpoint.model,
                "max_tokens": endpoint.max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if json_output and endpoint.json_mode:
                payload["response_format"] = {"type": "json_object"}
            for secret in endpoint.api_keys or [None]:
                token = secret.get_secret_value() if secret else ""
                try:
                    with httpx.Client(timeout=endpoint.timeout_seconds, trust_env=False) as client:
                        candidate = client.post(
                            endpoint.base_url.rstrip("/") + "/chat/completions",
                            json=payload,
                            headers={"Authorization": f"Bearer {token}"} if token else {},
                        )
                except httpx.HTTPError as exc:
                    errors.append(f"{endpoint.name or endpoint.id}: {type(exc).__name__}")
                    continue
                if candidate.is_error:
                    errors.append(f"{endpoint.name or endpoint.id}: HTTP {candidate.status_code}")
                    if (
                        candidate.status_code in {401, 403, 408, 429}
                        or candidate.status_code >= 500
                    ):
                        continue
                    raise RuntimeError("LLM 请求参数被拒绝；" + errors[-1])
                response = candidate
                self.last_endpoint = f"{endpoint.name or endpoint.id} / {endpoint.model}"
                break
            if response is not None:
                break
        if response is None:
            raise RuntimeError("LLM 连接失败；" + "；".join(errors))
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ValueError("LLM 输出被截断或拒绝，本次剧本未保存")
        content = choice["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LLM 未返回可用文本")
        return content


class ScriptEngine:
    def __init__(self, provider: LLMProvider, max_line_chars: int = 200):
        if not 3 <= max_line_chars <= 200:
            raise ValueError("单行台词上限必须为 3–200 字")
        self.provider = provider
        self.max_line_chars = max_line_chars

    def brief(
        self,
        topic: str,
        keywords: list[str],
        guidance: str = "",
        *,
        cast: list[CastMember] | None = None,
        prompts: ScriptPrompts | None = None,
    ) -> str:
        personas = cast if cast is not None else self.default_cast()
        prompts = prompts or ScriptPrompts()
        return question_particles(
            self.provider.complete(
                (prompts.brief.strip() or DEFAULT_BRIEF_PROMPT)
                + "\n\n"
                + DIALOGUE_QUALITY_CONTRACT,
                json.dumps(
                    {
                        "topic": topic,
                        "keywords": keywords,
                        "guidance": guidance,
                        "cast": [member.model_dump() for member in personas],
                    },
                    ensure_ascii=False,
                ),
            )
        )

    @staticmethod
    def default_cast() -> list[CastMember]:
        return [
            CastMember(slot=slot, persona=persona) for slot, persona in DEFAULT_PERSONAS.items()
        ]

    def extend_brief(self, script: Script) -> str:
        if not script.meta.brief.strip():
            raise ValueError("请先填写或生成草稿，再扩展对话")
        continuation = self.provider.complete(
            (script.meta.prompts.brief.strip() or DEFAULT_BRIEF_PROMPT)
            + "\n\n"
            + DIALOGUE_QUALITY_CONTRACT
            + "\n\n本次任务是扩展已有对话。保留已有内容，由程序追加你的输出。"
            "只输出新增对话，不重复或重写原草稿。原话题已经有结论时，"
            "以补充原因、边界、反例和生活场景切入，不撤销已有结论，不无限引入新问题。"
            "新增内容也必须回到原话题，给出明确结论和自然收尾，最后不能停在未回答的追问上。"
            "遵守原人设和本次要求，用 p1：、p2：标记说话人。",
            json.dumps(
                {
                    "topic": script.meta.topic,
                    "keywords": script.meta.keywords,
                    "guidance": script.meta.guidance,
                    "cast": [member.model_dump() for member in script.cast],
                    "brief": script.meta.brief,
                },
                ensure_ascii=False,
            ),
        ).strip()
        if not continuation:
            raise ValueError("模型未返回续写内容，原草稿已保留")
        return script.meta.brief + "\n\n" + question_particles(continuation)

    def structure(
        self,
        topic: str,
        keywords: list[str],
        brief: str,
        guidance: str = "",
        *,
        cast: list[CastMember] | None = None,
        prompts: ScriptPrompts | None = None,
    ) -> Script:
        personas = cast if cast is not None else self.default_cast()
        if len(personas) != 2 or {member.slot for member in personas} != {"p1", "p2"}:
            raise ValueError("duo_dialogue 需要 p1 与 p2")
        prompts = prompts or ScriptPrompts()
        writing = prompts.structure.strip() or DEFAULT_STRUCTURE_PROMPT
        content = self.provider.complete(
            writing + "\n\n" + structure_contract(self.max_line_chars),
            json.dumps(
                {
                    "topic": topic,
                    "keywords": keywords,
                    "brief": brief,
                    "guidance": guidance,
                    "cast": [member.model_dump() for member in personas],
                },
                ensure_ascii=False,
            ),
            json_output=True,
        )
        # Strict provider-only schema: no omitted defaults, coercion, or extra metadata.
        payload = json.loads(content, object_pairs_hook=unique_json_fields)
        generated = GeneratedScript.model_validate(payload, strict=True)
        script = Script.model_validate(generated.model_dump())
        for line in script.lines:
            line.words = question_particles(line.words)
        errors = validate_ids(script)
        if set(m.slot for m in script.cast) != {"p1", "p2"}:
            errors.append("duo_dialogue 需要 p1 与 p2")
        if not script.lines or any(len(line.words) > self.max_line_chars for line in script.lines):
            errors.append("空剧本或台词超过长度上限")
        if errors:
            raise ValueError("；".join(errors))
        script.meta.topic, script.meta.keywords = topic, keywords
        script.meta.brief, script.meta.guidance = brief, guidance
        script.meta.prompts = prompts.model_copy(deep=True)
        script.cast = [member.model_copy(deep=True) for member in personas]
        return script
