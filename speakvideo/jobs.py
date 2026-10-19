from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field

from .project import Project
from .storage import atomic_bytes, lock, read_json, sha256, write_json
from .tts.client import TTSClient, TTSError, wav_info
from .tts.grouping import SpeechGroup, join_group_audio, speech_groups
from .tts.preflight import preflight
from .voices import VoiceStore


class SynthesisOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concurrency: int = Field(default=1, ge=1, le=6, strict=True)


def synthesis_options(project: Project) -> SynthesisOptions:
    return SynthesisOptions.model_validate(read_json(project.root / "synthesis.json", {}))


def parameters(project: Project, line, cast: dict, *, group: SpeechGroup | None = None) -> dict:
    binding = cast[line.slot]
    emotion = binding.mood_map[line.mood]
    voice = VoiceStore(project.settings).path(binding.role, emotion)
    params = {
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
    if group is None:
        group = next(
            g
            for g in speech_groups(project.script().lines, cast)
            if any(item.id == line.id for item in g.lines)
        )
    if len(group.lines) > 1:
        params["speech_group"] = {
            "version": 1,
            "text": group.text,
            "line_ids": [item.id for item in group.lines],
            "words": [item.words for item in group.lines],
        }
    return params


def fingerprint(params: dict) -> str:
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def group_audio(project: Project, group: SpeechGroup, cast: dict, manifest: dict) -> bytes:
    """Read one complete generation unit, including lossless legacy recovery."""
    records = [manifest.get("lines", {}).get(line.id, {}) for line in group.lines]
    for line, record in zip(group.lines, records):
        if record.get("status") != "succeeded" or record.get("fingerprint") != fingerprint(
            parameters(project, line, cast, group=group)
        ):
            raise ValueError("语音组尚未生成或已失效")
    whole = any(record.get("audio_unit") == "group" for record in records)
    if whole:
        content = (project.root / "audio/groups" / f"{group.id}.wav").read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if any(
            record.get("audio_unit") != "group"
            or record.get("group_id") != group.id
            or record.get("sha256") != digest
            for record in records
        ):
            raise ValueError("语音组音频版本不一致")
    else:
        parts = []
        for line, record in zip(group.lines, records):
            part = (project.root / "audio/segments" / f"{line.id}.wav").read_bytes()
            if hashlib.sha256(part).hexdigest() != record.get("sha256"):
                raise ValueError("语音组音频已损坏")
            if wav_info(part)[0] != project.settings.audio.model_sample_rate:
                raise ValueError("旧语音组音频采样率不匹配")
            parts.append(part)
        content = join_group_audio(parts)
    if wav_info(content)[0] != project.settings.audio.model_sample_rate:
        raise ValueError("语音组音频采样率不匹配")
    return content


def reusable(
    project: Project, line, cast, record: dict | None, *, group=None, manifest=None
) -> bool:
    try:
        if not record or record.get("status") != "succeeded":
            return False
        if group is None:
            group = next(g for g in speech_groups(project.script().lines, cast) if line in g.lines)
        if manifest is None:
            manifest = read_json(project.root / "manifest.json", {"lines": {}})
        group_audio(project, group, cast, manifest)
        return record["fingerprint"] == fingerprint(parameters(project, line, cast, group=group))
    except (KeyError, OSError, ValueError, TTSError, StopIteration):
        return False


def report(project: Project, check_remote: bool = True) -> dict:
    script, cast = project.script(), project.cast()
    manifest = read_json(project.root / "manifest.json", {"lines": {}})
    groups = speech_groups(script.lines, cast)
    done = {
        line.id
        for group in groups
        if reusable(
            project,
            group.lines[0],
            cast,
            manifest["lines"].get(group.id),
            group=group,
            manifest=manifest,
        )
        for line in group.lines
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
    return {
        **asdict(result),
        "ok": result.ok,
        "reusable_ids": sorted(done),
        "speech_group_count": len(groups),
    }


def synthesize(
    project: Project,
    retry_line: str | None = None,
    cancelled=lambda: False,
    *,
    concurrency: int | None = None,
    retry_lines: list[str] | None = None,
):
    requested = set(retry_lines or []) | ({retry_line} if retry_line else set())
    limit = (
        synthesis_options(project).concurrency
        if concurrency is None
        else SynthesisOptions(concurrency=concurrency).concurrency
    )
    with lock(project.root / ".project.lock"), lock(project.settings.paths.cache / "gpu.lock"):
        script, cast = project.script(), project.cast()
        if requested - {line.id for line in script.lines}:
            raise ValueError("重试的行 ID 不存在")
        manifest_path = project.root / "manifest.json"
        manifest = read_json(manifest_path)
        with TTSClient(project.settings) as client:
            if client.mode == "gateway":
                VoiceStore(project.settings).remote_inventory(client)
            result = preflight(
                script,
                cast,
                client,
                max_line_chars=project.settings.script.max_line_chars,
                check_remote=False,
            )
            if not result.ok:
                raise ValueError("预检失败：" + "；".join(result.errors))
            groups = speech_groups(script.lines, cast)
            pending = [
                group
                for group in groups
                if (not requested or any(line.id in requested for line in group.lines))
                and (
                    requested
                    or not reusable(
                        project,
                        group.lines[0],
                        cast,
                        manifest["lines"].get(group.id),
                        group=group,
                        manifest=manifest,
                    )
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
                (cast[line.slot].role, cast[line.slot].mood_map[line.mood])
                for group in pending
                for line in group.lines
            }
            VoiceStore(project.settings).verify_remote(client, needed)
            manifest["status"] = "running"
            manifest["mode"] = project.settings.tts.mode
            manifest["concurrency"] = limit
            write_json(manifest_path, manifest)
            manifest_lock = threading.RLock()
            stopped = threading.Event()
            failures = []

            def should_stop():
                return cancelled() or stopped.is_set()

            def generate(group):
                if should_stop():
                    return
                line = group.lines[0]
                line_params = {
                    member.id: parameters(project, member, cast, group=group)
                    for member in group.lines
                }
                params = {**line_params[line.id], "text": group.text}
                fp = fingerprint(params)
                with manifest_lock:
                    records = (
                        manifest.setdefault("groups", {})
                        if len(group.lines) > 1
                        else manifest["lines"]
                    )
                    previous = records.get(group.id, {})
                    same = previous.get("fingerprint") == fp
                    record = {
                        "fingerprint": fp,
                        "parameters": params,
                        "status": "pending",
                        "attempts": previous.get("attempts", 0) if same else 0,
                    }
                    if (
                        same
                        and not requested
                        and previous.get("job_id")
                        and previous.get("status") != "failed"
                    ):
                        record["job_id"] = previous["job_id"]
                        record["idempotency_key"] = previous["idempotency_key"]
                    else:
                        generation = (
                            previous.get("attempts", 0)
                            if requested or previous.get("status") == "failed"
                            else 0
                        )
                        record["idempotency_key"] = (
                            f"{manifest['project_id']}-{line.id}-{fp}-{generation}"
                        )
                    records[group.id] = record

                    def sync_lines():
                        # All record mutations and serialization share the same lock.
                        if len(group.lines) > 1:
                            for member in group.lines:
                                manifest["lines"][member.id] = {
                                    **record,
                                    "fingerprint": fingerprint(line_params[member.id]),
                                    "parameters": line_params[member.id],
                                    "group_id": group.id,
                                    "group_tail": member.id == group.lines[-1].id,
                                }
                        write_json(manifest_path, manifest)

                    # Persist the idempotency key before any network request.
                    sync_lines()
                for attempt in range(project.settings.tts.max_retries + 1):
                    if should_stop():
                        return
                    with manifest_lock:
                        record["attempts"] += 1
                        record["status"] = "running"
                        sync_lines()
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
                                job_id = client.submit(payload, record["idempotency_key"])
                                with manifest_lock:
                                    record["job_id"] = job_id
                                    sync_lines()
                            audio = client.wait_job(record["job_id"], should_stop)
                        else:
                            audio = client.synthesize(
                                **{k: params[k] for k in ("text", "role", "emotion", "language")}
                            )
                        sr, duration = wav_info(audio.wav_bytes)
                        if sr != project.settings.audio.model_sample_rate:
                            raise ValueError(f"{line.id} 采样率为 {sr}，要求 24000；未落盘")
                        with manifest_lock:
                            path = (
                                project.root
                                / ("audio/groups" if len(group.lines) > 1 else "audio/segments")
                                / f"{group.id}.wav"
                            )
                            atomic_bytes(path, audio.wav_bytes)
                            record.update(
                                status="succeeded",
                                duration=duration,
                                sample_rate=sr,
                                sha256=sha256(path),
                                md5=hashlib.md5(audio.wav_bytes).hexdigest(),
                            )
                            record.pop("error", None)
                            for member in group.lines:
                                manifest["lines"][member.id] = {
                                    **record,
                                    "fingerprint": fingerprint(line_params[member.id]),
                                    "parameters": line_params[member.id],
                                    "group_id": group.id,
                                    "group_tail": member.id == group.lines[-1].id,
                                    "audio_unit": "group" if len(group.lines) > 1 else "line",
                                    "alignment": "group" if len(group.lines) > 1 else "line",
                                }
                            write_json(manifest_path, manifest)
                        return
                    except Exception as exc:
                        with manifest_lock:
                            record["error"] = str(exc)
                            # Unknown network outcome preserves the job and its key.
                            record["status"] = "pending" if client.mode == "gateway" else "failed"
                            if (
                                client.mode == "gateway"
                                and isinstance(exc, TTSError)
                                and str(exc).startswith("远端任务失败")
                            ):
                                record["status"] = "failed"
                            sync_lines()
                            if (
                                client.mode == "gateway"
                                or attempt == project.settings.tts.max_retries
                            ):
                                if not should_stop():
                                    failures.append(exc)
                                stopped.set()
                                raise
                        if should_stop():
                            return
                        time.sleep(min(2**attempt, 8))

            try:
                # Keep a bounded window, refill only when a group finishes.
                # HTTPX supports sharing the connection pool across these threads.
                with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="tts-group") as pool:
                    groups = iter(pending)
                    active = set()

                    def fill():
                        while len(active) < limit and not should_stop():
                            group = next(groups, None)
                            if group is None:
                                break
                            active.add(pool.submit(generate, group))

                    fill()
                    while active:
                        completed, active = wait(active, return_when=FIRST_COMPLETED)
                        for future in completed:
                            try:
                                future.result()
                            except Exception as exc:
                                # Workers retain the first concrete error; drain every active job.
                                with manifest_lock:
                                    if not failures and not cancelled():
                                        failures.append(exc)
                                stopped.set()
                        fill()
                if failures:
                    raise failures[0]
                manifest["status"] = (
                    "paused"
                    if cancelled()
                    else (
                        "succeeded"
                        if all(
                            reusable(project, line, cast, manifest["lines"].get(line.id))
                            for line in script.lines
                        )
                        else "partial"
                    )
                )
            except BaseException:
                manifest["status"] = "paused" if cancelled() else "failed"
                raise
            finally:
                write_json(manifest_path, manifest)
        return manifest
