from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict

from .project import Project
from .storage import atomic_bytes, lock, read_json, sha256, write_json
from .tts.client import TTSClient, TTSError, wav_info
from .tts.grouping import SpeechGroup, speech_groups, split_group_audio
from .tts.preflight import preflight
from .voices import VoiceStore


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


def reusable(project: Project, line, cast, record: dict | None, *, group=None) -> bool:
    try:
        if not record or record.get("status") != "succeeded":
            return False
        p = project.root / "audio" / "segments" / f"{line.id}.wav"
        return (
            record["fingerprint"] == fingerprint(parameters(project, line, cast, group=group))
            and record["sha256"] == sha256(p)
            and wav_info(p.read_bytes())[0] == project.settings.audio.model_sample_rate
        )
    except (KeyError, OSError, ValueError, TTSError, StopIteration):
        return False


def report(project: Project, check_remote: bool = True) -> dict:
    script, cast = project.script(), project.cast()
    manifest = read_json(project.root / "manifest.json", {"lines": {}})
    groups = speech_groups(script.lines, cast)
    done = {
        line.id
        for group in groups
        for line in group.lines
        if reusable(project, line, cast, manifest["lines"].get(line.id), group=group)
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


def synthesize(project: Project, retry_line: str | None = None, cancelled=lambda: False):
    with lock(project.root / ".project.lock"), lock(project.settings.paths.cache / "gpu.lock"):
        script, cast = project.script(), project.cast()
        if retry_line and retry_line not in {line.id for line in script.lines}:
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
                if (not retry_line or any(line.id == retry_line for line in group.lines))
                and (
                    retry_line
                    or not all(
                        reusable(project, line, cast, manifest["lines"].get(line.id), group=group)
                        for line in group.lines
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
            write_json(manifest_path, manifest)
            try:
                for group in pending:
                    if cancelled():
                        manifest["status"] = "paused"
                        break
                    line = group.lines[0]
                    # The group record owns a remote job; line records retain UI IDs.
                    records = (
                        manifest.setdefault("groups", {})
                        if len(group.lines) > 1
                        else manifest["lines"]
                    )
                    previous = records.get(group.id, {})
                    line_params = {
                        member.id: parameters(project, member, cast, group=group)
                        for member in group.lines
                    }
                    params = {**line_params[line.id], "text": group.text}
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
                    records[group.id] = record

                    def sync_lines():
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

                    sync_lines()
                    for attempt in range(project.settings.tts.max_retries + 1):
                        if cancelled():
                            manifest["status"] = "paused"
                            break
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
                            slices = split_group_audio(audio.wav_bytes, group.lines)
                            record.update(status="succeeded", duration=duration, sample_rate=sr)
                            record.pop("error", None)
                            for member, (wav, start_frame, end_frame) in zip(group.lines, slices):
                                path = project.root / "audio" / "segments" / f"{member.id}.wav"
                                atomic_bytes(path, wav)
                                target = (
                                    record
                                    if len(group.lines) == 1
                                    else {
                                        **record,
                                        "fingerprint": fingerprint(line_params[member.id]),
                                        "parameters": line_params[member.id],
                                        "group_id": group.id,
                                        "group_tail": member.id == group.lines[-1].id,
                                        "alignment": "estimated",
                                        "group_start_frame": start_frame,
                                        "group_end_frame": end_frame,
                                    }
                                )
                                target.update(
                                    duration=(end_frame - start_frame) / sr,
                                    sha256=sha256(path),
                                    md5=hashlib.md5(wav).hexdigest(),
                                )
                                manifest["lines"][member.id] = target
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
                            sync_lines()
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
