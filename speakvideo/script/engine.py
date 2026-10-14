from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import ValidationError

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


def _noop(*args, **kwargs):
    return None


@dataclass
class LLMHooks:
    """Optional observers for one generation; providers that ignore them stay compatible."""

    on_text: Callable[[str, int], None] = _noop  # (content so far, reasoning chars)
    on_finish: Callable[[dict], None] = _noop  # diagnostics + full raw output
    log: Callable[[str], None] = _noop
    cancelled: Callable[[], bool] = lambda: False
    register_abort: Callable[[Callable[[], None] | None], None] = _noop


class LLMCancelled(RuntimeError):
    """The user stopped the generation; nothing was saved."""


class LLMOutputError(ValueError):
    """The model answered, but the answer cannot be used."""


class _Retryable(Exception):
    """Failure before any output; the next key or provider may succeed."""


def unique_json_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"模型 JSON 字段重复：{key}")
        result[key] = value
    return result


def _redact(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _error_body(response: httpx.Response, secrets: list[str]) -> str:
    """Provider's own error message, with any configured key masked."""
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(error, dict):
            parts = [str(error.get(k)) for k in ("code", "type", "message") if error.get(k)]
            text = " · ".join(parts) or json.dumps(error, ensure_ascii=False)
        else:
            text = str(error)
    except (ValueError, json.JSONDecodeError):
        text = response.text
    text = " ".join(_redact(text, secrets).split())
    return text[:400] or "无响应正文"


def _usage_text(usage: dict | None) -> str:
    if not usage:
        return ""
    details = usage.get("completion_tokens_details") or {}
    reasoning = usage.get("reasoning_tokens") or details.get("reasoning_tokens")
    parts = [
        f"{label} {usage[key]:,}"
        for key, label in (("prompt_tokens", "输入"), ("completion_tokens", "输出"))
        if isinstance(usage.get(key), int)
    ]
    if isinstance(reasoning, int):
        parts.append(f"其中推理 {reasoning:,}")
    return " / ".join(parts) + " tokens" if parts else ""


class OpenAICompatibleProvider:
    def __init__(self, settings: LLMSettings, hooks: LLMHooks | None = None):
        self.settings = settings.model_copy(deep=True)
        self.hooks = hooks or LLMHooks()
        self.last_endpoint = ""
        self._response: httpx.Response | None = None
        self._aborted = False

    def abort(self) -> None:
        """Called from another thread: close the open stream so the read returns now."""
        self._aborted = True
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing a stream mid-read is best effort
                pass

    def _stopped(self) -> bool:
        return self._aborted or self.hooks.cancelled()

    def complete(self, system: str, user: str, *, json_output: bool = False) -> str:
        endpoints = self.settings.endpoints()
        if not endpoints:
            raise ValueError("请在 LLM 配置中启用一个模型供应商")
        secrets = [
            key.get_secret_value()
            for endpoint in endpoints
            for key in endpoint.api_keys
            if key.get_secret_value()
        ]
        errors = []
        self.hooks.register_abort(self.abort)
        try:
            for endpoint in endpoints:
                payload = {
                    "model": endpoint.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                }
                if endpoint.max_tokens:
                    payload["max_tokens"] = endpoint.max_tokens
                if json_output and endpoint.json_mode:
                    payload["response_format"] = {"type": "json_object"}
                if endpoint.stream:
                    payload["stream"] = True
                keys = endpoint.api_keys or [None]
                for index, secret in enumerate(keys, 1):
                    if self._stopped():
                        raise LLMCancelled("已停止生成")
                    label = f"{endpoint.name or endpoint.id} / {endpoint.model}"
                    if len(keys) > 1:
                        label += f" · Key {index}"
                    self.hooks.log(
                        f"请求 {label} · {'流式' if endpoint.stream else '非流式'}"
                        f"{' · JSON Mode' if 'response_format' in payload else ''}"
                        f" · 输出上限 {f'{endpoint.max_tokens:,} tokens' if endpoint.max_tokens else '不限（由模型决定）'}"
                        f" · 系统提示 {len(system):,} 字 + 输入 {len(user):,} 字"
                    )
                    token = secret.get_secret_value() if secret else ""
                    try:
                        content = self._call(endpoint, payload, token, label, secrets)
                    except _Retryable as exc:
                        errors.append(f"{label}：{exc}")
                        self.hooks.log(f"{label} 不可用：{exc}")
                        continue
                    self.last_endpoint = f"{endpoint.name or endpoint.id} / {endpoint.model}"
                    return content
            raise RuntimeError("LLM 连接失败；" + "；".join(errors))
        finally:
            self.hooks.register_abort(None)
            self._response = None

    def _call(self, endpoint, payload, token, label, secrets) -> str:
        started = time.monotonic()
        record = {"endpoint": label, "model": endpoint.model, "max_tokens": endpoint.max_tokens}
        content: list[str] = []
        reasoning: list[str] = []
        state = {"finish": None, "usage": None, "done": False, "first": None, "id": None}
        error: Exception | None = None

        def emit(force=False):
            now = time.monotonic()
            if force or now - state.get("emitted", 0) > 0.12:
                state["emitted"] = now
                self.hooks.on_text("".join(content), sum(map(len, reasoning)))

        def take(choice: dict, delta: dict):
            piece = delta.get("content")
            think = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(piece, str) and piece:
                content.append(piece)
            if isinstance(think, str) and think:
                reasoning.append(think)
            if choice.get("finish_reason"):
                state["finish"] = choice["finish_reason"]
            if state["first"] is None and (content or reasoning):
                state["first"] = time.monotonic() - started
                self.hooks.log(
                    f"收到首个输出 · {state['first']:.1f} 秒"
                    + ("（模型先输出推理过程）" if reasoning and not content else "")
                )

        try:
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(endpoint.timeout_seconds), trust_env=False
                ) as client:
                    with client.stream(
                        "POST",
                        endpoint.base_url.rstrip("/") + "/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {token}"} if token else {},
                    ) as response:
                        self._response = response
                        if response.is_error:
                            response.read()
                            body = _error_body(response, secrets)
                            code = response.status_code
                            if code in {401, 403, 408, 429} or code >= 500:
                                raise _Retryable(f"HTTP {code} · {body}")
                            raise RuntimeError(f"LLM 拒绝了请求（{label}，HTTP {code}）：{body}")
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            # The provider ignored stream=true or streaming is off.
                            response.read()
                            body = response.json()
                            if not isinstance(body, dict) or not body.get("choices"):
                                raise LLMOutputError(
                                    "模型响应不符合 OpenAI 接口格式："
                                    + _redact(json.dumps(body, ensure_ascii=False), secrets)[:300]
                                )
                            state["id"], state["usage"] = body.get("id"), body.get("usage")
                            choice = body["choices"][0]
                            take(choice, choice.get("message") or {})
                            state["done"] = True
                        else:
                            for line in response.iter_lines():
                                if self._stopped():
                                    raise LLMCancelled("已停止生成")
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    state["done"] = True
                                    break
                                try:
                                    chunk = json.loads(data)
                                except json.JSONDecodeError:
                                    self.hooks.log(f"忽略无法解析的流式数据：{data[:120]}")
                                    continue
                                if chunk.get("error"):
                                    detail = chunk["error"]
                                    message = (
                                        detail.get(
                                            "message", json.dumps(detail, ensure_ascii=False)
                                        )
                                        if isinstance(detail, dict)
                                        else str(detail)
                                    )
                                    raise LLMOutputError(
                                        "模型在输出过程中返回错误："
                                        + _redact(message, secrets)[:400]
                                    )
                                state["id"] = state["id"] or chunk.get("id")
                                state["usage"] = chunk.get("usage") or state["usage"]
                                for choice in chunk.get("choices") or []:
                                    take(choice, choice.get("delta") or {})
                                    if choice.get("finish_reason"):
                                        state["done"] = True
                                emit()
            except Exception as exc:
                # Closing the stream from another thread surfaces as a read/stream error.
                if self._stopped() and not isinstance(exc, LLMCancelled):
                    raise LLMCancelled("已停止生成") from None
                if (
                    isinstance(exc, (LLMCancelled, LLMOutputError, _Retryable))
                    or type(exc) is RuntimeError
                ):
                    raise
                if isinstance(exc, ValueError):
                    raise LLMOutputError(f"模型响应不是合法 JSON：{exc}") from None
                if not isinstance(exc, (httpx.HTTPError, httpx.StreamError)):
                    raise
                kind = type(exc).__name__
                if not content and not reasoning:
                    raise _Retryable(f"{kind}（{exc or '无详情'}）") from None
                raise LLMOutputError(
                    f"流式输出中断（{kind}）：已收到 {sum(map(len, content)):,} 字正文"
                    f"{f'、{sum(map(len, reasoning)):,} 字推理' if reasoning else ''}，"
                    f"连接在 {endpoint.timeout_seconds:g} 秒无响应超时内断开或被关闭。"
                ) from None
            emit(force=True)
            text = "".join(content)
            thought = sum(map(len, reasoning))
            finish = state["finish"]
            tokens = (state["usage"] or {}).get("completion_tokens")
            if finish == "length":
                raise LLMOutputError(
                    "模型输出达到长度上限被截断（finish_reason=length）："
                    f"已输出 {len(text):,} 字正文{f'、{thought:,} 字推理' if thought else ''}"
                    f"{f'，共 {tokens:,} tokens' if isinstance(tokens, int) else ''}。"
                    + (
                        f"当前输出上限为 {endpoint.max_tokens:,} tokens（推理也计入），"
                        "可在 LLM 配置中清空“输出上限”以不限制。"
                        if endpoint.max_tokens
                        else "未设置输出上限，已达到模型或供应商自身的单次输出上限。"
                    )
                )
            if finish and ("filter" in finish or finish in {"sensitive", "safety", "refusal"}):
                raise LLMOutputError(
                    f"模型拒绝输出：被内容安全审核拦截（finish_reason={finish}），"
                    f"拦截前已输出 {len(text):,} 字。可调整话题措辞，或在 LLM 配置中换一个供应商。"
                )
            if finish and finish not in {"stop", "end_turn", "eos", "tool_calls"}:
                raise LLMOutputError(
                    f"模型异常结束（finish_reason={finish}），已输出 {len(text):,} 字。"
                )
            if not state["done"]:
                raise LLMOutputError(
                    f"流式输出意外结束：没有收到结束标记，已收到 {len(text):,} 字。"
                    "可能是网络或供应商中途断开。"
                )
            if not text.strip():
                raise LLMOutputError(
                    f"模型只输出了推理过程（{thought:,} 字），没有正文。"
                    if thought
                    else f"模型返回了空内容（finish_reason={finish or '无'}）。"
                )
            return text
        except Exception as exc:
            error = exc
            raise
        finally:
            record.update(
                request_id=state["id"],
                finish_reason=state["finish"],
                usage=state["usage"],
                usage_text=_usage_text(state["usage"]),
                first_output_seconds=state["first"],
                elapsed_seconds=time.monotonic() - started,
                content="".join(content),
                reasoning="".join(reasoning),
                error=None if error is None else str(error),
                error_type=None if error is None else type(error).__name__,
            )
            if not isinstance(error, _Retryable):
                self.hooks.on_finish(record)


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
        try:
            payload = json.loads(content, object_pairs_hook=unique_json_fields)
        except json.JSONDecodeError as exc:
            near = content[max(0, exc.pos - 40) : exc.pos + 40].replace("\n", "⏎")
            fence = "输出带有 Markdown 代码围栏；" if content.lstrip().startswith("```") else ""
            raise LLMOutputError(
                f"模型输出不是合法 JSON：{fence}{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列，"
                f"共 {len(content):,} 字），出错位置附近：{near}"
            ) from None
        try:
            generated = GeneratedScript.model_validate(payload, strict=True)
        except ValidationError as exc:
            problems = [
                f"{'.'.join(map(str, e['loc'])) or '顶层'}：{e['msg']}"
                for e in exc.errors(include_input=False)[:6]
            ]
            raise LLMOutputError(
                f"模型输出不符合台词格式（{exc.error_count()} 处）：" + "；".join(problems)
            ) from None
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
