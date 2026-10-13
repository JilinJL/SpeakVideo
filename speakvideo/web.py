from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audio import audio_current, build_audio
from .config import Settings
from .connections import connection_status
from .jobs import report, synthesize
from .llm_config import public_llm, save_llm
from .processing import ffmpeg
from .project import Project
from .script.engine import OpenAICompatibleProvider
from .script.moods import MOOD_LABELS
from .script.schema import Script, VoiceBinding, validate_ids
from .storage import atomic_bytes, lock, read_json, write_json
from .tts.client import TTSClient, wav_info
from .voices import VoiceStore
from .workflow import advance, generate_brief, generate_script


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


def create_app(settings: Settings) -> FastAPI:
    settings.ensure_dirs()
    app = FastAPI(title="SpeakVideo", version="0.1.0")
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    tasks: dict[str, dict] = {}
    events: dict[str, threading.Event] = {}
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speakvideo")
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

    def submit(label: str, fn, project_name: str | None = None):
        if project_name and any(
            t.get("project") == project_name and t["status"] in {"queued", "running"}
            for t in tasks.values()
        ):
            raise HTTPException(409, "工程已有排队/执行中的任务")
        key = uuid.uuid4().hex
        item = {"id": key, "label": label, "project": project_name, "status": "queued"}
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
            persist()
            try:
                result = fn(events[key].is_set)
                item.update(
                    status="paused" if events[key].is_set() else "succeeded",
                    result=(
                        f"已交付 {len(result['files'])} 个音频/工程文件"
                        if isinstance(result, dict) and "files" in result
                        else f"有效配音 {sum(v.get('status') == 'succeeded' for v in result['lines'].values())} 行（含复用）"
                        if isinstance(result, dict) and "lines" in result
                        else f"生成 {len(result.lines)} 行剧本"
                        if isinstance(result, Script)
                        else str(result)[:500]
                    ),
                )
            except Exception as exc:
                item.update(status="paused" if events[key].is_set() else "failed", error=str(exc))
            finally:
                persist()

        executor.submit(execute)
        return item

    @app.get("/api/config")
    def configuration():
        return {
            "moods": MOOD_LABELS,
            "tts_mode": settings.tts.mode,
            "tts_url": settings.tts.base_url,
            "llm_configured": bool(settings.llm.endpoints()),
        }

    @app.get("/api/connections")
    def connections():
        return connection_status(settings)

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
        return VoiceStore(settings).list_roles()

    def mutable_voice():
        if any(
            t["status"] in {"queued", "running"}
            and t["label"] in {"sync", "create", "design", "preview", "synth", "retry", "run"}
            for t in tasks.values()
        ):
            raise HTTPException(409, "配音或音色任务运行中，请完成或暂停后再编辑音色")

    @app.get("/api/voices/assets")
    def voice_assets():
        return VoiceStore(settings).details()

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
            return VoiceStore(settings).import_pt(
                role, emotion, content, overwrite, ref_text, notes
            )

    @app.get("/api/voices/trash")
    def voice_trash():
        return VoiceStore(settings).trash()

    @app.post("/api/voices/trash/{key}/restore")
    def restore_voice(key: str):
        mutable_voice()
        with lock(settings.paths.cache / "gpu.lock"):
            return VoiceStore(settings).restore(key)

    @app.get("/api/voices/assets/{role}/{emotion}")
    def voice_detail(role: str, emotion: str):
        return VoiceStore(settings).detail(role, emotion)

    @app.get("/api/voices/assets/{role}/{emotion}/download")
    def download_voice(role: str, emotion: str):
        store = VoiceStore(settings)
        store.detail(role, emotion)
        path = store.path(role, emotion)
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.put("/api/voices/assets/{role}/{emotion}")
    def edit_voice(role: str, emotion: str, body: VoiceEdit):
        mutable_voice()
        with lock(settings.paths.cache / "gpu.lock"):
            return VoiceStore(settings).edit(
                role, emotion, body.role, body.emotion, body.ref_text, body.notes
            )

    @app.delete("/api/voices/assets/{role}/{emotion}")
    def delete_voice(role: str, emotion: str):
        mutable_voice()
        with lock(settings.paths.cache / "gpu.lock"):
            return VoiceStore(settings).delete(role, emotion)

    @app.get("/api/projects")
    def projects():
        return [
            {"name": path.parent.name, "topic": read_json(path)["meta"]["topic"]}
            for path in sorted(settings.paths.projects.glob("*/script.json"))
        ]

    @app.post("/api/projects", status_code=201)
    def new_project(body: NewProject):
        p = Project(settings, body.name)
        p.create(body.topic, body.keywords)
        return {"name": p.name}

    @app.get("/api/projects/{name}")
    def get_project(name: str):
        p = project(name)
        return {
            "script": p.script().model_dump(),
            "cast": {k: v.model_dump() for k, v in p.cast().items()},
            "workflow": read_json(p.root / "workflow.json"),
            "manifest": read_json(p.root / "manifest.json"),
            "preflight": report(p, False),
            "media": read_json(p.root / "media.json", {}),
            "audio_export": read_json(p.root / "exports/audio.json", {}),
            "audio_current": audio_current(p),
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
            "generate": lambda cancel: generate_script(p),
            "synth": lambda cancel: synthesize(p, cancelled=cancel),
            "retry": lambda cancel: synthesize(p, retry_line=body.line_id, cancelled=cancel),
            "merge": lambda cancel: build_audio(p, cancelled=cancel),
            "run": lambda cancel: advance(p, cancelled=cancel),
        }
        if body.action not in actions:
            raise ValueError("未知操作")
        if body.action == "retry" and not body.line_id:
            raise ValueError("请指定重试行")
        return submit(body.action, actions[body.action], name)

    @app.post("/api/projects/{name}/assets/{kind}")
    async def upload_asset(name: str, kind: str, file: UploadFile = File(...)):
        p = project(name)
        suffix = Path(file.filename or "").suffix.lower()
        if kind == "bgm" and suffix not in {".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
            raise ValueError("请选择音频文件")
        if kind != "bgm":
            raise ValueError("未知素材类型")
        relative = f"assets/{uuid.uuid4().hex}{suffix}"
        target = p.root / relative
        target.parent.mkdir(exist_ok=True)
        with lock(p.root / ".project.lock"), target.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
            media = read_json(p.root / "media.json", {})
            media["bgm"] = relative
            write_json(p.root / "media.json", media)
        return media

    @app.put("/api/projects/{name}/mix")
    def edit_mix(name: str, body: MixEdit):
        p = project(name)
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
            # MediaRecorder on Safari/Chromium emits mp4/webm; Qwen expects PCM WAV.
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
                    return client.sync_voices(VoiceStore(settings).assets())
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
                    if ref.name != body.reference_id or not (settings.paths.cache / ref).is_file():
                        raise ValueError("参考音频不存在")
                    preview = read_json(settings.paths.cache / (ref.name + ".json"))
                    if preview and preview["text"] != body.text:
                        raise ValueError("固化台词必须与试音台词一致")
                    return VoiceStore(settings).create(
                        client,
                        settings.paths.cache / ref,
                        body.role,
                        body.emotion,
                        body.text,
                        body.overwrite,
                    )
                raise ValueError("未知音色操作")

        return submit(body.action, execute)

    @app.get("/api/tasks")
    def task_list():
        result = []
        for task in list(tasks.values())[-30:]:
            item = dict(task)
            if task.get("project"):
                p = Project(settings, task["project"])
                script = p.script()
                manifest = read_json(p.root / "manifest.json", {"lines": {}})
                records = [manifest["lines"].get(line.id, {}) for line in script.lines]
                item["progress"] = {
                    "completed": sum(r.get("status") == "succeeded" for r in records),
                    "total": len(script.lines),
                    "failed": sum(r.get("status") == "failed" for r in records),
                    "current_line": next(
                        (
                            line.id
                            for line in script.lines
                            if manifest["lines"].get(line.id, {}).get("status") == "running"
                        ),
                        None,
                    ),
                }
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
