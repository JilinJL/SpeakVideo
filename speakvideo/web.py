from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audio import audio_current, build_audio
from .config import RuntimeSettings, Settings, TTSSettings
from .connections import connection_status
from .jobs import SynthesisOptions, group_audio, report, synthesis_options, synthesize
from .llm_config import public_llm, save_llm
from .processing import ffmpeg
from .project import Project
from .script.engine import LLMCancelled, LLMHooks, OpenAICompatibleProvider, ScriptEngine
from .script.moods import MOOD_LABELS
from .script.prompts import default_prompts, global_prompts, save_global_prompts, structure_contract
from .script.schema import Script, ScriptPrompts, VoiceBinding, validate_ids
from .script.stream import partial_lines
from .storage import atomic_bytes, lock, read_json, write_json
from .tts.client import TTSClient, TTSError, wav_info
from .tts.grouping import speech_groups
from .video import (
    SUBTITLE_COLORS,
    OverlayEdit,
    VideoEdit,
    asset_path,
    cover_current,
    cover_picture,
    default_clip,
    edit_config,
    render_cover,
    render_video,
    validate_asset,
    video_config,
    video_current,
    video_timeline,
)
from .voice_previews import VoicePreviews
from .voices import VoiceStore
from .workflow import advance, extend_brief, generate_brief, generate_script


class NewProject(BaseModel):
    name: str
    topic: str = ""
    keywords: list[str] = Field(default_factory=list)


class Edit(BaseModel):
    script: Script
    cast: dict[str, VoiceBinding]


class Action(BaseModel):
    action: str
    line_id: str | None = None
    line_ids: list[str] = Field(default_factory=list, max_length=1000)
    concurrency: int | None = Field(default=None, ge=1, le=6, strict=True)


class MixEdit(BaseModel):
    bgm_volume: float = Field(default=0.18, ge=0, le=1)
    remove_bgm: bool = False


class VoiceAction(BaseModel):
    action: str
    role: str = ""
    emotion: str = "平静"
    text: str = ""
    instruct: str = ""
    reference_id: str = ""
    overwrite: bool = False

    @model_validator(mode="after")
    def require_voice_inputs(self):
        if self.action in {"create", "design", "preview"}:
            self.text = self.text.strip()
            if not self.text:
                raise ValueError("请填写对应台词" if self.action != "preview" else "请填写试听台词")
        if self.action == "create":
            import re

            if not re.fullmatch("[a-f0-9]{32}\\.wav", self.reference_id):
                raise ValueError("请提供角色原声")
        return self


class VoiceEdit(BaseModel):
    role: str
    emotion: str
    ref_text: str = ""
    notes: str = ""


class ReferenceCrop(BaseModel):
    reference_id: str = Field(pattern="^[a-f0-9]{32}\\.wav$")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)


class TTSModeEdit(BaseModel):
    mode: str = Field(pattern="^(managed|direct|demo)$")
    local_url: str = "http://127.0.0.1:8001"


LLM_LABELS = {"brief", "extend", "generate", "llm_test"}


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_dirs()
    if settings.tts.mode == "managed":
        VoiceStore(settings).import_examples(first_run=True)
    app = FastAPI(title="SpeakVideo", version="0.1.0")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    tasks: dict[str, dict] = {}
    submission_lock = threading.RLock()
    events: dict[str, threading.Event] = {}
    streams: dict[str, dict] = {}
    aborts: dict[str, object] = {}
    llm_runs = settings.paths.cache / "llm-runs"
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speakvideo")
    runtime_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speakvideo-runtime")
    from .inference.manager import RuntimeManager

    runtime_manager = RuntimeManager(settings)
    app.state.runtime_manager = runtime_manager
    task_context = threading.local()
    voice_previews = VoicePreviews(settings.paths.root)
    voice_previews.migrate(settings.paths.cache)

    def task_progress(**fields):
        item = task_context.item
        item["progress"] = {**item.get("progress", {}), **fields}
        if fields.get("kind") == "stage":
            for key in ("completed", "total", "failed", "current_line"):
                item["progress"].pop(key, None)
        write_json(settings.paths.cache / f"task-{item['id']}.json", item)

    def line_progress(item):
        p = Project(settings, item["project"])
        manifest = read_json(p.root / "manifest.json", {"lines": {}})
        ids = item.get("line_ids", [line.id for line in p.script().lines])
        records = [manifest.get("lines", {}).get(line_id, {}) for line_id in ids]
        active_groups = len(
            {
                record.get("group_id", key)
                for (key, record) in zip(ids, records)
                if record.get("status") == "running"
            }
        )
        valid_ids = set(report(p, False)["reusable_ids"])

        def fresh(key, record):
            return record.get("attempts", 0) > item.get("line_attempts", {}).get(
                key, 0
            ) or record.get("fingerprint") != item.get("line_fingerprints", {}).get(key)

        if item["label"] == "retry":
            valid_ids = {
                key
                for (key, record) in zip(ids, records)
                if key in valid_ids and fresh(key, record)
            }
        running_ids = [
            key
            for (key, record) in zip(ids, records)
            if record.get("status") == "running"
            and (item["label"] != "retry" or fresh(key, record))
        ]
        return {
            "kind": "lines",
            "stage": "正在按连续角色分组合成配音",
            "active_groups": active_groups,
            "concurrency": item.get("concurrency", manifest.get("concurrency", 1)),
            "completed": sum((line_id in valid_ids for line_id in ids)),
            "completed_ids": [key for key in ids if key in valid_ids],
            "total": len(ids),
            "failed": sum((r.get("status") == "failed" for r in records)),
            "current_line": next(
                (key for (key, record) in zip(ids, records) if record.get("status") == "running"),
                None,
            ),
            "running_ids": running_ids,
        }

    def voice_store():
        return VoiceStore(settings)

    def inventory_assets(force=False):
        return [{**v, "source": "local", "editable": True} for v in voice_store().details()]

    for path in settings.paths.cache.glob("task-*.json"):
        item = read_json(path)
        if item["status"] in {"queued", "running"}:
            item.update(status="interrupted", error="服务重启；重新提交即可从 manifest 断点恢复")
            write_json(path, item)
        tasks[item["id"]] = item

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "拒绝跨站写请求"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def bad_runtime(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def project(name: str):
        p = Project(settings, name)
        if not (p.root / "script.json").exists():
            raise HTTPException(404, "工程不存在")
        return p

    def task_log(item: dict, message: str):
        entry = {"at": time.time(), "message": message}
        item["log"] = [*item.get("log", []), entry][-80:]
        write_json(settings.paths.cache / f"task-{item['id']}.json", item)

    def llm_hooks(item: dict, kind: str) -> LLMHooks:
        """Wire one model call to the task: live text, log lines, stop and raw output."""
        key = item["id"]
        previous = streams.get(key, {})
        live = streams[key] = {
            "kind": kind,
            "phase": previous.get("phase", 0) + 1,
            "text": "",
            "lines": [],
            "reasoning": 0,
            "version": previous.get("version", 0) + 1,
        }

        def on_text(text: str, reasoning: int):
            live.update(text=text, reasoning=reasoning, version=live["version"] + 1)
            if kind == "generate":
                live["lines"] = partial_lines(text)
            item.setdefault("progress", {})["stage"] = (
                f"正在生成台词 · 已输出 {len(live['lines'])} 行"
                if kind == "generate" and text
                else f"正在{('续写' if kind == 'extend' else '撰写')}草稿 · 已输出 {len(text):,} 字"
                if kind in {"brief", "extend"} and text
                else f"模型思考中 · 已推理 {reasoning:,} 字"
                if reasoning
                else item.get("progress", {}).get("stage", "等待模型响应")
            )

        def on_finish(record: dict):
            atomic_bytes(
                llm_runs / f"{key}-{live['phase']}.json",
                json.dumps(
                    {"task": key, "kind": kind, **record}, ensure_ascii=False, indent=2
                ).encode(),
            )
            summary = {
                k: record.get(k)
                for k in (
                    "endpoint",
                    "finish_reason",
                    "usage_text",
                    "first_output_seconds",
                    "elapsed_seconds",
                    "error",
                    "request_id",
                )
            }
            summary.update(
                kind=kind,
                content_chars=len(record.get("content") or ""),
                reasoning_chars=len(record.get("reasoning") or ""),
                output_tail=(record.get("content") or "")[-600:],
                file=f".cache/llm-runs/{key}-{live['phase']}.json",
            )
            item["llm"] = summary
            done = (
                f"模型结束 · finish_reason={record.get('finish_reason') or '无'} · 正文 {summary['content_chars']:,} 字"
                + (
                    f" · 推理 {summary['reasoning_chars']:,} 字"
                    if summary["reasoning_chars"]
                    else ""
                )
                + (f" · {summary['usage_text']}" if summary["usage_text"] else "")
                + f" · 用时 {record.get('elapsed_seconds') or 0:.1f} 秒"
            )
            task_log(item, done)

        def register(abort):
            if abort is None:
                aborts.pop(key, None)
            else:
                aborts[key] = abort

        return LLMHooks(
            on_text=on_text,
            on_finish=on_finish,
            log=lambda message: task_log(item, message),
            cancelled=events[key].is_set,
            register_abort=register,
        )

    def submit(
        label: str,
        fn,
        project_name: str | None = None,
        line_id: str | None = None,
        concurrency: int | None = None,
        line_ids: list[str] | None = None,
    ):
        with submission_lock:
            return submit_locked(label, fn, project_name, line_id, concurrency, line_ids)

    def submit_locked(label, fn, project_name, line_id, concurrency, line_ids):
        gpu_labels = {"sync", "create", "design", "preview", "synth", "retry", "run"}
        if label.startswith("runtime_") or label in gpu_labels:
            if any(
                t["status"] in {"queued", "running"}
                and (
                    t["label"].startswith("runtime_")
                    or (label.startswith("runtime_") and t["label"] in gpu_labels)
                )
                for t in tasks.values()
            ):
                raise HTTPException(409, "运行环境或配音任务正在执行，请等待完成")
        active = [
            t
            for t in tasks.values()
            if project_name
            and t.get("project") == project_name
            and (t["status"] in {"queued", "running"})
        ]
        if active and (
            label != "retry" or any((t["label"] not in {"synth", "retry"} for t in active))
        ):
            raise HTTPException(409, "工程已有排队/执行中的任务")
        key = uuid.uuid4().hex
        item = {
            "id": key,
            "label": label,
            "project": project_name,
            "status": "queued",
            "created_at": time.time(),
        }
        if concurrency is not None:
            item["concurrency"] = concurrency
        if project_name and label in {"synth", "retry"}:
            p = project(project_name)
            requested = set(line_ids or []) | ({line_id} if line_id else set())
            script = p.script()
            if label == "retry" and (
                not requested or requested - {line.id for line in script.lines}
            ):
                raise ValueError("请指定存在的重试行")
            occupied = {
                line_id for t in active if t["label"] == "retry" for line_id in t["line_ids"]
            }
            item["line_ids"] = [
                line.id
                for group in speech_groups(script.lines, p.cast())
                if (label == "synth" or any((member.id in requested for member in group.lines)))
                and (
                    not (
                        label == "retry" and any((member.id in occupied for member in group.lines))
                    )
                )
                for line in group.lines
            ]
            if label == "retry" and (not item["line_ids"]):
                return next(
                    (
                        t
                        for t in active
                        if t["label"] == "retry" and requested.intersection(t["line_ids"])
                    )
                )
            item["requested_line_ids"] = sorted(requested)
            manifest = read_json(p.root / "manifest.json", {"lines": {}})
            item["line_attempts"] = {
                key: manifest.get("lines", {}).get(key, {}).get("attempts", 0)
                for key in item["line_ids"]
            }
            item["line_fingerprints"] = {
                key: manifest.get("lines", {}).get(key, {}).get("fingerprint")
                for key in item["line_ids"]
            }
        (tasks[key], events[key]) = (item, threading.Event())
        for old in [k for (k, t) in tasks.items() if k in streams and t.get("finished_at")]:
            if time.time() - tasks[old]["finished_at"] > 60:
                streams.pop(old, None)

        def persist():
            write_json(settings.paths.cache / f"task-{key}.json", item)

        persist()

        def execute():
            if events[key].is_set():
                item["status"] = "paused"
                persist()
                return
            item["status"] = "running"
            item["started_at"] = time.time()
            task_context.item = item
            stages = {
                "brief": "正在调用模型撰写草稿",
                "extend": "正在调用模型续写追问与回答",
                "generate": "正在调用模型生成台词，随后校验 JSON 格式",
                "merge": "正在合并音频、混音并生成字幕",
                "run": "正在检查工作流阶段",
                "render": "正在检查完整音频与视频素材",
            }
            item["progress"] = {"kind": "stage", "stage": stages.get(label, "正在执行")}
            if project_name and label in {"synth", "retry"}:
                item["progress"] = line_progress(item)
            persist()
            try:
                result = fn(events[key].is_set)
                final_status = (
                    "paused" if events[key].is_set() and label not in LLM_LABELS else "succeeded"
                )
                item.update(
                    result=f"已生成视频 · {result['duration']:.1f} 秒"
                    if label == "render" and isinstance(result, dict)
                    else f"已交付 {len(result['files'])} 个音频/工程文件"
                    if isinstance(result, dict) and "files" in result
                    else f"有效配音 {sum((v.get('status') == 'succeeded' for v in result['lines'].values()))} 行（含复用）"
                    if isinstance(result, dict) and "lines" in result
                    else f"生成 {len(result.lines)} 行剧本"
                    if isinstance(result, Script)
                    else f"草稿已{('扩展' if label == 'extend' else '生成')} · {len(result)} 字"
                    if label in {"brief", "extend"} and isinstance(result, str)
                    else {
                        "script_review": "等待剧本定稿",
                        "binding_review": "等待确认音色绑定",
                        "audio_review": "等待试听并确认完整音频",
                        "segments_review": "语音组已就绪，请整组试听；满意后手动合并完整音频",
                        "complete": "音频工作流已完成",
                    }.get(str(result), str(result)[:500])
                )
            except Exception as exc:
                final_status = "paused" if events[key].is_set() else "failed"
                if isinstance(exc, LLMCancelled) or (
                    final_status == "paused" and label in LLM_LABELS
                ):
                    item["error"] = "已停止生成，未保存任何内容"
                else:
                    item["error"] = str(exc)
                    task_log(item, f"失败（{type(exc).__name__}）：{exc}")
            finally:
                aborts.pop(key, None)
                if item.get("progress", {}).get("kind") == "lines" and project_name:
                    item["progress"] = line_progress(item)
                item["finished_at"] = time.time()
                item.setdefault("progress", {})["stage"] = (
                    item.get("error") or item.get("result") or "已停止"
                )
                item["status"] = final_status
                if key in streams:
                    streams[key]["version"] += 1
                persist()

        (runtime_executor if label.startswith("runtime_") else executor).submit(execute)
        return item

    @app.get("/api/config")
    def configuration():
        return {
            "moods": MOOD_LABELS,
            "script_prompts": global_prompts(settings.paths.root).model_dump(),
            "script_prompt_defaults": default_prompts(),
            "script_json_contract": structure_contract(settings.script.max_line_chars),
            "tts_mode": settings.tts.mode,
            "subtitle_colors": SUBTITLE_COLORS,
            "tts_url": settings.tts.base_url,
            "llm_configured": bool(settings.llm.endpoints()),
            "voice_source": "内置 Qwen 音色库"
            if settings.tts.mode == "managed"
            else "本机 Qwen PT 目录"
            if settings.tts.mode == "direct"
            else "离线测试音色库",
        }

    @app.get("/api/script-prompts")
    def get_script_prompts():
        return global_prompts(settings.paths.root).model_dump()

    @app.put("/api/script-prompts")
    def put_script_prompts(prompts: ScriptPrompts):
        return save_global_prompts(settings.paths.root, prompts).model_dump()

    @app.get("/api/connections")
    def connections():
        return connection_status(settings)

    @app.post("/api/tts/mode")
    def edit_tts_mode(body: TTSModeEdit):
        with submission_lock, lock(settings.paths.cache / "gpu.lock"):
            if any(t["status"] in {"queued", "running"} for t in tasks.values()):
                raise HTTPException(409, "请等待任务完成再切换模式")
            if runtime_manager.owned_process():
                raise ValueError("请先停止内置推理服务再切换模式")
            if body.mode == "direct":
                address = urlparse(body.local_url)
                if (
                    address.scheme not in {"http", "https"}
                    or address.hostname not in {"localhost", "127.0.0.1", "::1"}
                    or address.username
                    or address.password
                    or address.query
                    or address.fragment
                ):
                    raise ValueError("本机服务必须使用不含凭据或查询参数的 HTTP(S) 回环地址")
            updated = TTSSettings(
                mode=body.mode,
                base_url=body.local_url
                if body.mode == "direct"
                else f"http://127.0.0.1:{settings.runtime.port}",
                local_pt_root=settings.paths.voices if body.mode == "managed" else None,
            )
            values = updated.model_dump(mode="json")
            values["api_key"] = ""
            path = settings.paths.cache / "tts-mode.json"
            write_json(path, {"active": body.mode, body.mode: values})
            path.chmod(0o600)
            settings.tts = updated
        return configuration()

    @app.get("/api/runtime")
    def runtime():
        state = runtime_manager.status()
        return {
            **state,
            "connection": connection_status(settings),
            "metrics": None,
            "metrics_status": "本机运行环境",
            "tasks": task_list(),
        }

    @app.put("/api/runtime/config")
    def runtime_config(body: RuntimeSettings):
        with (
            submission_lock,
            lock(settings.paths.cache / "gpu.lock"),
            lock(runtime_manager.root / "lifecycle.lock"),
        ):
            if any(t["status"] in {"queued", "running"} for t in tasks.values()):
                raise HTTPException(409, "请等待当前任务完成再修改运行配置")
            result = runtime_manager.save_config(body.model_dump())
            if settings.tts.mode == "managed":
                VoiceStore(settings).import_examples(first_run=True)
            return result

    def runtime_submit(body, model_action=False):
        allowed = (
            {"download", "validate", "remove"}
            if model_action
            else {"install", "start", "stop", "restart", "test"}
        )
        action = body.get("action")
        if action not in allowed:
            raise ValueError("未知运行环境操作")
        key = body.get("key", settings.runtime.model)
        if model_action:
            from .inference.models import model_path

            model_path(settings, key)
        return submit(
            "runtime_" + action,
            lambda cancelled: runtime_manager.action(
                action,
                key=key,
                log=lambda msg: task_log(task_context.item, msg),
                cancelled=cancelled,
            ),
        )

    @app.post("/api/runtime/actions")
    def runtime_action(body: dict):
        return runtime_submit(body)

    @app.post("/api/runtime/models/actions")
    def runtime_model_action(body: dict):
        return runtime_submit(body, True)

    @app.get("/api/llm/config")
    def llm_configuration():
        return public_llm(settings.llm)

    @app.put("/api/llm/config")
    def llm_edit(body: dict):
        try:
            return save_llm(settings, body)
        except ValidationError as exc:
            raise ValueError(
                "；".join(
                    (
                        f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                        for e in exc.errors(include_input=False)
                    )
                )
            ) from None

    @app.post("/api/llm/test")
    def llm_test(body: dict):
        snapshot = settings.llm.model_copy(deep=True)
        if body.get("provider_id"):
            snapshot.active_provider = body["provider_id"]
            snapshot.failover = False

        def execute(cancelled):
            provider = OpenAICompatibleProvider(snapshot, llm_hooks(task_context.item, "test"))
            if body.get("structured"):
                script = ScriptEngine(provider).structure(
                    "连接测试", [], "P1 问工作台能否使用，P2 回答已经准备好。只生成两句简短对话。"
                )
                answer = f"结构化剧本通过（{len(script.lines)} 行）"
            else:
                answer = provider.complete("你是连接测试助手。", "仅回答：LLM 连接成功。")
            return f"{provider.last_endpoint}：{answer}"

        return submit("llm_test", execute)

    @app.get("/api/llm/models")
    def llm_models(provider_id: str):
        import httpx

        endpoint = next((p for p in settings.llm.providers if p.id == provider_id), None)
        if endpoint is None:
            raise ValueError("请先保存供应商配置")
        token = endpoint.api_keys[0].get_secret_value() if endpoint.api_keys else ""
        try:
            with httpx.Client(trust_env=False, timeout=10) as client:
                result = client.get(
                    endpoint.base_url.rstrip("/") + "/models",
                    headers={"Authorization": "Bearer " + token} if token else {},
                )
            if result.is_error:
                raise ValueError(f"模型发现返回 {result.status_code}，可手工填写模型名")
            return {
                "models": [
                    v["id"]
                    for v in result.json().get("data", [])
                    if isinstance(v, dict) and isinstance(v.get("id"), str)
                ]
            }
        except (httpx.HTTPError, KeyError, TypeError):
            raise ValueError("模型列表不可用，请确认地址或手工填写模型名") from None

    @app.get("/api/voices")
    def voices():
        assets = inventory_assets()
        roles = {}
        for item in assets:
            roles.setdefault(item["role"], []).append(item["emotion"])
        return roles

    def mutable_voice():
        if any(
            (
                t["status"] in {"queued", "running"}
                and t["label"] in {"sync", "create", "design", "preview", "synth", "retry", "run"}
                for t in tasks.values()
            )
        ):
            raise HTTPException(409, "配音或音色任务运行中，请完成或暂停后再编辑音色")

    @app.get("/api/voices/assets")
    def voice_assets():
        return inventory_assets()

    @app.get("/api/voices/status")
    def voice_status():
        inventory_assets()
        return {"editable": True, "source": "local", "message": "本机音色管理已连接"}

    @app.post("/api/voices/examples")
    def import_example_voices():
        with submission_lock, lock(settings.paths.cache / "gpu.lock"):
            mutable_voice()
            return voice_store().import_examples()

    @app.post("/api/voices/import")
    async def import_voice(
        file: UploadFile = File(...),
        role: str = Form(...),
        emotion: str = Form(...),
        overwrite: bool = Form(False),
        ref_text: str | None = Form(None),
        notes: str | None = Form(None),
    ):
        mutable_voice()
        if Path(file.filename or "").suffix.lower() != ".pt":
            raise ValueError("请选择 PT 文件")
        content = await file.read(32 * 1024 * 1024 + 1)
        with lock(settings.paths.cache / "gpu.lock"):
            store = voice_store()
            return store.import_pt(role, emotion, content, overwrite, ref_text, notes)

    @app.get("/api/voices/assets/{role}/{emotion}")
    def voice_detail(role: str, emotion: str):
        for asset in inventory_assets():
            if asset["role"] == role and asset["emotion"] == emotion:
                return asset
        raise ValueError("音色不存在")

    @app.get("/api/voices/assets/{role}/{emotion}/download")
    def download_voice(role: str, emotion: str):
        inventory_assets()
        store = voice_store()
        store.detail(role, emotion)
        path = store.path(role, emotion)
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.put("/api/voices/assets/{role}/{emotion}")
    def edit_voice(role: str, emotion: str, body: VoiceEdit):
        mutable_voice()
        with lock(settings.paths.cache / "gpu.lock"):
            store = voice_store()
            result = store.edit(role, emotion, body.role, body.emotion, body.ref_text, body.notes)
            with TTSClient(settings) as client:
                store.flush_deletions(client)
            return result

    @app.delete("/api/voices/assets/{role}/{emotion}")
    def delete_voice(role: str, emotion: str, detach: bool = False):
        mutable_voice()
        with lock(settings.paths.cache / "gpu.lock"):
            store = voice_store()
            result = store.delete(role, emotion, detach)
            with TTSClient(settings) as client:
                items = store.flush_deletions(client)
            pending = [i for i in items if i["status"] == "pending"]
            return {**result, "remote_status": "pending" if pending else "deleted"}

    @app.get("/api/voices/deletions")
    def voice_deletions():
        return VoiceStore(settings).deletions()

    @app.post("/api/voices/deletions/retry")
    def retry_deletions():
        return submit("sync", lambda cancelled: "本机音色库已更新")

    @app.get("/api/voices/assets/{role}/{emotion}/reference")
    def voice_reference(role: str, emotion: str):
        store = voice_store()
        store.detail(role, emotion)
        path = store.path(role, emotion).with_suffix(".wav")
        if not path.is_file():
            raise HTTPException(404, "此 PT 没有保存角色原声，请重新上传或录音")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/projects")
    def projects():
        return [
            {"name": path.parent.name, "topic": read_json(path)["meta"]["topic"]}
            for path in sorted(settings.paths.projects.glob("*/script.json"))
        ]

    @app.get("/api/home")
    def home():
        summaries = []
        for path in settings.paths.projects.glob("*/script.json"):
            p = Project(settings, path.parent.name)
            script = p.script()
            audio = read_json(p.root / "exports/audio.json", {})
            video = read_json(p.root / "exports/video.json", {})

            def has_export(metadata, filename):
                relative = metadata.get("files", {}).get(filename, {}).get("path")
                if not relative:
                    return False
                target = (p.root / "exports" / relative).resolve()
                return target.is_relative_to(p.root.resolve()) and target.is_file()

            def has_video(metadata):
                return any(
                    (
                        has_export(metadata, name)
                        for name in metadata.get("files", {})
                        if name.endswith(".mp4")
                    )
                )

            if not script.lines:
                (stage, tab) = (
                    "草稿待整理" if script.meta.brief.strip() else "从话题开始",
                    "script",
                )
            elif not p.approved("script"):
                (stage, tab) = ("台词待定稿", "script")
            elif not p.approved("binding"):
                (stage, tab) = ("待绑定音色", "binding")
            elif has_video(video):
                (stage, tab) = ("已有视频", "video")
            elif has_export(audio, "master.mp3"):
                (stage, tab) = ("已有完整音频", "production")
            else:
                (stage, tab) = ("可以开始配音", "production")
            updated = max(
                (
                    file.stat().st_mtime
                    for name in (
                        "script.json",
                        "cast.json",
                        "workflow.json",
                        "media.json",
                        "video.json",
                        "exports/audio.json",
                        "exports/video.json",
                    )
                    if (file := (p.root / name)).is_file()
                )
            )
            summaries.append(
                {
                    "name": p.name,
                    "topic": script.meta.topic,
                    "line_count": len(script.lines),
                    "stage": stage,
                    "tab": tab,
                    "updated_at": updated,
                }
            )
        return {"projects": sorted(summaries, key=lambda item: (-item["updated_at"], item["name"]))}

    @app.post("/api/projects", status_code=201)
    def new_project(body: NewProject):
        p = Project(settings, body.name)
        p.create(body.topic, body.keywords)
        return {"name": p.name}

    @app.get("/api/projects/{name}")
    def get_project(name: str):
        p = project(name)
        workflow = read_json(p.root / "workflow.json")
        checks = report(p, False)
        manifest = read_json(p.root / "manifest.json")
        groups = speech_groups(p.script().lines, p.cast())
        audio = {}
        for group in groups:
            if all((line.id in checks["reusable_ids"] for line in group.lines)):
                try:
                    content = group_audio(p, group, p.cast(), manifest)
                except (OSError, KeyError, ValueError, TTSError):
                    continue
                audio[group.id] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "duration": wav_info(content)[1],
                }
        return {
            "speech_groups": [
                {
                    "id": group.id,
                    "line_ids": [line.id for line in group.lines],
                    "slot": group.lines[0].slot,
                    "text": group.text,
                }
                for group in groups
            ],
            "group_audio": audio,
            "script": p.script().model_dump(),
            "cast": {k: v.model_dump() for (k, v) in p.cast().items()},
            "workflow": {
                **workflow,
                "approvals": {
                    gate: p.approved(gate) for gate in ("script", "binding", "audio", "video")
                },
            },
            "manifest": manifest,
            "preflight": checks,
            "synthesis": synthesis_options(p).model_dump(),
            "media": read_json(p.root / "media.json", {}),
            "audio_export": read_json(p.root / "exports/audio.json", {}),
            "audio_current": audio_current(p),
            "video": video_config(p),
            "video_export": read_json(p.root / "exports/video.json", {}),
            "video_current": video_current(p),
            "video_timeline": video_timeline(p),
            "cover_export": read_json(p.root / "exports/cover.json", {}),
            "cover_current": cover_current(p),
        }

    @app.get("/api/projects/{name}/groups/{line_id}/audio")
    @app.get("/api/projects/{name}/lines/{line_id}/audio")
    def line_audio(name: str, line_id: str, version: str | None = None):
        p = project(name)
        group = next(
            (
                group
                for group in speech_groups(p.script().lines, p.cast())
                if any((line.id == line_id for line in group.lines))
            ),
            None,
        )
        if group is None:
            raise HTTPException(404, "语音组不存在")
        try:
            content = group_audio(p, group, p.cast(), read_json(p.root / "manifest.json"))
        except (OSError, KeyError, ValueError, TTSError) as exc:
            raise HTTPException(409, "本组配音尚未生成或已失效，请重新生成本组") from exc
        digest = hashlib.sha256(content).hexdigest()
        if version is not None and version != digest:
            raise HTTPException(409, "配音版本已变化，请刷新后试听")
        return Response(
            content,
            media_type="audio/wav",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable" if version else "no-store",
                "ETag": f'"{digest}"',
                "X-Speech-Group": group.id,
            },
        )

    @app.put("/api/projects/{name}")
    def edit_project(name: str, body: Edit):
        p = project(name)
        if any(
            (
                t.get("project") == name and t["status"] in {"running", "queued"}
                for t in tasks.values()
            )
        ):
            raise HTTPException(409, "任务运行中，请先暂停再编辑")
        problems = validate_ids(body.script)
        if problems:
            raise ValueError("；".join(problems))
        with lock(p.root / ".project.lock"):
            p.save(body.script, body.cast)
        return {"saved": True, "preflight": report(p, False)}

    @app.post("/api/projects/{name}/preflight")
    def check(name: str):
        return report(project(name))

    @app.post("/api/projects/{name}/approve/{gate}")
    def approve(name: str, gate: str):
        p = project(name)
        mutable_project(name)
        with lock(p.root / ".project.lock"):
            if gate == "binding":
                if not p.approved("script"):
                    raise ValueError("当前剧本尚未定稿")
                checks = report(p)
                if not checks["ok"]:
                    raise ValueError("；".join(checks["errors"]))
            p.approve(gate)
        return {"approved": gate}

    @app.post("/api/projects/{name}/actions")
    def action(name: str, body: Action):
        p = project(name)
        concurrency = (
            body.concurrency if body.concurrency is not None else synthesis_options(p).concurrency
        )
        actions = {
            "brief": lambda cancel: generate_brief(p, llm_hooks(task_context.item, "brief")),
            "extend": lambda cancel: extend_brief(p, llm_hooks(task_context.item, "extend")),
            "generate": lambda cancel: generate_script(p, llm_hooks(task_context.item, "generate")),
            "synth": lambda cancel: synthesize(p, cancelled=cancel, concurrency=concurrency),
            "retry": lambda cancel: synthesize(
                p,
                retry_lines=task_context.item["line_ids"],
                cancelled=cancel,
                concurrency=concurrency,
            ),
            "merge": lambda cancel: build_audio(p, cancelled=cancel),
            "run": lambda cancel: advance(
                p,
                cancelled=cancel,
                progress=task_progress,
                llm=lambda kind: llm_hooks(task_context.item, kind),
                concurrency=concurrency,
                stop_before_merge=True,
            ),
            "render": lambda cancel: render_video(p, cancelled=cancel, progress=task_progress),
            "cover": lambda cancel: render_cover(p, cancelled=cancel, progress=task_progress),
        }
        if body.action not in actions:
            raise ValueError("未知操作")
        if body.action == "retry" and (not (body.line_id or body.line_ids)):
            raise ValueError("请指定重试行")
        if body.action == "extend" and (not p.script().meta.brief.strip()):
            raise ValueError("请先填写或生成草稿，再扩展对话")
        if body.action == "render" and (not audio_current(p)):
            raise ValueError("请先合并导出与当前工程一致的完整音频")
        return submit(
            body.action,
            actions[body.action],
            name,
            body.line_id,
            concurrency=concurrency if body.action in {"synth", "retry", "run"} else None,
            line_ids=body.line_ids,
        )

    @app.put("/api/projects/{name}/synthesis")
    def edit_synthesis(name: str, body: SynthesisOptions):
        p = project(name)
        mutable_project(name)
        with lock(p.root / ".project.lock"):
            write_json(p.root / "synthesis.json", body.model_dump())
        return body.model_dump()

    def mutable_project(name):
        if any(
            (
                t.get("project") == name and t["status"] in {"queued", "running"}
                for t in tasks.values()
            )
        ):
            raise HTTPException(409, "任务运行中，请完成或暂停后再修改素材")

    @app.put("/api/projects/{name}/video")
    def edit_video(name: str, body: VideoEdit):
        p = project(name)
        mutable_project(name)
        with lock(p.root / ".project.lock"):
            config = edit_config(video_config(p), body)
            write_json(p.root / "video.json", config)
        return config

    @app.post("/api/projects/{name}/cover-preview")
    def preview_cover(name: str, body: VideoEdit):
        import io

        p = project(name)
        with lock(p.root / ".project.lock"):
            config = edit_config(video_config(p), body)
            image = cover_picture(p, config)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/projects/{name}/assets/{kind}")
    async def upload_asset(name: str, kind: str, file: UploadFile = File(...)):
        p = project(name)
        mutable_project(name)
        suffix = Path(file.filename or "").suffix.lower()
        if kind == "bgm" and suffix not in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
            raise ValueError("请选择音频文件")
        if kind in {"overlay", "cover"} and suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("请选择 PNG、JPG 或 WebP 图片；透明贴图推荐 PNG")
        if kind == "video" and suffix not in {
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".m4v",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            raise ValueError("请选择视频或图片素材")
        if kind not in {"bgm", "video", "overlay", "cover"}:
            raise ValueError("未知素材类型")
        relative = f"assets/{uuid.uuid4().hex}{suffix}"
        target = p.root / relative
        target.parent.mkdir(exist_ok=True)
        try:
            size = 0
            with target.open("wb") as f:
                while chunk := (await file.read(1024 * 1024)):
                    size += len(chunk)
                    limit = 32 if kind in {"overlay", "cover"} else 500 if kind == "bgm" else None
                    if limit is not None and size > limit * 1024 * 1024:
                        raise ValueError(f"单个素材上限 {limit} MB")
                    f.write(chunk)
            details = (
                await run_in_threadpool(validate_asset, target, suffix)
                if kind in {"video", "overlay", "cover"}
                else {}
            )
            mutable_project(name)
            with lock(p.root / ".project.lock"):
                if kind in {"video", "overlay", "cover"}:
                    config = video_config(p)
                    if kind == "video" and len(config["assets"]) >= 100:
                        raise ValueError("每个工程最多 100 个视频素材")
                    asset = {
                        "id": target.stem,
                        "path": relative,
                        "name": Path(file.filename).name,
                        "bytes": size,
                        **details,
                    }
                    if kind == "overlay":
                        if len(config["overlays"]) >= 12:
                            raise ValueError("每个工程最多 12 张贴图")
                        placement = OverlayEdit(
                            id=asset["id"], x=0.05 if len(config["overlays"]) % 2 == 0 else 0.55
                        )
                        asset["source_width"] = asset.pop("width")
                        asset["source_height"] = asset.pop("height")
                        config["overlays"].append({**asset, **placement.model_dump()})
                    elif kind == "cover":
                        config["cover"]["image"] = asset
                        config["cover"]["options"].update(background="image", enabled=True)
                    else:
                        config["assets"].append(asset)
                        config["clips"].append(default_clip(asset))
                    write_json(p.root / "video.json", config)
                    return config
                media = read_json(p.root / "media.json", {})
                media["bgm"] = relative
                write_json(p.root / "media.json", media)
                return media
        except Exception:
            target.unlink(missing_ok=True)
            raise

    @app.post("/api/projects/{name}/video-preview/{asset_id}")
    def prepare_video_preview(name: str, asset_id: str):
        p = project(name)
        asset = next((a for a in video_config(p)["assets"] if a["id"] == asset_id), None)
        if not asset or asset["kind"] != "video":
            raise HTTPException(404, "视频素材不存在")
        source = asset_path(p, asset)
        relative = f"assets/previews/{asset_id}.mp4"
        target = p.root / relative
        if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return {"path": relative}
        mutable_project(name)

        def convert(cancelled):
            target.parent.mkdir(exist_ok=True)
            temporary = target.with_name(f"{asset_id}-{uuid.uuid4().hex}.mp4")
            try:
                task_progress(stage="正在生成可拖动播放的兼容预览，原视频保持不变", kind="stage")
                ffmpeg(
                    [
                        "-i",
                        source,
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-vf",
                        "scale=trunc(min(960\\,iw)/2)*2:-2,fps=24",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "27",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "96k",
                        "-movflags",
                        "+faststart",
                        temporary,
                    ]
                )
                if cancelled():
                    raise RuntimeError("已取消兼容预览")
                temporary.replace(target)
                return "兼容预览已就绪"
            finally:
                temporary.unlink(missing_ok=True)

        return submit("preview_video", convert, name)

    @app.put("/api/projects/{name}/mix")
    def edit_mix(name: str, body: MixEdit):
        p = project(name)
        mutable_project(name)
        with lock(p.root / ".project.lock"):
            media = read_json(p.root / "media.json", {})
            media["bgm_volume"] = body.bgm_volume
            if body.remove_bgm:
                media.pop("bgm", None)
            write_json(p.root / "media.json", media)
        return media

    @app.post("/api/voices/reference")
    async def upload_reference(file: UploadFile = File(...)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {
            ".wav",
            ".mp3",
            ".m4a",
            ".flac",
            ".ogg",
            ".aac",
            ".webm",
            ".mp4",
            ".mov",
            ".mkv",
            ".avi",
            ".m4v",
            ".mpg",
            ".mpeg",
            ".ts",
            ".mts",
            ".m2ts",
            ".opus",
            ".aiff",
            ".aif",
            ".wma",
            ".amr",
            ".3gp",
            ".ogv",
            ".mka",
        }:
            raise ValueError("不支持的角色原声格式，请上传音频或视频文件")
        content = await file.read(50 * 1024 * 1024 + 1)
        if len(content) > 50 * 1024 * 1024:
            raise ValueError("角色原声上限 50 MB")
        if not content:
            raise ValueError("角色原声为空")
        stem = uuid.uuid4().hex
        (source, target) = (
            settings.paths.cache / (stem + ".upload" + suffix),
            settings.paths.cache / (stem + ".wav"),
        )
        atomic_bytes(source, content)
        try:
            await run_in_threadpool(
                ffmpeg,
                [
                    "-i",
                    source,
                    "-t",
                    "121",
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-c:a",
                    "pcm_s16le",
                    target,
                ],
                timeout=60,
            )
            (_, duration) = wav_info(target.read_bytes())
            if duration > 120:
                raise ValueError("角色原声请控制在 120 秒以内")
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            source.unlink(missing_ok=True)
        return {
            "reference_id": target.name,
            "duration": round(duration, 2),
            "preview_url": f"/api/previews/{target.name}",
        }

    @app.post("/api/voices/actions")
    def voice_action(body: VoiceAction):

        def execute(cancelled):
            with lock(settings.paths.cache / "gpu.lock"), TTSClient(settings) as client:
                if body.action == "sync":
                    inventory_assets()
                    return "已刷新本机 Qwen 实际 PT 目录"
                if body.action == "design":
                    result = client.synthesize_design(text=body.text, instruct=body.instruct)
                    entry = voice_previews.save(
                        result.wav_bytes,
                        kind="design",
                        role=body.role,
                        emotion=body.emotion,
                        text=body.text,
                        instruct=body.instruct,
                    )
                    return entry["id"] + ".wav"
                if body.action == "preview":
                    if not body.text.strip():
                        raise ValueError("请填写试听台词")
                    store = VoiceStore(settings)
                    store.detail(body.role, body.emotion)
                    store.verify_remote(client, {(body.role, body.emotion)})
                    result = client.synthesize(text=body.text, role=body.role, emotion=body.emotion)
                    from .storage import sha256

                    entry = voice_previews.save(
                        result.wav_bytes,
                        kind="preview",
                        role=body.role,
                        emotion=body.emotion,
                        text=body.text,
                        voice_sha256=sha256(store.path(body.role, body.emotion)),
                    )
                    return entry["id"] + ".wav"
                if body.action == "create":
                    ref = Path(body.reference_id)
                    if (
                        ref.suffix != ".wav"
                        or len(ref.stem) != 32
                        or any((c not in "0123456789abcdef" for c in ref.stem))
                        or (not (settings.paths.cache / ref).is_file())
                    ):
                        raise ValueError("角色原声不存在")
                    preview = read_json(settings.paths.cache / (ref.name + ".json"))
                    if preview and "text" in preview and (preview["text"] != body.text):
                        raise ValueError("对应台词必须与试音台词一致")
                    result = voice_store().create(
                        client,
                        settings.paths.cache / ref,
                        body.role,
                        body.emotion,
                        body.text,
                        body.overwrite,
                    )
                    voice_previews.save(
                        (settings.paths.cache / ref).read_bytes(),
                        kind="original",
                        role=body.role,
                        emotion=body.emotion,
                        text=body.text,
                    )
                    return result
                raise ValueError("未知音色操作")

        return submit(body.action, execute)

    @app.post("/api/voices/reference/crop")
    def crop_reference(body: ReferenceCrop):
        source = settings.paths.cache / body.reference_id
        if not source.is_file():
            raise ValueError("角色原声不存在")
        key = uuid.uuid4().hex + ".wav"
        target = settings.paths.cache / key
        with wave.open(str(source), "rb") as reader:
            (rate, total) = (reader.getframerate(), reader.getnframes())
            (first, last) = (round(body.start * rate), round(body.end * rate))
            if last > total or last - first < round(rate * 0.1):
                raise ValueError("裁剪范围必须在音频内，且至少保留 0.1 秒")
            reader.setpos(first)
            with wave.open(str(target), "wb") as writer:
                writer.setparams(reader.getparams())
                writer.writeframes(reader.readframes(last - first))
        write_json(
            settings.paths.cache / (key + ".json"),
            {"source_id": body.reference_id, "start": first / rate, "end": last / rate},
        )
        return {
            "reference_id": key,
            "duration": (last - first) / rate,
            "preview_url": f"/api/previews/{key}",
        }

    @app.get("/api/tasks")
    def task_list():
        result = []
        recent = list(tasks.values())
        for task in [
            t
            for (i, t) in enumerate(recent)
            if i >= len(recent) - 30 or t["status"] in {"queued", "running"}
        ]:
            item = dict(task)
            item["streaming"] = task["id"] in aborts
            if "started_at" in task:
                item["elapsed_seconds"] = max(
                    0, (task.get("finished_at") or time.time()) - task["started_at"]
                )
            if task.get("project"):
                p = Project(settings, task["project"])
                if not (p.root / "script.json").is_file():
                    item["project_missing"] = True
                    result.append(item)
                    continue
                if task["status"] == "running" and task.get("progress", {}).get("kind") == "lines":
                    item["progress"] = line_progress(task)
            result.append(item)
        return result

    @app.post("/api/tasks/{key}/pause")
    def pause(key: str):
        if key not in events:
            raise HTTPException(404, "当前服务没有该任务")
        events[key].set()
        abort = aborts.get(key)
        if abort:
            abort()
        return {"pause_requested": True}

    @app.get("/api/tasks/{key}/stream")
    async def task_stream(key: str, request: Request):
        """Server-sent events with the model output of a running LLM task."""
        if key not in tasks:
            raise HTTPException(404, "当前服务没有该任务")

        async def events_source():
            (phase, sent, version) = (None, 0, -1)
            yield "retry: 1500\n\n"
            while True:
                (task, live) = (tasks[key], streams.get(key))
                if live and live["version"] != version:
                    version = live["version"]
                    if live["phase"] != phase:
                        (phase, sent) = (live["phase"], 0)
                    body = {
                        "kind": live["kind"],
                        "phase": phase,
                        "from": sent,
                        "chars": len(live["text"]),
                        "reasoning": live["reasoning"],
                    }
                    if live["kind"] == "generate":
                        body["lines"] = live["lines"][sent:]
                        sent = len(live["lines"])
                    else:
                        body["text"] = live["text"][sent:]
                        sent = len(live["text"])
                    yield ("data: " + json.dumps(body, ensure_ascii=False) + "\n\n")
                if task["status"] not in {"queued", "running"}:
                    yield (
                        "event: end\ndata: "
                        + json.dumps(
                            {"status": task["status"], "error": task.get("error")},
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.15)

        return StreamingResponse(
            events_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tasks/{key}/llm")
    def task_llm_output(key: str):
        """Full raw model output and diagnostics of the latest model call in a task."""
        runs = sorted(llm_runs.glob(f"{key}-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        if not runs:
            raise HTTPException(404, "该任务没有模型输出记录")
        return read_json(runs[-1])

    @app.get("/api/voices/previews")
    def voice_preview_history():
        return voice_previews.entries()

    @app.get("/api/voices/previews/{key}/audio")
    def voice_preview_audio(key: str):
        path = voice_previews.path(key)
        if not path.is_file():
            raise HTTPException(404, "试听记录不存在")
        return FileResponse(
            path, media_type="audio/wav", filename=f"{key}.wav", content_disposition_type="inline"
        )

    @app.get("/api/previews/{filename}")
    def preview(filename: str):
        if not filename.endswith(".wav") or Path(filename).name != filename:
            raise HTTPException(404)
        p = settings.paths.cache / filename
        if not p.is_file() and len(Path(filename).stem) == 32:
            p = voice_previews.path(Path(filename).stem)
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="audio/wav")

    @app.get("/api/projects/{name}/files/{relative:path}")
    def project_file(name: str, relative: str):
        p = project(name)
        target = (p.root / relative).resolve()
        if not target.is_relative_to(p.root.resolve()) or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target)

    app.mount(
        "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="editor"
    )
    return app
