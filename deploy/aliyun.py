"""Deploy via the configured my-server-root wrapper; never print credentials.

Build first: GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go -C gateway build
    -trimpath -ldflags='-s -w' -o ../.cache/speakvideo-gateway .
Run: uv run python deploy/aliyun.py
"""

from __future__ import annotations

import base64
import concurrent.futures
import gzip
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = Path("/home/example/.codex/skills/my-server-root/scripts/ssh-root.sh")
REMOTE = "/opt/speakvideo"


def remote(command: str) -> str:
    result = subprocess.run([str(WRAPPER), command], capture_output=True, text=True, timeout=90)
    if result.returncode:
        # The wrapper echoes its command. Never forward captured output containing secrets.
        raise RuntimeError(f"remote command failed (exit {result.returncode})")
    return result.stdout


def python(code: str) -> str:
    encoded = base64.b64encode(code.encode()).decode()
    return remote("python3 -c " + shlex.quote(f"import base64;exec(base64.b64decode('{encoded}'))"))


def save_secret(path: Path, content: str):
    path.parent.mkdir(exist_ok=True, parents=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    temporary.replace(path)
    path.chmod(0o600)


def upload(path: Path, destination: str):
    data = gzip.compress(path.read_bytes())
    encoded = base64.b64encode(data).decode()
    pieces = [encoded[i : i + 60000] for i in range(0, len(encoded), 60000)]
    folder = REMOTE + "/upload-" + hashlib.sha256(data).hexdigest()[:12]
    remote(f"install -d -m 700 {shlex.quote(folder)}")

    def part(item):
        i, piece = item
        remote(f"printf %s {shlex.quote(piece)} > {shlex.quote(folder + '/' + str(i).zfill(5))}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(part, enumerate(pieces)))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    python(f"""
import base64,gzip,hashlib,pathlib,os
folder=pathlib.Path({folder!r})
data=gzip.decompress(base64.b64decode(b''.join(p.read_bytes() for p in sorted(folder.iterdir()))))
assert hashlib.sha256(data).hexdigest()=={digest!r}, 'upload checksum mismatch'
target=pathlib.Path({destination!r})
temp=target.with_suffix('.new')
temp.write_bytes(data)
temp.chmod(0o755 if target.name=='speakvideo-gateway' else 0o644)
temp.replace(target)
for p in folder.iterdir(): p.unlink()
folder.rmdir()
""")
    print("Installed", path.name, flush=True)


def main():
    binary = ROOT / ".cache" / "speakvideo-gateway"
    if not binary.exists():
        raise SystemExit("Build the Linux gateway binary first")
    remote(
        f"install -d -m 755 {REMOTE}; id -u speakvideo >/dev/null 2>&1 || useradd --system --home {REMOTE} --shell /usr/sbin/nologin speakvideo"
    )
    python("""
import pathlib,secrets,os
p=pathlib.Path('/opt/speakvideo/gateway.env')
if not p.exists():
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        f.write('LISTEN_ADDR=127.0.0.1:18765\\nWORKER_ID=example-worker\\nCLIENT_TOKEN='+secrets.token_urlsafe(36)+'\\nWORKER_TOKEN='+secrets.token_urlsafe(36)+'\\n')
""")
    upload(binary, REMOTE + "/speakvideo-gateway")
    upload(
        ROOT / "deploy" / "speakvideo-gateway.service",
        "/etc/systemd/system/speakvideo-gateway.service",
    )
    upload(ROOT / "deploy" / "nginx-speakvideo.conf", "/etc/nginx/snippets/speakvideo.conf")
    # Known source anchor was inspected before deployment; make a reversible backup.
    python("""
import pathlib,subprocess,time
p=pathlib.Path('/etc/nginx/sites-enabled/blog.conf')
source=p.read_text()
include='    include /etc/nginx/snippets/speakvideo.conf;'
if include not in source:
    anchor='    root /var/www/jilinblog;'
    assert source.count(anchor)==1, 'nginx server anchor changed'
    backup=p.with_name(p.name+'.speakvideo-'+str(int(time.time()))+'.bak')
    # Backup outside sites-enabled to avoid nginx including it twice.
    backup=pathlib.Path('/opt/speakvideo')/backup.name
    backup.write_text(source)
    p.write_text(source.replace(anchor,include+'\\n'+anchor))
    if subprocess.run(['nginx','-t'],capture_output=True).returncode:
        p.write_text(source)
        raise RuntimeError('nginx validation failed; original restored')
""")
    remote(
        "nginx -t && systemctl daemon-reload && systemctl enable speakvideo-gateway && systemctl restart speakvideo-gateway && systemctl reload nginx && systemctl is-active speakvideo-gateway"
    )
    output = python("""
import json,pathlib
v=dict(line.split('=',1) for line in pathlib.Path('/opt/speakvideo/gateway.env').read_text().splitlines())
print('CREDENTIAL_RESULT:'+json.dumps(v))
""")
    # This raw SSH output remains private to this process; tool output only gets status.
    line = next(line for line in output.splitlines() if line.startswith("CREDENTIAL_RESULT:"))
    values = json.loads(line.split(":", 1)[1])
    save_secret(ROOT / ".cache" / "gateway-secrets.json", json.dumps(values))
    print("Gateway service active; credentials stored privately in .cache", flush=True)


if __name__ == "__main__":
    main()
