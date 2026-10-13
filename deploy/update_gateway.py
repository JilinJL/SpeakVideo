"""Update only the existing gateway and its nginx snippet, retaining rollback copies.

Build .cache/speakvideo-gateway first. No credentials or Windows files are changed.
Run after approval: uv run python deploy/update_gateway.py
"""

from __future__ import annotations

import time

from aliyun import ROOT, remote, upload

from speakvideo.config import load_settings
from speakvideo.tts.client import TTSClient, TTSError

ACTIVATE = (
    "cp -p /opt/speakvideo/speakvideo-gateway /opt/speakvideo/speakvideo-gateway.previous"
    " && cp -p /etc/nginx/snippets/speakvideo.conf /opt/speakvideo/nginx-speakvideo.previous"
    " && install -m 755 /opt/speakvideo/speakvideo-gateway.candidate /opt/speakvideo/speakvideo-gateway.new"
    " && mv /opt/speakvideo/speakvideo-gateway.new /opt/speakvideo/speakvideo-gateway"
    " && install -m 644 /opt/speakvideo/nginx-speakvideo.candidate /etc/nginx/snippets/speakvideo.conf"
    " && nginx -t && systemctl restart speakvideo-gateway"
    " && systemctl reload nginx && systemctl is-active speakvideo-gateway"
)
ROLLBACK = (
    "install -m 755 /opt/speakvideo/speakvideo-gateway.previous /opt/speakvideo/speakvideo-gateway.new"
    " && mv /opt/speakvideo/speakvideo-gateway.new /opt/speakvideo/speakvideo-gateway"
    " && install -m 644 /opt/speakvideo/nginx-speakvideo.previous /etc/nginx/snippets/speakvideo.conf"
    " && nginx -t && systemctl restart speakvideo-gateway && systemctl reload nginx"
)


def main():
    binary = ROOT / ".cache/speakvideo-gateway"
    if not binary.is_file():
        raise SystemExit("Build .cache/speakvideo-gateway first")
    remote("systemctl is-active speakvideo-gateway")
    upload(binary, "/opt/speakvideo/speakvideo-gateway.candidate")
    upload(ROOT / "deploy/nginx-speakvideo.conf", "/opt/speakvideo/nginx-speakvideo.candidate")
    try:
        remote(ACTIVATE)
        settings = load_settings(ROOT)
        settings.tts.timeout_seconds = 8
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            try:
                with TTSClient(settings) as client:
                    docs = client.capabilities()
                    if not docs.get("gateway_capabilities"):
                        raise RuntimeError("Capability route is missing")
                    voices = client.manage_voices("list")
                    client._request("GET", "/v1/worker/metrics", timeout=8)
                print(
                    f"Gateway verified: {len(docs['rpc'])} RPCs, {len(voices['assets'])} assets",
                    flush=True,
                )
                return
            except (TTSError, RuntimeError, KeyError):
                time.sleep(2)
        raise RuntimeError("Worker did not reconnect or required routes failed")
    except Exception:
        remote(ROLLBACK)
        raise RuntimeError(
            "Gateway update failed; previous binary and nginx snippet restored"
        ) from None


if __name__ == "__main__":
    main()
