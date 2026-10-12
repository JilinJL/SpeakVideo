from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict

from .project import Project
from .storage import atomic_bytes, lock, read_json, sha256, write_json
from .tts.client import TTSClient, TTSError, wav_info
from .tts.preflight import preflight
from .voices import VoiceStore


def parameters(project: Project, line, cast: dict) -> dict:
    binding = cast[line.slot]
    emotion = binding.mood_map[line.mood]
    voice = VoiceStore(project.settings).path(binding.role, emotion)
    return {
        "text": line.words,
        "role": binding.role,
        "emotion": emotion,
        "language": "Chinese",
        "voice_sha256": sha256(voice),
        "generation_revision": project.settings.tts.generation_revision,
        "endpoint": project.settings.tts.base_url,
        "mode": project.settings.tts.mode,
        **(
            {"worker_id": project.settings.tts.worker_id}
            if project.settings.tts.mode == "gateway"
            else {}
        ),
    }


def fingerprint(params: dict) -> str:
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def reusable(project: Project, line, cast, record: dict | None) -> bool:
    try:
        if not record or record.get("status") != "succeeded":
            return False
        p = project.root / "audio" / "segments" / f"{line.id}.wav"
        return (
            record["fingerprint"] == fingerprint(parameters(project, line, cast))
            and record["sha256"] == sha256(p)
            and wav_info(p.read_bytes())[0] == project.settings.audio.model_sample_rate
        )
    except (KeyError, OSError, ValueError, TTSError):
        return False


def report(project: Project, check_remote: bool = True) -> dict:
    script, cast = project.script(), project.cast()
    manifest = read_json(project.root / "manifest.json", {"lines": {}})
    done = {
        line.id
        for line in script.lines
        if reusable(project, line, cast, manifest["lines"].get(line.id))
    }
    with TTSClient(project.settings) as client:
        result = preflight(
            script,
            cast,
            client,
            max_line_chars=project.settings.script.max_line_chars,
            done_ids=done,
            check_remote=check_remote,
        )
    return {**asdict(result), "ok": result.ok}


def synthesize(project: Project, retry_line: str | None = None, cancelled=lambda: False):
    with lock(project.root / ".project.lock"), lock(project.settings.paths.cache / "gpu.lock"):
        script, cast = project.script(), project.cast()
        if retry_line and retry_line not in {line.id for line in script.lines}:
            raise ValueError("重试的行 ID 不存在")
        manifest_path = project.root / "manifest.json"
        manifest = read_json(manifest_path)
        with TTSClient(project.settings) as client:
            result = preflight(
                script,
                cast,
                client,
                max_line_chars=project.settings.script.max_line_chars,
                check_remote=False,
            )
            if not result.ok:
                raise ValueError("预检失败：" + "；".join(result.errors))
            pending = [
                line
                for line in script.lines
                if (not retry_line or line.id == retry_line)
                and (
                    retry_line or not reusable(project, line, cast, manifest["lines"].get(line.id))
                )
            ]
            if not pending:
                manifest["status"] = "succeeded"
                write_json(manifest_path, manifest)
                return manifest
            result = preflight(
                script, cast, client, max_line_chars=project.settings.script.max_line_chars
            )
            if not result.ok:
                raise ValueError("预检失败：" + "；".join(result.errors))
            needed = {
                (cast[line.slot].role, cast[line.slot].mood_map[line.mood]) for line in pending
            }
            VoiceStore(project.settings).verify_remote(client, needed)
            manifest["status"] = "running"
            manifest["mode"] = project.settings.tts.mode
            write_json(manifest_path, manifest)
            try:
                for line in script.lines:
                    if cancelled():
                        manifest["status"] = "paused"
                        break
                    if retry_line and line.id != retry_line:
                        continue
                    previous = manifest["lines"].get(line.id, {})
                    if not retry_line and reusable(project, line, cast, previous):
                        continue
                    params = parameters(project, line, cast)
                    fp = fingerprint(params)
                    same = previous.get("fingerprint") == fp
                    record = {
                        "fingerprint": fp,
                        "parameters": params,
                        "status": "pending",
                        "attempts": previous.get("attempts", 0) if same else 0,
                    }
                    if (
                        same
                        and not retry_line
                        and previous.get("job_id")
                        and previous.get("status") != "failed"
                    ):
                        record["job_id"] = previous["job_id"]
                        record["idempotency_key"] = previous["idempotency_key"]
                    else:
                        # Persist this BEFORE POST, including lost-ack recovery.
                        generation = (
                            previous.get("attempts", 0)
                            if retry_line or previous.get("status") == "failed"
                            else 0
                        )
                        record["idempotency_key"] = (
                            f"{manifest['project_id']}-{line.id}-{fp}-{generation}"
                        )
                    manifest["lines"][line.id] = record
                    write_json(manifest_path, manifest)
                    for attempt in range(project.settings.tts.max_retries + 1):
                        if cancelled():
                            manifest["status"] = "paused"
                            break
                        record["attempts"] += 1
                        record["status"] = "running"
                        write_json(manifest_path, manifest)
                        try:
                            payload = {
                                k: params[k]
                                for k in (
                                    "text",
                                    "role",
                                    "emotion",
                                    "language",
                                    "voice_sha256",
                                    "generation_revision",
                                )
                            }
                            if client.mode == "gateway":
                                if not record.get("job_id"):
                                    record["job_id"] = client.submit(
                                        payload, record["idempotency_key"]
                                    )
                                    write_json(manifest_path, manifest)
                                audio = client.wait_job(record["job_id"], cancelled)
                            else:
                                audio = client.synthesize(
                                    **{
                                        k: params[k]
                                        for k in ("text", "role", "emotion", "language")
                                    }
                                )
                            sr, duration = wav_info(audio.wav_bytes)
                            if sr != project.settings.audio.model_sample_rate:
                                raise ValueError(f"{line.id} 采样率为 {sr}，要求 24000；未落盘")
                            path = project.root / "audio" / "segments" / f"{line.id}.wav"
                            atomic_bytes(path, audio.wav_bytes)
                            record.update(
                                status="succeeded",
                                duration=duration,
                                sample_rate=sr,
                                sha256=sha256(path),
                                md5=hashlib.md5(audio.wav_bytes).hexdigest(),
                            )
                            record.pop("error", None)
                            write_json(manifest_path, manifest)
                            break
                        except Exception as exc:
                            record["error"] = str(exc)
                            # Gateway polling failures keep job_id; never blindly resubmit.
                            record["status"] = "pending" if client.mode == "gateway" else "failed"
                            if (
                                client.mode == "gateway"
                                and isinstance(exc, TTSError)
                                and str(exc).startswith("远端任务失败")
                            ):
                                record["status"] = "failed"
                            write_json(manifest_path, manifest)
                            if (
                                client.mode == "gateway"
                                or attempt == project.settings.tts.max_retries
                            ):
                                raise
                            time.sleep(min(2**attempt, 8))
                    if manifest["status"] == "paused":
                        break
                else:
                    manifest["status"] = (
                        "succeeded"
                        if all(
                            reusable(project, line, cast, manifest["lines"].get(line.id))
                            for line in script.lines
                        )
                        else "partial"
                    )
            except BaseException:
                manifest["status"] = "paused" if cancelled() else "failed"
                raise
            finally:
                write_json(manifest_path, manifest)
        return manifest
