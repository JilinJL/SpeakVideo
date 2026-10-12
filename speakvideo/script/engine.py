from __future__ import annotations

import json
from typing import Protocol

import httpx

from ..config import LLMSettings
from .moods import mood_vocab_for_prompt
from .schema import Script, validate_ids


class LLMProvider(Protocol):
    def complete(self, system: str, user: str, *, json_output: bool = False) -> str: ...


class OpenAICompatibleProvider:
    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def complete(self, system: str, user: str, *, json_output: bool = False) -> str:
        if not self.settings.model:
            raise ValueError("请配置 SPEAKVIDEO_LLM_MODEL 和供应商 base_url/API key")
        payload = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_output and self.settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        token = self.settings.api_key.get_secret_value()
        with httpx.Client(timeout=self.settings.timeout_seconds) as client:
            response = client.post(
                self.settings.base_url.rstrip("/") + "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
        if response.is_error:
            raise RuntimeError(f"LLM 返回 HTTP {response.status_code}；请检查供应商配置")
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ValueError("LLM 输出被截断或拒绝，本次剧本未保存")
        content = choice["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LLM 未返回可用文本")
        return content


class ScriptEngine:
    def __init__(self, provider: LLMProvider, max_line_chars: int = 100):
        self.provider = provider
        self.max_line_chars = max_line_chars

    def brief(self, topic: str, keywords: list[str], guidance: str = "") -> str:
        return self.provider.complete(
            "你是中文双人对谈节目的编剧。写详细编剧阐述，包含核心论点与反差、包袱抛接位置、"
            "p1/p2 人设与语言差异、节奏停顿、金句候选。只输出可编辑阐述，不输出 JSON。",
            json.dumps(
                {"topic": topic, "keywords": keywords, "guidance": guidance}, ensure_ascii=False
            ),
        )

    def structure(self, topic: str, keywords: list[str], brief: str, guidance: str = "") -> Script:
        content = self.provider.complete(
            "将编剧阐述翻译成严格 JSON 剧本，不添加具体角色音色。template=duo_dialogue；cast 仅 p1/p2；"
            f"mood 只能使用：{mood_vocab_for_prompt()}。每行台词不超过 {self.max_line_chars} 字。"
            "每行 id 必须唯一且稳定（例如 L001），仅字母数字下划线横线。"
            "pause_after_ms 是非负整数。遵守这个 JSON Schema："
            + json.dumps(Script.model_json_schema(), ensure_ascii=False),
            json.dumps(
                {"topic": topic, "keywords": keywords, "brief": brief, "guidance": guidance},
                ensure_ascii=False,
            ),
            json_output=True,
        )
        script = Script.model_validate_json(content)
        errors = validate_ids(script)
        if set(m.slot for m in script.cast) != {"p1", "p2"}:
            errors.append("duo_dialogue 需要 p1 与 p2")
        if not script.lines or any(len(line.words) > self.max_line_chars for line in script.lines):
            errors.append("空剧本或台词超过长度上限")
        if errors:
            raise ValueError("；".join(errors))
        script.meta.topic, script.meta.keywords = topic, keywords
        script.meta.brief, script.meta.guidance = brief, guidance
        return script
