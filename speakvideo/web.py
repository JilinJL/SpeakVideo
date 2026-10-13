from __future__ import annotations

import base64
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audio import audio_current, build_audio
from .config import Settings, TTSSettings
from .connections import connection_status
from .delivery import build_delivery, delivery_current
from .jobs import report, synthesize
from .llm_config import public_llm, save_llm
from .processing import ffmpeg
from .project import Project
from .script.engine import OpenAICompatibleProvider
from .script.moods import MOOD_LABELS
from .script.prompts import default_prompts, structure_contract
from .script.schema import Script, VoiceBinding, validate_ids
from .storage import atomic_bytes, lock, read_json, write_json
from .tts.client import TTSClient, TTSError, wav_info
from .tts.grouping import speech_groups
from .video import (
    SUBTITLE_COLORS,
    VideoEdit,
    render_video,
    validate_asset,
    video_config,
    video_current,
)
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


class VoiceEdit(BaseModel):
    role: str
    emotion: str
    ref_text: str = ""
    notes: str = ""


class ReferenceCrop(BaseModel):
    reference_id: str = Field(pattern=r"^[a-f0-9]{32}\.wav$")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)


class TTSModeEdit(BaseModel):
    mode: str = Field(pattern=r"^(gateway|direct)$")
    local_url: str = "http://127.0.0.1:8001"


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_dirs()
    app = FastAPI(title="SpeakVideo", version="0.1.0")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    tasks: dict[str, dict] = {}
    events: dict[str, threading.Event] = {}
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speakvideo")
    remote_inventory_cache = {"at": 0.0, "assets": [], "status": {}}
    capabilities_cache = {"at": 0.0, "value": {}}
    task_context = threading.local()

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
        valid_ids = set(report(p, False)["reusable_ids"])
        return {
            "kind": "lines",
            "stage": "正在按连续角色分组合成配音",
            "completed": sum(line_id in valid_ids for line_id in ids),
            "total": len(ids),
            "failed": sum(r.get("status") == "failed" for r in records),
            "current_line": next(
                (key for key, record in zip(ids, records) if record.get("status") == "running"),
                None,
            ),
        }

    def voice_store():
        return VoiceStore(settings)

    def inventory_assets(force=False):
        store = voice_store()
        if settings.tts.mode != "gateway":
            return [{**v, "source": "local", "editable": True} for v in store.details()]
        if not force and time.monotonic() - remote_inventory_cache["at"] < 3:
            return remote_inventory_cache["assets"]
        with TTSClient(settings) as client:
            assets = store.remote_inventory(client)
        remote_inventory_cache.update(
            at=time.monotonic(), assets=assets, status=store.inventory_status
        )
        return assets

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

    def submit(label: str, fn, project_name: str | None = None, line_id: str | None = None):
        if project_name and any(
            t.get("project") == project_name and t["status"] in {"queued", "running"}
            for t in tasks.values()
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
        if project_name and label in {"synth", "retry"}:
            p = project(project_name)
            item["line_ids"] = [
                line.id
                for group in speech_groups(p.script().lines, p.cast())
                if not line_id or any(member.id == line_id for member in group.lines)
                for line in group.lines
            ]
        tasks[key], events[key] = item, threading.Event()

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
                item.update(
                    status="paused" if events[key].is_set() else "succeeded",
                    result=(
                        f"已生成视频 · {result['duration']:.1f} 秒"
                        if label == "render" and isinstance(result, dict)
                        else "已打包成片、音频、字幕与工程快照"
                        if label == "package"
                        else f"已交付 {len(result['files'])} 个音频/工程文件"
                        if isinstance(result, dict) and "files" in result
                        else f"有效配音 {sum(v.get('status') == 'succeeded' for v in result['lines'].values())} 行（含复用）"
                        if isinstance(result, dict) and "lines" in result
                        else f"生成 {len(result.lines)} 行剧本"
                        if isinstance(result, Script)
                        else f"草稿已{'扩展' if label == 'extend' else '生成'} · {len(result)} 字"
                        if label in {"brief", "extend"} and isinstance(result, str)
                        else {
                            "script_review": "等待剧本定稿",
                            "binding_review": "等待确认音色绑定",
                            "audio_review": "等待试听并确认完整音频",
                            "complete": "音频工作流已完成",
                        }.get(str(result), str(result)[:500])
                    ),
                )
            except Exception as exc:
                item.update(status="paused" if events[key].is_set() else "failed", error=str(exc))
            finally:
                if item.get("progress", {}).get("kind") == "lines" and project_name:
                    item["progress"] = line_progress(item)
                item["finished_at"] = time.time()
                item.setdefault("progress", {})["stage"] = (
                    item.get("error") or item.get("result") or "已停止"
                )
                persist()

        executor.submit(execute)
        return item

    @app.get("/api/config")
    def configuration():
        return {
            "moods": MOOD_LABELS,
            "script_prompts": default_prompts(),
            "script_json_contract": structure_contract(settings.script.max_line_chars),
            "tts_mode": settings.tts.mode,
            "subtitle_colors": SUBTITLE_COLORS,
            "tts_url": settings.tts.base_url,
            "llm_configured": bool(settings.llm.endpoints()),
            "voice_source": "Windows 实时音色库"
            if settings.tts.mode == "gateway"
            else "本机 Qwen PT 目录"
            if settings.tts.mode == "direct"
            else "离线测试音色库",
        }

    @app.get("/api/connections")
    def connections():
        return connection_status(settings)

    @app.get("/api/worker/capabilities")
    def worker_capabilities(refresh: bool = False):
        if not refresh and time.monotonic() - capabilities_cache["at"] < 30:
            return capabilities_cache["value"]
        bounded = settings.model_copy(deep=True)
        bounded.tts.timeout_seconds = 12
        with TTSClient(bounded) as client:
            value = client.capabilities()
        capabilities_cache.update(at=time.monotonic(), value=value)
        return value

    @app.post("/api/tts/mode")
    def edit_tts_mode(body: TTSModeEdit):
        if any(t["status"] in {"queued", "running"} for t in tasks.values()):
            raise HTTPException(409, "请等待当前任务完成再切换算力模式")
        path = settings.paths.cache / "tts-mode.json"
        modes = read_json(path, {})
        private = settings.tts.model_dump(mode="json")
        private["api_key"] = settings.tts.api_key.get_secret_value()
        modes[settings.tts.mode] = private
        if body.mode == "direct":
            if urlparse(body.local_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("本地模式地址必须是 localhost / 127.0.0.1")
            values = {
                **private,
                "mode": "direct",
                "base_url": body.local_url,
                "api_key": "",
                "local_pt_root": None,
            }
        else:
            values = modes.get("gateway")
            if not values:
                raise ValueError("尚未配置 Windows Gateway，请先设置 config.local.toml")
        updated = TTSSettings.model_validate(values)
        modes[body.mode], modes["active"] = values, body.mode
        write_json(path, modes)
        path.chmod(0o600)
        settings.tts = updated
        remote_inventory_cache["at"] = 0
        capabilities_cache["at"] = 0
        return configuration()

    @app.get("/api/runtime")
    def runtime():
        status = connection_status(settings)
        metrics = None
        metrics_status = "本机 Qwen 模式"
        if settings.tts.mode == "gateway":
            metrics_status = "Windows 未连接"
        if settings.tts.mode == "gateway" and status.get("worker") == "connected":
            with TTSClient(settings) as client:
                try:
                    metrics = client._request("GET", "/v1/worker/metrics", timeout=8).json()
                    metrics_status = "available"
                except TTSError as exc:
                    metrics_status = (
                        "Gateway 缺少资源监控路由，请升级网关"
                        if exc.status_code == 404
                        else str(exc)
                    )
        return {
            "connection": status,
            "metrics": metrics,
            "metrics_status": metrics_status,
            "tasks": task_list(),
        }

    @app.get("/api/llm/config")
    def llm_configuration():
        return public_llm(settings.llm)

    @app.put("/api/llm/config")
    def llm_edit(body: dict):
        try:
            return save_llm(settings, body)
        except ValidationError as exc:
            # Do not echo credentials from Pydantic's input_value diagnostics.
            raise ValueError(
                "；".join(
                    f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                    for e in exc.errors(include_input=False)
                )
            ) from None

    @app.post("/api/llm/test")
    def llm_test(body: dict):
        snapshot = settings.llm.model_copy(deep=True)
        if body.get("provider_id"):
            snapshot.active_provider = body["provider_id"]
            snapshot.failover = False

        def execute(cancelled):
            provider = OpenAICompatibleProvider(snapshot)
            answer = provider.complete("你是连接测试助手。", "仅回答：LLM 连接成功。")
            return f"{provider.last_endpoint}：{answer}"

        return submit("llm_test", execute)

    @app.get("/api/voices")
    def voices():
        assets = inventory_assets()
        roles = {}
        for item in assets:
            roles.setdefault(item["role"], []).append(item["emotion"])
        return roles

    def mutable_voice():
        if any(
            t["status"] in {"queued", "running"}
            and t["label"] in {"sync", "create", "design", "preview", "synth", "retry", "run"}
            for t in tasks.values()
        ):
            raise HTTPException(409, "配音或音色任务运行中，请完成或暂停后再编辑音色")

    @app.get("/api/voices/assets")
    def voice_assets():
        return inventory_assets()

    @app.get("/api/voices/status")
    def voice_status():
        inventory_assets()
        if settings.tts.mode == "gateway":
            return remote_inventory_cache["status"]
        return {"editable": True, "source": "local", "message": "本机音色管理已连接"}

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
            if settings.tts.mode == "gateway":
                store.validate_pt(content)
                with TTSClient(settings) as client:
                    result = client.manage_voices(
                        "import",
                        role=role,
                        emotion=emotion,
                        content_base64=base64.b64encode(content).decode(),
                        overwrite=overwrite,
                        ref_text=ref_text or "",
                        notes=notes or "",
                    )
                inventory_assets(True)
                return {**result, **voice_detail(result["role"], result["emotion"])}
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
            if settings.tts.mode == "gateway":
                asset = voice_detail(role, emotion)
                if (role, emotion) != (body.role, body.emotion) and asset["usage"]:
                    raise RuntimeError("该 PT 被工程使用，请先解除绑定再重命名")
                with TTSClient(settings) as client:
                    result = client.manage_voices(
                        "edit",
                        role=role,
                        emotion=emotion,
                        new_role=body.role,
                        new_emotion=body.emotion,
                        ref_text=body.ref_text,
                        notes=body.notes,
                        sha256=asset["sha256"],
                    )
                inventory_assets(True)
                return {**result, **voice_detail(result["role"], result["emotion"])}
            result = store.edit(role, emotion, body.role, body.emotion, body.ref_text, body.notes)
            with TTSClient(settings) as client:
                store.flush_deletions(client)
            return result

    @app.delete("/api/voices/assets/{role}/{emotion}")
    def delete_voice(role: str, emotion: str, detach: bool = False):
        mutable_voice()
        with lock(settings.paths.cache / "gpu.lock"):
            store = voice_store()
            if settings.tts.mode == "gateway":
                asset = voice_detail(role, emotion)
                if asset["usage"] and not detach:
                    raise RuntimeError("请勾选解绑后再删除")
                try:
                    with TTSClient(settings) as client:
                        result = client.manage_voices(
                            "delete_permanent", role=role, emotion=emotion, sha256=asset["sha256"]
                        )
                except TTSError as exc:
                    if exc.status_code == 404 or "unknown voice action" in str(exc):
                        raise RuntimeError(
                            "Windows Agent 暂不支持永久删除。请同步最新 worker/ 文件并重启 Agent 后重试。"
                        ) from exc
                    raise
                if result.get("deleted") is not True or result.get("permanent") is not True:
                    raise RuntimeError("Windows 未确认永久删除，请更新 Windows Agent 后重试")
                local = store.delete(role, emotion, detach, remote_deleted=True)
                inventory_assets(True)
                return {**local, **result}
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
        def execute(cancelled):
            with lock(settings.paths.cache / "gpu.lock"), TTSClient(settings) as client:
                result = VoiceStore(settings).flush_deletions(client)
                if any(i["status"] == "pending" for i in result):
                    raise RuntimeError("Windows 删除尚未完成，请检查连接或文件哈希冲突")
                return "Windows 删除已同步"

        return submit("sync", execute)

    @app.get("/api/voices/assets/{role}/{emotion}/reference")
    def voice_reference(role: str, emotion: str):
        if settings.tts.mode == "gateway":
            asset = voice_detail(role, emotion)
            with TTSClient(settings) as client:
                result = client.manage_voices(
                    "reference", role=role, emotion=emotion, sha256=asset["sha256"]
                )
            key = uuid.uuid4().hex + ".wav"
            atomic_bytes(
                settings.paths.cache / key,
                base64.b64decode(result["content_base64"], validate=True),
            )
            return FileResponse(settings.paths.cache / key, media_type="audio/wav")
        store = voice_store()
        store.detail(role, emotion)
        path = store.path(role, emotion).with_suffix(".wav")
        if not path.is_file():
            raise HTTPException(404, "此 PT 没有保存参考音频，请重新上传或录音")
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

            if not script.lines:
                stage, tab = ("草稿待整理" if script.meta.brief.strip() else "从话题开始"), "script"
            elif not p.approved("script"):
                stage, tab = "台词待定稿", "script"
            elif not p.approved("binding"):
                stage, tab = "待绑定音色", "binding"
            elif has_export(video, "final.mp4"):
                stage, tab = "已有视频", "video"
            elif has_export(audio, "master.mp3"):
                stage, tab = "已有完整音频", "production"
            else:
                stage, tab = "可以开始配音", "production"
            updated = max(
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
                if (file := p.root / name).is_file()
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
        return {
            "script": p.script().model_dump(),
            "cast": {k: v.model_dump() for k, v in p.cast().items()},
            "workflow": {
                **workflow,
                "approvals": {
                    gate: p.approved(gate) for gate in ("script", "binding", "audio", "video")
                },
            },
            "manifest": read_json(p.root / "manifest.json"),
            "preflight": report(p, False),
            "media": read_json(p.root / "media.json", {}),
            "audio_export": read_json(p.root / "exports/audio.json", {}),
            "audio_current": audio_current(p),
            "video": video_config(p),
            "video_export": read_json(p.root / "exports/video.json", {}),
            "video_current": video_current(p),
            "delivery_export": read_json(p.root / "exports/delivery.json", {}),
            "delivery_current": delivery_current(p),
        }

    @app.put("/api/projects/{name}")
    def edit_project(name: str, body: Edit):
        p = project(name)
        if any(
            t.get("project") == name and t["status"] in {"running", "queued"}
            for t in tasks.values()
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
        actions = {
            "brief": lambda cancel: generate_brief(p),
            "extend": lambda cancel: extend_brief(p),
            "generate": lambda cancel: generate_script(p),
            "synth": lambda cancel: synthesize(p, cancelled=cancel),
            "retry": lambda cancel: synthesize(p, retry_line=body.line_id, cancelled=cancel),
            "merge": lambda cancel: build_audio(p, cancelled=cancel),
            "run": lambda cancel: advance(p, cancelled=cancel, progress=task_progress),
            "render": lambda cancel: render_video(p, cancelled=cancel, progress=task_progress),
            "package": lambda cancel: build_delivery(p, cancelled=cancel, progress=task_progress),
        }
        if body.action not in actions:
            raise ValueError("未知操作")
        if body.action == "retry" and not body.line_id:
            raise ValueError("请指定重试行")
        if body.action == "extend" and not p.script().meta.brief.strip():
            raise ValueError("请先填写或生成草稿，再扩展对话")
        if body.action == "render" and not audio_current(p):
            raise ValueError("请先合并导出与当前工程一致的完整音频")
        return submit(body.action, actions[body.action], name, body.line_id)

    def mutable_project(name):
        if any(
            t.get("project") == name and t["status"] in {"queued", "running"}
            for t in tasks.values()
        ):
            raise HTTPException(409, "任务运行中，请完成或暂停后再修改素材")

    @app.put("/api/projects/{name}/video")
    def edit_video(name: str, body: VideoEdit):
        p = project(name)
        mutable_project(name)
        with lock(p.root / ".project.lock"):
            config = video_config(p)
            assets = {asset["id"]: asset for asset in config["assets"]}
            if len(body.asset_ids) != len(set(body.asset_ids)) or any(
                key not in assets for key in body.asset_ids
            ):
                raise ValueError("素材顺序包含重复或不存在的素材")
            config["assets"] = [assets[key] for key in body.asset_ids]
            config["options"] = body.model_dump(exclude={"asset_ids"})
            write_json(p.root / "video.json", config)
        return config

    @app.post("/api/projects/{name}/assets/{kind}")
    async def upload_asset(name: str, kind: str, file: UploadFile = File(...)):
        p = project(name)
        mutable_project(name)
        suffix = Path(file.filename or "").suffix.lower()
        if kind == "bgm" and suffix not in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
            raise ValueError("请选择音频文件")
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
        if kind not in {"bgm", "video"}:
            raise ValueError("未知素材类型")
        relative = f"assets/{uuid.uuid4().hex}{suffix}"
        target = p.root / relative
        target.parent.mkdir(exist_ok=True)
        try:
            size = 0
            with target.open("wb") as f:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 500 * 1024 * 1024:
                        raise ValueError("单个素材上限 500 MB")
                    f.write(chunk)
            details = (
                await run_in_threadpool(validate_asset, target, suffix) if kind == "video" else {}
            )
            mutable_project(name)
            with lock(p.root / ".project.lock"):
                if kind == "video":
                    config = video_config(p)
                    if len(config["assets"]) >= 100:
                        raise ValueError("每个工程最多 100 个视频素材")
                    config["assets"].append(
                        {
                            "id": target.stem,
                            "path": relative,
                            "name": Path(file.filename).name,
                            "bytes": size,
                            **details,
                        }
                    )
                    write_json(p.root / "video.json", config)
                    return config
                media = read_json(p.root / "media.json", {})
                media["bgm"] = relative
                write_json(p.root / "media.json", media)
                return media
        except Exception:
            target.unlink(missing_ok=True)
            raise

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
        if suffix not in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"}:
            raise ValueError("不支持的参考音频")
        content = await file.read(50 * 1024 * 1024 + 1)
        if len(content) > 50 * 1024 * 1024:
            raise ValueError("参考音频上限 50 MB")
        if not content:
            raise ValueError("参考音频为空")
        stem = uuid.uuid4().hex
        source, target = (
            settings.paths.cache / (stem + ".upload" + suffix),
            settings.paths.cache / (stem + ".wav"),
        )
        atomic_bytes(source, content)
        try:
            # Uploaded audio and PCM recordings share one normalized Qwen input format.
            await run_in_threadpool(
                ffmpeg,
                [
                    "-i",
                    source,
                    "-t",
                    "121",
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
            _, duration = wav_info(target.read_bytes())
            if duration > 120:
                raise ValueError("参考音频请控制在 120 秒以内")
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
                    if client.mode == "gateway":
                        inventory_assets(True)
                        return "已刷新 Windows 真实 PT 库；不向远端覆盖 Mac 音色"
                    inventory_assets()
                    return "已刷新本机 Qwen 实际 PT 目录"
                if body.action == "design":
                    result = client.synthesize_design(text=body.text, instruct=body.instruct)
                    key = uuid.uuid4().hex + ".wav"
                    atomic_bytes(settings.paths.cache / key, result.wav_bytes)
                    write_json(
                        settings.paths.cache / (key + ".json"),
                        {"text": body.text, "instruct": body.instruct},
                    )
                    return key
                if body.action == "preview":
                    if not body.text.strip():
                        raise ValueError("请填写试听台词")
                    store = VoiceStore(settings)
                    store.detail(body.role, body.emotion)
                    store.verify_remote(client, {(body.role, body.emotion)})
                    if client.mode == "gateway":
                        from .storage import sha256

                        job = client.submit(
                            {
                                "text": body.text,
                                "role": body.role,
                                "emotion": body.emotion,
                                "language": "Chinese",
                                "voice_sha256": sha256(store.path(body.role, body.emotion)),
                            },
                            "preview-" + uuid.uuid4().hex,
                        )
                        result = client.wait_job(job, cancelled)
                    else:
                        result = client.synthesize(
                            text=body.text, role=body.role, emotion=body.emotion
                        )
                    key = uuid.uuid4().hex + ".wav"
                    atomic_bytes(settings.paths.cache / key, result.wav_bytes)
                    return key
                if body.action == "create":
                    # Only use a previously uploaded/generated reference, never arbitrary local paths.
                    ref = Path(body.reference_id)
                    if (
                        ref.suffix != ".wav"
                        or len(ref.stem) != 32
                        or any(c not in "0123456789abcdef" for c in ref.stem)
                        or not (settings.paths.cache / ref).is_file()
                    ):
                        raise ValueError("参考音频不存在")
                    preview = read_json(settings.paths.cache / (ref.name + ".json"))
                    if preview and "text" in preview and preview["text"] != body.text:
                        raise ValueError("固化台词必须与试音台词一致")
                    result = voice_store().create(
                        client,
                        settings.paths.cache / ref,
                        body.role,
                        body.emotion,
                        body.text,
                        body.overwrite,
                    )
                    if settings.tts.mode == "gateway":
                        inventory_assets(True)
                    return result
                raise ValueError("未知音色操作")

        return submit(body.action, execute)

    @app.post("/api/voices/reference/crop")
    def crop_reference(body: ReferenceCrop):
        source = settings.paths.cache / body.reference_id
        if not source.is_file():
            raise ValueError("参考音频不存在")
        key = uuid.uuid4().hex + ".wav"
        target = settings.paths.cache / key
        with wave.open(str(source), "rb") as reader:
            rate, total = reader.getframerate(), reader.getnframes()
            first, last = round(body.start * rate), round(body.end * rate)
            if last > total or last - first < round(rate * 0.1):
                raise ValueError("裁剪范围必须在音频内，且至少保留 0.1 秒")
            reader.setpos(first)
            with wave.open(str(target), "wb") as writer:
                writer.setparams(reader.getparams())
                writer.writeframes(reader.readframes(last - first))
        write_json(
            settings.paths.cache / (key + ".json"),
            {
                "source_id": body.reference_id,
                "start": first / rate,
                "end": last / rate,
            },
        )
        return {
            "reference_id": key,
            "duration": (last - first) / rate,
            "preview_url": f"/api/previews/{key}",
        }

    @app.get("/api/tasks")
    def task_list():
        result = []
        for task in list(tasks.values())[-30:]:
            item = dict(task)
            # Older task records stored no timing or stage; avoid invented line counts.
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
        return {"pause_requested": True}

    @app.get("/api/previews/{filename}")
    def preview(filename: str):
        if not filename.endswith(".wav") or Path(filename).name != filename:
            raise HTTPException(404)
        p = settings.paths.cache / filename
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
