import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from speakvideo.project import Project
from speakvideo.script.engine import ScriptEngine
from speakvideo.script.prompts import (
    DEFAULT_BRIEF_PROMPT,
    DEFAULT_PERSONAS,
    DIALOGUE_QUALITY_CONTRACT,
    default_prompts,
    structure_contract,
    structure_examples,
)
from speakvideo.script.schema import CastMember, GeneratedScript, ScriptPrompts
from speakvideo.storage import read_json, write_json
from speakvideo.web import create_app
from speakvideo.workflow import generate_brief, generate_script


def test_custom_prompts_and_personas_reach_both_requests(project, monkeypatch):
    script = project.script()
    script.cast = [
        CastMember(slot="p1", persona="敢问职场问题的新人"),
        CastMember(slot="p2", persona="用真实场景解释的前辈"),
    ]
    script.meta.prompts = ScriptPrompts(brief="写一段朋友聊天。", structure="保留完整解释和追问。")
    script.meta.guidance = "先问公司为什么要求员工忠诚。"
    project.save(script, project.cast())
    output = script.model_copy(deep=True)
    output.cast[0].persona = "模型擅自改掉的人设"
    output.lines[1].slot = "p2"
    output.lines[2].slot = "p2"
    calls = []
    draft = "p1：公司为什么要我忠诚呢？\np2：先得看它对你有什么承诺。"

    def complete(self, system, user, *, json_output=False):
        calls.append((system, json.loads(user), json_output))
        return json.dumps({**output.model_dump(), "meta": {}}) if json_output else draft

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", complete)
    assert generate_brief(project) == draft
    result = generate_script(project)
    assert calls[0][0] == script.meta.prompts.brief + "\n\n" + DIALOGUE_QUALITY_CONTRACT
    assert calls[1][0].startswith(script.meta.prompts.structure)
    assert "JSON Schema" in calls[1][0]
    assert "同一 slot" in calls[1][0]
    for _, user, _ in calls:
        assert user["cast"] == [member.model_dump() for member in script.cast]
        assert user["guidance"] == script.meta.guidance
    assert calls[1][1]["brief"] == draft
    assert result.meta.prompts == script.meta.prompts
    assert result.cast == script.cast
    assert result.lines[1].slot == result.lines[2].slot == "p2"
    assert project.script() == result
    backup = list((project.root / "history").glob("*.script.json"))
    assert len(backup) == 1
    assert read_json(backup[0])["meta"]["brief"] == draft


def test_generate_directly_from_topic_without_brief(project, monkeypatch):
    expected = project.script()
    script = expected.model_copy(deep=True)
    script.meta.brief = ""
    script.lines = []
    project.save(script, project.cast())
    calls = []

    def complete(self, system, user, *, json_output=False):
        calls.append(json.loads(user))
        assert json_output
        return json.dumps({**expected.model_dump(), "meta": {}})

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", complete)
    result = generate_script(project)
    assert result.lines
    assert result.meta.brief == ""
    assert calls[0]["topic"] == script.meta.topic
    assert len(calls) == 1


def test_empty_topic_and_draft_rejected_before_llm(project, monkeypatch):
    script = project.script()
    script.meta.topic = " "
    script.meta.brief = "\n"
    project.save(script, project.cast())

    def unexpected(*args, **kwargs):
        pytest.fail("Empty input must not call the provider")

    monkeypatch.setattr("speakvideo.workflow.engine", unexpected)
    with pytest.raises(ValueError, match="话题或对话草稿"):
        generate_script(project)


def test_prompt_api_roundtrip_reset_and_legacy_script(project):
    legacy = read_json(project.root / "script.json")
    legacy["meta"].pop("prompts", None)
    write_json(project.root / "script.json", legacy)
    with TestClient(create_app(project.settings)) as client:
        assert client.get("/api/config").json()["script_prompts"] == default_prompts()
        body = client.get("/api/projects/test").json()
        assert body["script"]["meta"]["prompts"] == {"brief": "", "structure": ""}
        custom = {"brief": "允许追问和打断。", "structure": "详细回答，不要口号。"}
        body["script"]["meta"]["prompts"] = custom
        assert client.put("/api/projects/test", json=body).status_code == 200
        assert client.get("/api/projects/test").json()["script"]["meta"]["prompts"] == custom
        body["script"]["meta"]["prompts"] = {"brief": "", "structure": ""}
        assert client.put("/api/projects/test", json=body).status_code == 200
        assert project.script().meta.prompts == ScriptPrompts()
        body["script"]["meta"]["prompts"]["brief"] = "x" * 20001
        assert client.put("/api/projects/test", json=body).status_code == 422


def test_blank_prompt_uses_default_and_contract_still_validates(project):
    class Provider:
        def complete(self, system, user, *, json_output=False):
            if not json_output:
                assert system == DEFAULT_BRIEF_PROMPT + "\n\n" + DIALOGUE_QUALITY_CONTRACT
                return "p1：为什么？\np2：举个例子。"
            invalid = project.script().model_dump()
            invalid["meta"] = {}
            invalid["lines"][0]["mood"] = "invented"
            return json.dumps(invalid, ensure_ascii=False)

    engine = ScriptEngine(Provider())
    prompts = ScriptPrompts(brief=" \n", structure="写自由一点。")
    brief = engine.brief("话题", [], prompts=prompts)
    with pytest.raises(ValueError, match="封闭词表"):
        engine.structure("话题", [], brief, prompts=prompts)


def test_new_project_default_personas(project):
    fresh = Project(project.settings, "new-dialogue")
    fresh.create("一个大胆的问题")
    assert {member.slot: member.persona for member in fresh.script().cast} == DEFAULT_PERSONAS


def test_prompt_edits_preserve_legacy_approval_but_dialogue_edits_invalidate(project):
    legacy_script = project.script().model_dump()
    legacy_script["meta"].pop("prompts")
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_script, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    state = read_json(project.root / "workflow.json")
    state["approvals"]["script"] = legacy_digest
    write_json(project.root / "workflow.json", state)
    assert project.approved("script")
    project.approve("binding")
    script = project.script()
    script.meta.prompts = ScriptPrompts(brief="更大胆一点。", structure="把例子讲清楚。")
    project.save(script, project.cast())
    assert project.approved("script") and project.approved("binding")
    script.lines[0].words += "再问一句。"
    project.save(script, project.cast())
    assert not project.approved("script") and not project.approved("binding")


@pytest.mark.parametrize("limit", [3, 4, 20, 100, 200])
def test_complete_fewshots_obey_actual_contract_and_line_limit(limit):
    examples = structure_examples(limit)
    assert len(examples) == 2
    assert examples[0]["input"]["brief"] and examples[1]["input"]["brief"] == ""
    for example in examples:
        output = GeneratedScript.model_validate_json(json.dumps(example["output"]), strict=True)
        assert all(len(line.words) <= limit for line in output.lines)
        assert {member.slot for member in output.cast} == {"p1", "p2"}
        assert len({line.id for line in output.lines}) == len(output.lines)
    first = examples[0]
    original = "".join(turn.split("：", 1)[1] for turn in first["input"]["brief"].splitlines())
    assert "".join(line["words"] for line in first["output"]["lines"]) == original
    assert any(
        a["slot"] == b["slot"] == "p2"
        for a, b in zip(first["output"]["lines"], first["output"]["lines"][1:])
    )


def test_custom_writing_cannot_remove_attached_fewshots_and_ui_shows_same_contract(project):
    calls = []
    output = {**project.script().model_dump(), "meta": {}}

    class Provider:
        def complete(self, system, user, *, json_output=False):
            calls.append(system)
            return json.dumps(output)

    contract = structure_contract(200)
    ScriptEngine(Provider()).structure(
        "话题", [], "草稿", prompts=ScriptPrompts(structure="自由聊天。")
    )
    assert calls[0] == "自由聊天。\n\n" + contract
    with TestClient(create_app(project.settings)) as client:
        assert client.get("/api/config").json()["script_json_contract"] == contract
    schema = GeneratedScript.response_schema(200)
    assert schema["$defs"]["GeneratedLine"]["properties"]["words"]["maxLength"] == 200
    assert schema["$defs"]["GeneratedLine"]["properties"]["mood"]["enum"]


@pytest.mark.parametrize("field", ["schema_version", "template", "meta", "cast", "lines"])
def test_missing_top_level_field_is_rejected(project, field):
    output = {**project.script().model_dump(), "meta": {}}
    del output[field]
    with pytest.raises(ValueError):
        GeneratedScript.model_validate_json(json.dumps(output), strict=True)


@pytest.mark.parametrize("field", ["id", "slot", "words", "mood", "pause_after_ms"])
def test_missing_line_field_is_rejected(project, field):
    output = {**project.script().model_dump(), "meta": {}}
    del output["lines"][0][field]
    with pytest.raises(ValueError):
        GeneratedScript.model_validate_json(json.dumps(output), strict=True)


@pytest.mark.parametrize(
    "case",
    [
        "extra_root",
        "extra_meta",
        "extra_cast",
        "extra_line",
        "unknown_mood",
        "unknown_slot",
        "pause_string",
        "pause_float",
        "pause_bool",
        "pause_negative",
        "pause_too_long",
        "numeric_id",
        "unicode_id",
        "duplicate_id",
        "duplicate_slot",
        "blank_words",
        "short_line",
        "long_line",
        "empty_lines",
        "wrong_version",
        "wrong_template",
        "markdown",
        "trailing_prose",
        "duplicate_json_key",
    ],
)
def test_invalid_model_output_does_not_replace_project_or_cache(project, monkeypatch, case):
    original = {
        name: (project.root / name).read_bytes()
        for name in (
            "script.json",
            "cast.json",
            "workflow.json",
            "manifest.json",
        )
    }
    output = {**project.script().model_dump(), "meta": {}}
    line = output["lines"][0]
    if case == "extra_root":
        output["notes"] = "unexpected"
    elif case == "extra_meta":
        output["meta"]["prompt"] = "unexpected"
    elif case == "extra_cast":
        output["cast"][0]["voice"] = "unexpected"
    elif case == "extra_line":
        line["audio_path"] = "unexpected"
    elif case == "unknown_mood":
        line["mood"] = "happy"
    elif case == "unknown_slot":
        line["slot"] = "p3"
    elif case == "pause_string":
        line["pause_after_ms"] = "300"
    elif case == "pause_float":
        line["pause_after_ms"] = 300.0
    elif case == "pause_bool":
        line["pause_after_ms"] = True
    elif case == "pause_negative":
        line["pause_after_ms"] = -1
    elif case == "pause_too_long":
        line["pause_after_ms"] = 10001
    elif case == "numeric_id":
        line["id"] = 123
    elif case == "unicode_id":
        line["id"] = "台词一"
    elif case == "duplicate_id":
        output["lines"][1]["id"] = line["id"]
    elif case == "duplicate_slot":
        output["cast"][1]["slot"] = "p1"
    elif case == "blank_words":
        line["words"] = "  "
    elif case == "short_line":
        line["words"] = "对。"
    elif case == "long_line":
        line["words"] = "字" * 201
    elif case == "empty_lines":
        output["lines"] = []
    elif case == "wrong_version":
        output["schema_version"] = "2.0"
    elif case == "wrong_template":
        output["template"] = "blog"
    text = json.dumps(output)
    if case == "markdown":
        text = "```json\n" + text + "\n```"
    elif case == "trailing_prose":
        text += "\n生成完毕"
    elif case == "duplicate_json_key":
        text = text.replace('"meta": {}', '"meta": {}, "meta": {}', 1)

    def complete(self, system, user, *, json_output=False):
        return text

    monkeypatch.setattr("speakvideo.workflow.OpenAICompatibleProvider.complete", complete)
    with pytest.raises(ValueError):
        generate_script(project)
    assert {name: (project.root / name).read_bytes() for name in original} == original
    assert not (project.root / "history").exists()
