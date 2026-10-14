from __future__ import annotations

from datetime import UTC

from .audio import audio_current, build_audio, mix_options
from .jobs import report, synthesize
from .project import Project
from .script.engine import LLMCancelled, LLMHooks, OpenAICompatibleProvider, ScriptEngine
from .storage import lock, read_json, write_json


def engine(project: Project, hooks: LLMHooks | None = None):
    return ScriptEngine(
        OpenAICompatibleProvider(project.settings.llm, hooks),
        project.settings.script.max_line_chars,
    )


def _keep(hooks: LLMHooks | None):
    """Stopping after the model finished but before saving still discards the result."""
    if hooks is not None and hooks.cancelled():
        raise LLMCancelled("已停止生成，未保存任何内容")


def generate_brief(project: Project, hooks: LLMHooks | None = None):
    with lock(project.root / ".project.lock"):
        script = project.script()
        script.meta.brief = engine(project, hooks).brief(
            script.meta.topic,
            script.meta.keywords,
            script.meta.guidance,
            cast=script.cast,
            prompts=script.meta.prompts,
        )
        _keep(hooks)
        project.save(script, project.cast())
    return script.meta.brief


def extend_brief(project: Project, hooks: LLMHooks | None = None):
    with lock(project.root / ".project.lock"):
        script = project.script()
        extended = engine(project, hooks).extend_brief(script)
        _keep(hooks)
        from datetime import datetime

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        write_json(project.root / "history" / f"{stamp}.script.json", script.model_dump())
        script.meta.brief = extended
        project.save(script, project.cast())
    return extended


def generate_script(project: Project, hooks: LLMHooks | None = None):
    with lock(project.root / ".project.lock"):
        old = project.script()
        if not old.meta.brief.strip() and not old.meta.topic.strip():
            raise ValueError("请先填写话题或对话草稿")
        script = engine(project, hooks).structure(
            old.meta.topic,
            old.meta.keywords,
            old.meta.brief,
            old.meta.guidance,
            cast=old.cast,
            prompts=old.meta.prompts,
        )
        _keep(hooks)
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
        manifest["groups"] = {}
        manifest["status"] = "idle"
        write_json(project.root / "manifest.json", manifest)
    return script


def advance(
    project: Project,
    bgm=None,
    bgm_volume=None,
    cancelled=lambda: False,
    progress=lambda **kwargs: None,
    include_video: bool = False,
    llm=lambda kind: None,
) -> str:
    if not project.script().lines:
        if not project.script().meta.brief:
            progress(stage="正在调用模型撰写草稿")
            generate_brief(project, llm("brief"))
        if cancelled():
            return "paused"
        progress(stage="正在调用模型整理台词并校验格式")
        generate_script(project, llm("generate"))
    if not project.approved("script"):
        return "script_review"
    if not project.approved("binding"):
        return "binding_review"
    checks = report(project, False)
    if not checks["ok"]:
        raise ValueError("预检失败：" + "；".join(checks["errors"]))
    progress(stage="正在按连续角色分组合成配音", kind="lines")
    manifest = synthesize(project, cancelled=cancelled)
    if cancelled() or manifest["status"] == "paused":
        return "paused"
    options = mix_options(project, bgm, bgm_volume)
    if audio_current(project, options):
        if not project.approved("audio"):
            return "audio_review"
        if not include_video:
            return "complete"
        from .delivery import build_delivery
        from .video import render_video, video_current

        if not video_current(project):
            render_video(project, cancelled=cancelled, progress=progress)
        if not project.approved("video"):
            return "video_review"
        build_delivery(project, cancelled=cancelled, progress=progress)
        return "delivery_ready"
    progress(stage="正在合并音频、混音并生成字幕", kind="stage")
    build_audio(project, bgm=bgm, bgm_volume=bgm_volume, cancelled=cancelled)
    return "audio_review"
