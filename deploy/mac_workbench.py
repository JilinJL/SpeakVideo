"""Run the local editor under launchd so closing a terminal does not disconnect it."""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.speakvideo.workbench"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "restart", "stop", "uninstall", "status"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("仅适用于 macOS")
    root = Path(__file__).resolve().parent.parent
    path = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    domain = f"gui/{os.getuid()}"
    service = domain + "/" + LABEL
    if args.action == "status":
        subprocess.run(["launchctl", "print", service], check=True)
    elif args.action == "restart":
        subprocess.run(["launchctl", "kickstart", "-k", service], check=True)
    elif args.action in {"stop", "uninstall"}:
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)], check=False, capture_output=True
        )
        if args.action == "uninstall":
            path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        logs = root / ".cache" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit("请先安装 uv")
        content = {
            "Label": LABEL,
            "ProgramArguments": [uv, "run", "--no-sync", "speakvideo", "serve"],
            "WorkingDirectory": str(root),
            "EnvironmentVariables": {
                "PATH": ":".join(
                    dict.fromkeys(
                        [
                            str(Path(uv).parent),
                            "/opt/homebrew/bin",
                            "/usr/local/bin",
                            "/usr/bin",
                            "/bin",
                        ]
                    )
                )
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(logs / "workbench.stdout.log"),
            "StandardErrorPath": str(logs / "workbench.stderr.log"),
        }
        if path.exists():
            previous = plistlib.loads(path.read_bytes())
            if previous.get("WorkingDirectory") != str(root):
                raise SystemExit("已有其他目录注册工作台，请先从原目录停止该服务")
            subprocess.run(
                ["launchctl", "bootout", domain, str(path)], check=False, capture_output=True
            )
        path.write_bytes(plistlib.dumps(content))
        path.chmod(0o600)
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        print("已启动常驻工作台：http://127.0.0.1:8765/")


if __name__ == "__main__":
    main()
