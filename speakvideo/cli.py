from __future__ import annotations

import json
from pathlib import Path

import typer

from .audio import build_audio
from .config import load_settings
from .jobs import report, synthesize
from .project import Project
from .storage import lock, read_json
from .tts.client import TTSClient
from .voices import VoiceStore
from .workflow import advance, generate_brief, generate_script

app = typer.Typer(help="SpeakVideo · 独立对谈视频工作台", no_args_is_help=True)
voices = typer.Typer(help="本地音色工厂")
script = typer.Typer(help="对话草稿、台词生成与验证")
app.add_typer(voices, name="voices")
app.add_typer(script, name="script")


def show(data):
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def project(name: str) -> Project:
    p = Project(load_settings(), name)
    if not (p.root / "script.json").exists():
        raise typer.BadParameter(f"工程不存在: {name}")
    return p


@app.command()
def init(name: str, topic: str = "", keywords: str = ""):
    p = Project(load_settings(), name)
    p.create(topic, [v.strip() for v in keywords.split(",") if v.strip()])
    typer.echo(f"已创建 {p.root}")


@app.command()
def doctor():
    import shutil

    s = load_settings()
    with TTSClient(s) as c:
        try:
            health = c.health()
        except Exception as exc:
            health = {"ready": False, "error": str(exc)}
    show(
        {
            "root": s.paths.root,
            "tts_mode": s.tts.mode,
            "tts": health,
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
            "llm_configured": bool(s.llm.endpoints()),
            "voices": VoiceStore(s).index(),
        }
    )


@app.command()
def tts(text: str, role: str, emotion: str = "平静", output: Path = Path("preview.wav")):
    with TTSClient(load_settings()) as c:
        from .storage import atomic_bytes

        atomic_bytes(output, c.synthesize(text=text, role=role, emotion=emotion).wav_bytes)
    typer.echo(str(output.resolve()))


@voices.command("list")
def voice_list(remote: bool = False):
    s = load_settings()
    if remote:
        with TTSClient(s) as c:
            show(c.list_roles())
    else:
        show(VoiceStore(s).list_roles())


@voices.command("import")
def voice_import(source: Path, overwrite: bool = False):
    s = load_settings()
    show({"imported": VoiceStore(s).import_directory(source, overwrite)})


@voices.command("sync")
def voice_sync():
    s = load_settings()
    with TTSClient(s) as c:
        show(c.list_roles())


@voices.command("create")
def voice_create(
    reference: Path, text: str, role: str, emotion: str = "平静", overwrite: bool = False
):
    s = load_settings()
    with lock(s.paths.cache / "gpu.lock"), TTSClient(s) as c:
        typer.echo(str(VoiceStore(s).create(c, reference, role, emotion, text, overwrite)))


@voices.command("design")
def voice_design(description: str, text: str, output: Path):
    s = load_settings()
    with lock(s.paths.cache / "gpu.lock"), TTSClient(s) as c:
        from .storage import atomic_bytes, write_json

        audio = c.synthesize_design(text=text, instruct=description)
        atomic_bytes(output, audio.wav_bytes)
        write_json(output.with_suffix(".json"), {"text": text, "instruct": description})
    typer.echo(f"试听 {output.resolve()}；满意后用 voices create 固化（需要相同 text）")


@script.command("brief")
def brief(name: str):
    typer.echo(generate_brief(project(name)))


@script.command("generate")
def generate(name: str):
    show(generate_script(project(name)).model_dump())


@script.command("validate")
def validate(path: Path):
    from .script.schema import Script, validate_ids

    s = Script.model_validate_json(path.read_text(encoding="utf-8"))
    errors = validate_ids(s)
    show({"ok": not errors, "errors": errors, "lines": len(s.lines)})
    if errors:
        raise typer.Exit(1)


@app.command()
def preflight(name: str, offline: bool = False):
    result = report(project(name), check_remote=not offline)
    show(result)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command()
def approve(name: str, gate: str):
    p = project(name)
    if gate == "binding":
        checks = report(p)
        if not checks["ok"]:
            raise typer.BadParameter("；".join(checks["errors"]))
        if not p.approved("script"):
            raise typer.BadParameter("请先确认当前剧本")
    p.approve(gate)
    typer.echo(f"已确认 {gate}")


@app.command()
def synth(name: str, retry_line: str | None = None):
    show(synthesize(project(name), retry_line))


@app.command()
def status(name: str):
    p = project(name)
    show(
        {
            "workflow": read_json(p.root / "workflow.json"),
            "manifest": read_json(p.root / "manifest.json"),
            "preflight": report(p, False),
        }
    )


@app.command()
def merge(
    name: str, bgm: Path | None = None, bgm_volume: float | None = typer.Option(None, min=0, max=1)
):
    """合并、双遍响度、可选 BGM 闪避、WAV/MP3、分角色音轨与 SRT。"""
    result = build_audio(project(name), bgm=bgm, bgm_volume=bgm_volume)
    show(result)


@app.command()
def capabilities():
    """读取本机语音接口能力。"""
    with TTSClient(load_settings()) as client:
        show(client.capabilities())


@app.command()
def render(name: str):
    """按已保存的视频设置合成成片。"""
    from .video import render_video

    show(render_video(project(name)))


@app.command()
def run(
    topic: str,
    keywords: str = "",
    episodes: int = typer.Option(1, min=1, max=30),
    name: str = "series",
    bgm: Path | None = None,
    video: bool = False,
):
    s = load_settings()
    for i in range(1, episodes + 1):
        p = Project(s, f"{name}-{i:02}")
        if not p.root.exists():
            p.create(topic, [k.strip() for k in keywords.split(",") if k.strip()])
        if p.script().meta.topic != topic:
            raise typer.BadParameter("工程名称已被其他话题使用，请更换 --name")
        stage = advance(p, bgm=bgm, include_video=video)
        typer.echo(f"{p.name}: {stage} → http://127.0.0.1:8765（serve 开启编辑页）")


@app.command()
def demo(name: str = "demo"):
    from .demo import populate

    s = load_settings()
    s.tts.mode = "demo"
    p = Project(s, name)
    if not p.root.exists():
        p.create("离线演示")
        populate(p)
    synthesize(p)
    result = build_audio(p)
    typer.echo(str(p.root / "exports" / result["files"]["master.wav"]["path"]))
    typer.echo("完成：正弦测试音 + 固定演示剧本，未调用 LLM/TTS 模型")


@app.command()
def serve(port: int = 8765):
    import uvicorn

    from .web import create_app

    settings = load_settings()
    from .inference.manager import RuntimeManager
    from .inference.models import model_path, validate_model

    manager = RuntimeManager(settings)
    if (
        settings.tts.mode == "managed"
        and manager.python.exists()
        and not validate_model(model_path(settings))
        and not manager.owned_process()
    ):
        import threading

        def autostart():
            try:
                manager.action("start")
            except Exception as exc:
                typer.echo(f"本机推理尚未就绪：{exc}")

        threading.Thread(target=autostart, daemon=True).start()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=port, access_log=False)


runtime_app = typer.Typer(help="本机模型安装、服务管理与离线校验")
app.add_typer(runtime_app, name="runtime")


@runtime_app.command("status")
def runtime_status():
    from .inference.manager import RuntimeManager

    show(RuntimeManager(load_settings()).status())


@runtime_app.command("config")
def runtime_config(
    model: str = "base-small",
    device: str = "auto",
    source: str = "huggingface",
    models_dir: Path | None = None,
    offline: bool = False,
    port: int = 8001,
):
    from .inference.manager import RuntimeManager

    show(
        RuntimeManager(load_settings()).save_config(
            dict(
                model=model,
                device=device,
                source=source,
                models_dir=models_dir,
                offline=offline,
                port=port,
            )
        )
    )


@runtime_app.command("action")
def runtime_action(action: str, key: str | None = None):
    from .inference.manager import RuntimeManager

    settings = load_settings()
    show(RuntimeManager(settings).action(action, key=key or settings.runtime.model))


if __name__ == "__main__":
    app()
