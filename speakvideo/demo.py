from .project import Project
from .script.schema import Script, VoiceBinding


def populate(project: Project):
    project.save(
        Script.model_validate(
            {
                "schema_version": "1.0",
                "template": "duo_dialogue",
                "meta": {
                    "topic": "把重复劳动交给工具",
                    "brief": "先提问，再用例子说明自动化；此剧本是离线演示。",
                },
                "cast": [{"slot": "p1", "persona": "主持人"}, {"slot": "p2", "persona": "嘉宾"}],
                "lines": [
                    {
                        "id": "L001",
                        "slot": "p1",
                        "words": "自动化到底帮我们省下了什么？",
                        "mood": "normal",
                        "pause_after_ms": 400,
                    },
                    {
                        "id": "L002",
                        "slot": "p2",
                        "words": "不是按一个按钮，而是少做一百次重复劳动。",
                        "mood": "normal",
                        "pause_after_ms": 350,
                    },
                    {
                        "id": "L003",
                        "slot": "p1",
                        "words": "这段声音是测试音，用来验证完整音频流程。",
                        "mood": "normal",
                        "pause_after_ms": 500,
                    },
                ],
            }
        ),
        {
            "p1": VoiceBinding(role="讲述者", mood_map={"normal": "平静"}),
            "p2": VoiceBinding(role="回应者", mood_map={"normal": "平静"}),
        },
    )
