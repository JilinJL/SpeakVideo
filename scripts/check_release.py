"""Check source/artifact privacy without printing matched secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

PUBLIC_ASSETS = json.loads(Path(__file__).with_name("public_assets.json").read_text())

RULES = {
    "private-machine": re.compile(
        rb"(?:/Users/(?!Shared/)[A-Za-z0-9_-]+/|192[.]168[.][0-9]+[.][0-9]+)"
    ),
    "credential": re.compile(
        rb"(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})"
    ),
}
PRIVATE_ROOTS = (
    "projects/",
    "voices/",
    "voice-previews/",
    "models/",
    ".cache/",
    "runtime/",
    ".venv/",
    ".uv/",
)


def check(name, content):
    problems = []
    if (
        name.startswith(PRIVATE_ROOTS)
        or name in {".env", "config.local.toml", "script-prompts.json"}
        or (
            name.endswith((".pt", ".safetensors", ".wav", ".mp4"))
            and hashlib.sha256(content).hexdigest() != PUBLIC_ASSETS.get(name)
        )
    ):
        problems.append((name, "private-artifact"))
    if b"\0" not in content[:8192]:
        for rule, pattern in RULES.items():
            if pattern.search(content):
                problems.append((name, rule))
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history",
        action="store_true",
        help="scan every reachable blob; works in a bare candidate",
    )
    args = parser.parse_args()
    problems = []
    if args.history:
        records = subprocess.check_output(
            ["git", "rev-list", "--objects", "--all"], text=True
        ).splitlines()
        for record in records:
            sha, _, name = record.partition(" ")
            if not name:
                continue
            kind = subprocess.check_output(["git", "cat-file", "-t", sha], text=True).strip()
            if kind == "blob":
                content = subprocess.check_output(["git", "cat-file", "blob", sha])
                problems.extend(check(name, content))
    else:
        tracked = (
            subprocess.check_output(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
            )
            .decode()
            .split("\0")
        )
        for name in sorted(set(tracked)):
            path = Path(name)
            if name and path.is_file():
                problems.extend(check(name, path.read_bytes()))
        for path in Path("dist").glob("*.whl"):
            with zipfile.ZipFile(path) as wheel:
                names = wheel.namelist()
                for name in names:
                    problems.extend(check(name, wheel.read(name)))
                if not any(
                    name.endswith("assets/fonts/NotoSansCJKsc-Regular.otf") for name in names
                ):
                    problems.append((str(path), "font-missing"))
                if not any(name.endswith("inference/constraints/macos.txt") for name in names):
                    problems.append((str(path), "runtime-constraints-missing"))
        for path in Path("dist").glob("*.tar.gz"):
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    if member.isfile():
                        content = archive.extractfile(member).read()
                        name = member.name.partition("/")[2]
                        problems.extend(check(name, content))
    for name, rule in sorted(set(problems)):
        print(f"{rule}: {name}")
    if problems:
        raise SystemExit(1)
    print("Release source/history and built wheels passed the configured privacy checks.")


if __name__ == "__main__":
    main()
