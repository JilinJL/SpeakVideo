"""Streaming model calls: live output, stopping, and failure reasons people can act on."""

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from speakvideo.config import LLMSettings, load_settings
from speakvideo.script.engine import (
    LLMHooks,
    LLMOutputError,
    OpenAICompatibleProvider,
    ScriptEngine,
)
from speakvideo.script.stream import partial_lines
from speakvideo.web import create_app


def settings(**provider):
    return LLMSettings.model_validate(
        {
            "active_provider": "one",
            "providers": [
                {
                    "id": "one",
                    "name": "测试",
                    "base_url": "https://llm.example/v1",
                    "model": "m",
                    "api_keys": ["secret-key-123"],
                    **provider,
                }
            ],
        }
    )


def sse(*chunks, done=True):
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return body + ("data: [DONE]\n\n" if done else "")


def delta(content=None, finish="", reasoning=None, usage=None):
    d = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d["reasoning"] = reasoning
    chunk = {"id": "req-1", "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    return chunk


REAL_CLIENT = httpx.Client


def use(monkeypatch, handler):
    monkeypatch.setattr(
        "speakvideo.script.engine.httpx.Client",
        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler)),
    )


def stream_response(text, status=200):
    return httpx.Response(status, headers={"content-type": "text/event-stream"}, text=text)


def test_stream_collects_text_omits_max_tokens_and_reports_progress(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return stream_response(
            sse(
                delta(reasoning="先想想"),
                delta("p1：你好"),
                delta("？\np2：好。", "stop", usage={"prompt_tokens": 9, "completion_tokens": 7}),
            )
        )

    use(monkeypatch, handler)
    texts, finished = [], []
    hooks = LLMHooks(on_text=lambda t, r: texts.append((t, r)), on_finish=finished.append)
    assert OpenAICompatibleProvider(settings(), hooks).complete("s", "u") == "p1：你好？\np2：好。"
    assert seen["stream"] is True and "max_tokens" not in seen
    assert texts[-1] == ("p1：你好？\np2：好。", 3)
    record = finished[0]
    assert record["finish_reason"] == "stop" and record["reasoning"] == "先想想"
    assert record["usage_text"] == "输入 9 / 输出 7 tokens" and record["request_id"] == "req-1"


def test_failure_reasons_are_specific(monkeypatch):
    cases = {
        "length": (sse(delta("一半"), delta("", "length")), "长度上限被截断"),
        "content_filter": (sse(delta("开头"), delta("", "content_filter")), "内容安全审核"),
        "no_done": (sse(delta("没说完"), done=False), "没有收到结束标记"),
        "reasoning_only": (sse(delta(reasoning="想了很多"), delta("", "stop")), "只输出了推理"),
    }
    for name, (body, expected) in cases.items():
        use(monkeypatch, lambda request, body=body: stream_response(body))
        finished = []
        with pytest.raises(LLMOutputError, match=expected):
            OpenAICompatibleProvider(settings(), LLMHooks(on_finish=finished.append)).complete(
                "s", "u"
            )
        assert finished[0]["error"], name

    use(monkeypatch, lambda request: stream_response(sse(delta("x"), delta("", "length"))))
    with pytest.raises(LLMOutputError, match="当前输出上限为 4,000 tokens"):
        OpenAICompatibleProvider(settings(max_tokens=4000)).complete("s", "u")


def test_http_errors_show_provider_message_without_secret(monkeypatch):
    def handler(request):
        return httpx.Response(
            400, json={"error": {"code": "bad", "message": "prompt 违规 key secret-key-123"}}
        )

    use(monkeypatch, handler)
    with pytest.raises(RuntimeError) as error:
        OpenAICompatibleProvider(settings()).complete("s", "u")
    assert "HTTP 400" in str(error.value) and "prompt 违规" in str(error.value)
    assert "secret-key-123" not in str(error.value)

    use(monkeypatch, lambda request: httpx.Response(429, json={"error": {"message": "限流"}}))
    with pytest.raises(RuntimeError, match="HTTP 429 · 限流"):
        OpenAICompatibleProvider(settings()).complete("s", "u")


def test_invalid_json_names_position_and_schema_fields():
    class Provider:
        def __init__(self, text):
            self.text = text

        def complete(self, system, user, *, json_output=False):
            return self.text

    with pytest.raises(LLMOutputError, match="不是合法 JSON.*第 1 行"):
        ScriptEngine(Provider('{"lines": [')).structure("话题", [], "")
    with pytest.raises(LLMOutputError, match="Markdown 代码围栏"):
        ScriptEngine(Provider("```json\n{}\n```")).structure("话题", [], "")
    with pytest.raises(LLMOutputError, match="不符合台词格式.*schema_version"):
        ScriptEngine(Provider("{}")).structure("话题", [], "")


def test_partial_lines_reads_only_closed_objects():
    text = (
        '{"cast": [{"slot": "p1"}], "lines": [{"id": "L001", "slot": "p1", "words": "带 }'
        ' 括号\\"引号", "mood": "normal", "pause_after_ms": 300}, {"id": "L002", "slot": "p2", "wo'
    )
    assert partial_lines(text) == [
        {
            "id": "L001",
            "slot": "p1",
            "words": '带 } 括号"引号',
            "mood": "normal",
            "pause_after_ms": 300,
        }
    ]
    assert partial_lines('{"cast": []') == []


def test_stop_closes_stream_and_keeps_project(project, monkeypatch):
    """Stopping mid-stream returns immediately, saves nothing and marks the task paused."""
    root = project.settings.paths.root
    (root / ".cache").mkdir(exist_ok=True)
    (root / ".cache" / "llm.local.json").write_text(
        settings().model_dump_json().replace("**********", "secret-key-123")
    )
    app_settings = load_settings(root)
    app_settings.tts.mode = "demo"
    first_chunk = threading.Event()

    class SlowStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield sse(delta('{"lines": [{"id": "L001"'), done=False).encode()
            first_chunk.set()
            while not self.closed:
                time.sleep(0.02)
            raise httpx.ReadError("closed")

        def close(self):
            self.closed = True

    use(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=SlowStream()
        ),
    )
    before = (project.root / "script.json").read_bytes()
    with TestClient(create_app(app_settings)) as client:
        task = client.post("/api/projects/test/actions", json={"action": "generate"}).json()
        assert first_chunk.wait(5)
        deadline = time.monotonic() + 5
        while not next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])[
            "streaming"
        ]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        started = time.monotonic()
        assert client.post(f"/api/tasks/{task['id']}/pause").status_code == 200
        while True:
            state = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
            if state["status"] not in {"queued", "running"}:
                break
            assert time.monotonic() - started < 3, "停止后任务应立即结束"
            time.sleep(0.02)
        assert state["status"] == "paused" and "未保存" in state["error"]
        assert any("请求 测试 / m" in entry["message"] for entry in state["log"])
        raw = client.get(f"/api/tasks/{task['id']}/llm").json()
        assert raw["error_type"] == "LLMCancelled" and '"L001"' in raw["content"]
    assert (project.root / "script.json").read_bytes() == before


def test_task_stream_sends_lines_and_failure_details(project, monkeypatch):
    script = project.script()
    output = json.dumps({**script.model_dump(), "meta": {}}, ensure_ascii=False)
    half = output.index('"lines"') + 200

    def complete(self, system, user, *, json_output=False):
        self.hooks.on_text(output[:half], 0)
        self.hooks.on_text(output, 0)
        self.hooks.on_finish({"content": output, "finish_reason": "stop", "endpoint": "x"})
        return output

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", complete)
    with TestClient(create_app(project.settings)) as client:
        task = client.post("/api/projects/test/actions", json={"action": "generate"}).json()
        with client.stream("GET", f"/api/tasks/{task['id']}/stream") as response:
            body = "".join(response.iter_text())
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
        streamed = [line for event in events for line in event.get("lines", [])]
        assert [line["id"] for line in streamed] == [line.id for line in script.lines]
        assert '"status": "succeeded"' in body
        finished = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
        assert finished["llm"]["finish_reason"] == "stop" and finished["llm"]["file"]

    def broken(self, system, user, *, json_output=False):
        self.hooks.on_finish({"content": "{oops", "finish_reason": "stop", "error": "bad"})
        return "{oops"

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", broken)
    with TestClient(create_app(project.settings)) as client:
        task = client.post("/api/projects/test/actions", json={"action": "generate"}).json()
        deadline = time.monotonic() + 5
        while (state := next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"]))[
            "status"
        ] in {"queued", "running"}:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert state["status"] == "failed" and "不是合法 JSON" in state["error"]
        assert any("LLMOutputError" in entry["message"] for entry in state["log"])
        assert client.get(f"/api/tasks/{task['id']}/llm").json()["content"] == "{oops"


def test_old_8192_default_is_lifted_on_load(tmp_path):
    (tmp_path / ".cache").mkdir()
    body = json.loads(settings(max_tokens=8192).model_dump_json())
    for provider in body["providers"]:
        provider.pop("stream")
    (tmp_path / ".cache" / "llm.local.json").write_text(json.dumps(body))
    loaded = load_settings(tmp_path).llm.providers[0]
    assert loaded.max_tokens is None and loaded.stream
