"""Loopback-only optional Qwen server; launched in runtime/venv by the manager."""

from __future__ import annotations

import argparse
import gc
import io
import os
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import load_settings
from ..storage import atomic_bytes, lock
from ..voices import VoiceStore
from .models import model_path, validate_model
from .prompts import load_prompt, save_prompt


def device_and_dtype(requested="auto"):
    import torch

    if requested == "auto":
        requested = "cuda:0" if torch.cuda.is_available() else "mps"
    if requested == "mps":
        if not torch.backends.mps.is_available():
            raise ValueError("MPS 不可用，不回退 CPU")
        return requested, torch.float32
    if not requested.startswith("cuda") or not torch.cuda.is_available():
        raise ValueError("CUDA 不可用，不回退 CPU")
    index = int(requested.partition(":")[2] or 0)
    if index >= torch.cuda.device_count():
        raise ValueError("CUDA 设备不存在")
    with torch.cuda.device(index):
        return f"cuda:{index}", torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16


class CloneRequest(BaseModel):
    role: str
    emotion: str = "平静"
    ref_audio: str
    ref_text: str = Field(min_length=1, max_length=5000)
    overwrite: bool = False


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    role: str
    emotion: str = "平静"
    language: str = "Chinese"
    mode: str = "file"


class DesignRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    instruct: str = Field(min_length=1, max_length=2000)
    language: str = "Chinese"
    mode: str = "file"
    allow_download: bool = False


def create_worker(settings, instance, token):
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    settings.tts.mode = "managed"
    store = VoiceStore(settings)
    state = {"model": None, "key": None, "ready": False}
    mutex = threading.Lock()
    uploads = settings.paths.root / "runtime" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    device, dtype = device_and_dtype(settings.runtime.device)

    def get_model(key):
        if state["model"] is not None and state["key"] == key:
            return state["model"]
        path = model_path(settings, key)
        errors = validate_model(path)
        if errors:
            raise ValueError("请先在运行环境页下载完整模型：" + "; ".join(errors[:3]))
        old_key = state["key"]
        state.update(model=None, ready=False, key=None)
        gc.collect()
        if device == "mps":
            torch.mps.empty_cache()
        else:
            torch.cuda.empty_cache()
        try:
            print(f"加载 {key}，设备 {device}，精度 {dtype}", flush=True)
            model = Qwen3TTSModel.from_pretrained(
                str(path),
                device_map=device,
                dtype=dtype,
                attn_implementation="sdpa",
                local_files_only=True,
            )
            state.update(model=model, key=key, ready=True)
            return model
        except Exception:
            # Keep the configuration/weights. Attempt to restore the previous loaded model.
            if old_key and old_key != key:
                try:
                    model = Qwen3TTSModel.from_pretrained(
                        str(model_path(settings, old_key)),
                        device_map=device,
                        dtype=dtype,
                        attn_implementation="sdpa",
                        local_files_only=True,
                    )
                    state.update(model=model, key=old_key, ready=True)
                except Exception:
                    pass
            raise

    @asynccontextmanager
    async def lifespan(app):
        with lock(settings.paths.root / "runtime" / "inference.lock"):
            get_model(settings.runtime.model)
            try:
                yield
            finally:
                state.update(model=None, ready=False)

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def authorization(request: Request, call_next):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"detail": "推理服务认证失败"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def inference_failed(request, exc):
        message = str(exc)
        if "out of memory" in message.lower():
            message = "推理内存不足。请关闭占用资源的程序，或停止服务后切回 0.6B，再重启尝试。"
        return JSONResponse({"detail": message[:800]}, status_code=503)

    @app.get("/api/health")
    def health():
        return {
            "ready": state["ready"],
            "model": state["key"],
            "device": device,
            "instance": instance,
        }

    @app.get("/api/pt/list")
    def voices():
        return {"roles": store.list_roles(), "dir": str(store.root)}

    @app.get("/api/pt/download")
    def download(role: str, emotion: str):
        path = store.path(role, emotion)
        if not path.is_file():
            raise HTTPException(404, "音色不存在")
        return FileResponse(path, media_type="application/octet-stream")

    @app.post("/api/pt/upload")
    async def upload(file: UploadFile = File(...)):
        content = await file.read(32 * 1024 * 1024 + 1)
        if not content or len(content) > 32 * 1024 * 1024:
            raise ValueError("参考音频必须非空且不超过 32 MiB")
        sf.info(io.BytesIO(content))
        path = uploads / (uuid.uuid4().hex + ".wav")
        atomic_bytes(path, content)
        return {"path": str(path)}

    @app.post("/api/pt/create")
    def clone(body: CloneRequest):
        path = store.path(body.role, body.emotion)
        reference = uploads / os.path.basename(body.ref_audio)
        if reference.resolve() != Path(body.ref_audio).resolve() or not reference.is_file():
            raise ValueError("请先上传参考音频")
        if path.exists() and not body.overwrite:
            raise ValueError("同名音色已存在")
        try:
            with mutex, torch.inference_mode():
                items = get_model(settings.runtime.model).create_voice_clone_prompt(
                    ref_audio=str(reference), ref_text=body.ref_text.strip()
                )
                content = save_prompt(items, model_key=settings.runtime.model)
                load_prompt(content, expected_model=settings.runtime.model)
                atomic_bytes(path, content)
            return {"role": body.role, "emotion": body.emotion, "status": "ok"}
        finally:
            reference.unlink(missing_ok=True)

    def wav_bytes(wavs, sr):
        out = io.BytesIO()
        sf.write(out, wavs[0], sr, format="WAV", subtype="PCM_16")
        return Response(out.getvalue(), media_type="audio/wav")

    @app.post("/api/tts")
    def speech(body: SpeechRequest):
        with mutex, torch.inference_mode():
            path = store.path(body.role, body.emotion)
            if not path.is_file():
                raise ValueError("请先创建或导入音色")
            prompt = load_prompt(path.read_bytes(), expected_model=settings.runtime.model)
            return wav_bytes(
                *get_model(settings.runtime.model).generate_voice_clone(
                    text=body.text, language=body.language, voice_clone_prompt=prompt
                )
            )

    @app.post("/api/voice-design")
    def design(body: DesignRequest):
        with mutex, torch.inference_mode():
            return wav_bytes(
                *get_model("voicedesign").generate_voice_design(
                    text=body.text, language=body.language, instruct=body.instruct
                )
            )

    @app.post("/api/test")
    def test():
        roles = store.list_roles()
        if not roles:
            raise ValueError("模型已加载；真实试音请先在音色库上传原声并克隆一个音色")
        for role, emotions in roles.items():
            for emotion in emotions:
                try:
                    load_prompt(
                        store.path(role, emotion).read_bytes(),
                        expected_model=settings.runtime.model,
                    )
                except ValueError:
                    continue
                return speech(
                    SpeechRequest(text="你好，这是本机语音测试。", role=role, emotion=emotion)
                )
        raise ValueError("当前 Base 档位没有可用音色，请用原声在此档位重新克隆，再进行真实试音")

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default="")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if args.probe:
        from .environment import profile

        print(
            profile(settings.runtime.device, settings.runtime.model),
            device_and_dtype(settings.runtime.device),
        )
        return
    if not args.instance or not os.environ.get("SPEAKVIDEO_SERVICE_TOKEN"):
        raise SystemExit("请通过 speakvideo runtime start 启动")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    import uvicorn

    uvicorn.run(
        create_worker(settings, args.instance, os.environ["SPEAKVIDEO_SERVICE_TOKEN"]),
        host="127.0.0.1",
        port=settings.runtime.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
