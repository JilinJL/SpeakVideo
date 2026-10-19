"""Bounded checks for the selected local TTS service."""

import time

from .tts.client import TTSClient, TTSError


def connection_status(settings):
    start = time.monotonic()
    bounded = settings.model_copy(deep=True)
    bounded.tts.timeout_seconds = 2
    result = {
        "mode": settings.tts.mode,
        "url": settings.tts.base_url,
        "ready": False,
        "gateway": "local",
        "worker": "unknown",
    }
    try:
        with TTSClient(bounded) as client:
            health = client.health()
        result.update(
            ready=health.get("ready") is True,
            worker="connected",
            model=health.get("model", ""),
            device=health.get("device", ""),
            message="配音服务已就绪" if health.get("ready") else "本机服务正在加载模型",
        )
        if settings.tts.mode == "demo":
            result["message"] = "离线测试音，不是真实配音"
    except TTSError as exc:
        result.update(message="本机配音服务未就绪，请在运行环境页安装模型并启动服务：" + str(exc))
    result["latency_ms"] = round((time.monotonic() - start) * 1000)
    return result
