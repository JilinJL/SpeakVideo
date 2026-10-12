from __future__ import annotations

from datetime import UTC

from .audio import audio_current, build_audio, mix_options
from .jobs import report, synthesize
from .project import Project
from .script.engine import OpenAICompatibleProvider, ScriptEngine
from .storage import lock, read_json, write_json


def engine(project: Project):
    return ScriptEngine(
        OpenAICompatibleProvider(project.settings.llm), project.settings.script.max_line_chars
    )


def generate_brief(project: Project):
    with lock(project.root / ".project.lock"):
        script = project.script()
        script.meta.brief = engine(project).brief(
            script.meta.topic, script.meta.keywords, script.meta.guidance
        )
        project.save(script, project.cast())
    return script.meta.brief


def generate_script(project: Project):
    with lock(project.root / ".project.lock"):
        old = project.script()
        if not old.meta.brief.strip():
            raise ValueError("请先生成/填写编剧阐述")
        script = engine(project).structure(
            old.meta.topic, old.meta.keywords, old.meta.brief, old.meta.guidance
        )
        if old.lines:
            backup = project.root / "history"
            backup.mkdir(exist_ok=True)
            from datetime import datetime

            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            write_json(backup / f"{stamp}.script.json", old.model_dump())
        project.save(script, project.cast())
        # Full regeneration has a fresh cache, even if the LLM happens to reuse IDs/text.
        manifest = read_json(project.root / "manifest.json")
        manifest["lines"] = {}
        manifest["status"] = "idle"
        write_json(project.root / "manifest.json", manifest)
    return script


def advance(project: Project, bgm=None, bgm_volume=None, cancelled=lambda: False) -> str:
    if not project.script().lines:
        if not project.script().meta.brief:
            generate_brief(project)
        generate_script(project)
    if not project.approved("script"):
        return "script_review"
    if not project.approved("binding"):
        return "binding_review"
    checks = report(project, False)
    if not checks["ok"]:
        raise ValueError("预检失败：" + "；".join(checks["errors"]))
    manifest = synthesize(project, cancelled=cancelled)
    if cancelled() or manifest["status"] == "paused":
        return "paused"
    options = mix_options(project, bgm, bgm_volume)
    if audio_current(project, options):
        return "complete" if project.approved("audio") else "audio_review"
    build_audio(project, bgm=bgm, bgm_volume=bgm_volume, cancelled=cancelled)
    return "audio_review"
