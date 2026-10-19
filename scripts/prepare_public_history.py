"""Prepare a scrubbed external bare clone; never push or rewrite the working repo."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path, help="new external directory for review")
    parser.add_argument(
        "--replacements",
        type=Path,
        required=True,
        help="Private external git-filter-repo replacement rules; never commit this file",
    )
    args = parser.parse_args()
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    destination = args.destination.expanduser().resolve()
    if destination.exists() or destination.is_relative_to(root):
        raise SystemExit("Choose a new directory outside the checkout.")
    replacements = args.replacements.expanduser().resolve()
    if not replacements.is_file() or replacements.is_relative_to(root):
        raise SystemExit("Replacement rules must be an existing private file outside the checkout.")
    subprocess.run(
        ["git", "clone", "--mirror", "--no-hardlinks", str(root), str(destination)], check=True
    )
    refs = subprocess.check_output(
        ["git", "for-each-ref", "--format=%(refname)"], cwd=destination, text=True
    ).splitlines()
    for ref in refs:
        # App checkpoints can be tree refs; never let unfiltered private objects survive.
        if ref not in {"refs/heads/main", "refs/heads/divide"} and not ref.startswith("refs/tags/"):
            subprocess.run(["git", "update-ref", "-d", ref], cwd=destination, check=True)
    command = [
        "uv",
        "tool",
        "run",
        "--from",
        "git-filter-repo",
        "git-filter-repo",
        "--force",
        "--invert-paths",
    ]
    for path in [
        "projects/",
        "voices/",
        "voice-previews/",
        "models/",
        ".cache/",
        "runtime/",
        "config.local.toml",
        "script-prompts.json",
        ".env",
    ]:
        command.extend(["--path", path])
    for pattern in [
        "docs/*.png",
        "docs/*.jpg",
        "*.pt",
        "*.safetensors",
        "*.wav",
        "*.mp4",
        ".env.*",
    ]:
        command.extend(["--path-glob", pattern])
    command.extend(["--replace-text", str(replacements), "--replace-message", str(replacements)])
    subprocess.run(command, cwd=destination, check=True)
    # The candidate contains only local refs. Publishing remains a separate reviewed operation.
    remotes = subprocess.check_output(["git", "remote"], cwd=destination, text=True).splitlines()
    if "origin" in remotes:
        subprocess.run(["git", "remote", "remove", "origin"], cwd=destination, check=True)
    print(f"Review candidate at {destination}. No remote push or visibility change was performed.")


if __name__ == "__main__":
    main()
