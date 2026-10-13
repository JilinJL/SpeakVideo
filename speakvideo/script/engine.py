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
