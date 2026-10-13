"""Bounded health probes distinguish gateway access, worker and inference readiness."""

import time

from .config import Settings
from .tts.client import TTSClient, TTSError


def connection_status(settings: Settings) -> dict:
    start = time.monotonic()
    bounded = settings.model_copy(deep=True)
    bounded.tts.timeout_seconds = 12
    result = {
        "mode": settings.tts.mode,
        "url": settings.tts.base_url,
        "worker_id": settings.tts.worker_id,
        "ready": False,
    }
    try:
        with TTSClient(bounded) as client:
            health = client.health()
        result.update(
            ready=health.get("ready") is True,
            gateway="connected",
            worker="connected",
            model=health.get("model", ""),
            device=health.get("device", ""),
            message="配音服务已就绪" if health.get("ready") else "已连接，模型尚未就绪",
        )
        if settings.tts.mode == "demo":
            result.update(gateway="demo", worker="demo", message="离线测试音，不是真实配音")
    except TTSError as exc:
        code = exc.status_code
        result.update(
            gateway="connected" if code else "unreachable",
            worker="offline" if code == 503 else "unknown",
            message="Windows Agent 离线，请确认 Windows 已开机且 SpeakVideoTtsAgent 正在运行"
            if code == 503
            else "网关认证失败，请检查 Client Token"
            if code in {401, 403}
            else str(exc),
        )
    result["latency_ms"] = round((time.monotonic() - start) * 1000)
    return result
