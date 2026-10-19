"""Global writing instructions and the shared JSON/audio contract."""

import json
from pathlib import Path

from ..storage import lock, read_json, write_json
from .moods import mood_vocab_for_prompt
from .schema import GeneratedScript, ScriptPrompts

DEFAULT_PERSONAS = {
    "p1": "好奇、敢问的聊天伙伴。直说疑惑，敢质疑常见说法，会顺着回答追问，不故意抬杠。",
    "p2": "耐心、讲得透的聊天伙伴。直接回答，用具体例子解释原因、过程和边界，不端着讲课。",
}

DIALOGUE_QUALITY_CONTRACT = """固定对话质量要求（包括自定义提示词、草稿、扩展和台词整理）：
围绕本次话题形成完整闭环：提出具体问题，解释原因并给出例子与适用边界，最后明确回答开头的问题，给出有实质意义的结论和自然收尾。
草稿阶段就必须完成结论与收尾；台词整理保留并落实这条闭环，草稿缺失结论时补齐，不能删掉结尾或无限引入新问题。
扩展用于把原话题讲透，新增部分也要落回清晰结论。结尾不留下未回答的追问，不用“下次再聊”代替回答，不把对话写成永无止境的闲聊。
使用明确的疑问语气：真正询问对方的句子必须带适合语义的疑问尾词并以问号结束，例如“这样理解对吗？”、“为什么会这样呢？”、“你说的是什么呢？”、“还能怎么做呢？”。
不能只写“为什么？”、“怎么回事？”、“聊什么？”这类缺少尾词的问句；用吗、么、呢等自然表达，不机械追加“么”改变原意。陈述句和引用的词语不强行加尾词。
每条台词至少 3 字、最多 200 字（首尾空白不计，标点计入），过短回应应和同一人的解释合并；长句沿语义边界拆开，保留完整意思。
"""


DEFAULT_BRIEF_PROMPT = """你负责写两个普通人之间自然、具体、有信息量的中文聊天对话。
默认 p1 比较敢问：把大家不好意思问的疑惑说出来，质疑习以为常的说法；大胆体现在问题本身，不靠辱骂、猎奇或故意抬杠。
默认 p2 能把事情讲透：先回应问题，再用生活场景、具体例子、因果过程解释；该区分的情况就区分，不用一句金句代替回答。
如果用户提供了人设或创作要求，以用户的设定为准。

从一个具体疑惑切入，直接开始聊天。下一句要接得住上一句：追问可以针对刚才的例子、漏洞或没听懂的地方，不是换个标题继续采访。
回答需要展开时就展开，允许一个人连续讲几段；另一个人也可以简短回应、打断、反问或表示没理解，不必每轮都一问一答。
用能自然说出口的语言，少用概念堆叠、排比和书面连接词。幽默、反差、停顿随内容自然出现，不必硬塞包袱、金句或升华。
涉及事实时区分事实、判断和假设，不编造数据、来源或亲身经历。
草稿必须围绕开头的问题形成闭环，末尾给出明确结论和自然收尾，不以新的未回答问题或“下次再聊”结束。
疑问句要带自然尾词，例如“对吗？”、“为什么呢？”，避免缺少疑问语气。

"""

DEFAULT_STRUCTURE_PROMPT = """根据草稿的基本思路和大致话题走向，生成和补全人物之间的对话，草稿中有省略轮次内容的地方你要考虑补全，完善。
如果提供的是创作想法、旧版编剧阐述，或者还没有草稿，就根据话题和要求写出自然的完整对话。

默认设定：
- p1 敢于提出尖锐但真实的疑问，语气可以带点不耐烦、好奇或怀疑，但不要刻意挑衅。
- p2 直接回应并耐心讲透，解释必须有具体例子、原因和适用边界。用户提供的人设与要求优先。

【关键要求：人味与口语感】
1. 台词必须是真人说话的样子，而不是书面表达。鼓励：
   - 口语化用词、省略、重复
   - 自然停顿和语气词（嗯、没毛病、那个、其实、说白了、你想啊……）
   - 不完全正确的语法和松散结构
2. 禁止书面腔、新闻腔、教科书腔。避免“首先……其次……最后……”这种分点结构出现在台词里，禁止发明晦涩的非生活化词语。
3. 追问必须由上一句回答自然引出，不要机械轮流。p1 可以因为听懂了或没听懂而改变语气和追问角度。
4. 长回答按语义和句子边界拆成同一说话人的连续台词，宁可一个人连续说三四句，也不要为了换人而硬切。
5. 情绪和停顿要克制、真实，通过用词和节奏体现，不要写括号动作或情绪标注。
6. 结尾自然明确地回答话题，不强制金句、反转或总结口号。
"""

BRIEF_OUTPUT_CONTRACT = "只输出可直接朗读的完整对话草稿，用 p1：、p2：标记说话人。不输出编剧分析、章节提纲、舞台说明或 JSON。"


def default_prompts() -> dict[str, str]:
    return {"brief": DEFAULT_BRIEF_PROMPT, "structure": DEFAULT_STRUCTURE_PROMPT}


def global_prompts(root: Path) -> ScriptPrompts:
    """Legacy project overrides never participate in global prompt selection."""
    saved = ScriptPrompts.model_validate(read_json(root / "script-prompts.json", {}))
    return ScriptPrompts(
        **{
            key: getattr(saved, key) if getattr(saved, key).strip() else value
            for key, value in default_prompts().items()
        }
    )


def save_global_prompts(root: Path, prompts: ScriptPrompts) -> ScriptPrompts:
    with lock(root / ".cache" / "script-prompts.lock"):
        write_json(root / "script-prompts.json", prompts.model_dump())
        return global_prompts(root)


def structure_examples(max_line_chars: int) -> list[dict]:
    """Concrete input/output pairs that obey the configured TTS line limit."""
    if not 3 <= max_line_chars <= 200:
        raise ValueError("单行台词上限必须为 3–200 字")
    cast = [{"slot": slot, "persona": persona} for slot, persona in DEFAULT_PERSONAS.items()]
    samples = [
        (
            "草稿转 JSON：详细解释拆成同一人的连续台词，并保留追问",
            "为什么学了自动化工具还是忙？",
            ["工具", "重复劳动"],
            "保留草稿原话，回答可以比问题长。",
            [
                ("p1", '工具不是号称"一键完成"吗，怎么学完还是这么忙呢？', "curious"),
                (
                    "p2",
                    "它省的是操作时间，但前提是你知道要做什么。比如整理表格，你得先确定哪些列要留下，哪些重复记录要删掉。",
                    "normal",
                ),
                (
                    "p2",
                    "如果这些规则没想清楚，工具只是帮你更快地做出一张错误的表。你还要检查、返工，时间可能反而花得更多。",
                    "serious",
                ),
                (
                    "p2",
                    "再比如同一个报表每周都要做，规则已经稳定了，自动化就能省掉反复复制、筛选、排版的时间。",
                    "normal",
                ),
                ("p1", "那我是不是应该先把流程理顺，再挑工具吗？", "curious"),
                (
                    "p2",
                    "对。先手动做一遍，找出重复的步骤，再试着把其中一步交给工具，看看结果对不对。",
                    "normal",
                ),
            ],
        ),
        (
            "无草稿：按话题生成自然问答，不输出提纲或舞台说明",
            "为什么休息时也会觉得内疚？",
            ["休息", "内疚"],
            "像朋友聊天，用假设场景解释，不编造经历或数据。",
            [
                ("p1", "我问个直白的，没在干活就一定是在浪费时间吗？", "curious"),
                (
                    "p2",
                    "不一定。假设你累得看同一段话看了三遍都没看懂，再坐半小时，未必比先休息一下更有效。",
                    "normal",
                ),
                ("p1", "可我一停下来就心虚，这是怎么回事呢？", "curious"),
                (
                    "p2",
                    "可能是你习惯用完成任务来判断一天有没有价值。停下来时，看不到新的成果，就容易觉得自己不够努力。",
                    "normal",
                ),
                (
                    "p2",
                    "你可以先想想，眼前的任务是真的紧急，还是只要闲着就会内疚。这两种情况，需要处理的问题不一样。",
                    "serious",
                ),
            ],
        ),
    ]
    if max_line_chars == 3:
        samples = [
            (
                name,
                topic,
                keywords,
                guidance,
                [
                    ("p1", "好吗？", "curious"),
                    ("p2", "可以。", "normal"),
                    ("p2", "先做。", "normal"),
                ],
            )
            for name, topic, keywords, guidance, _ in samples
        ]
    examples = []
    for index, (name, topic, keywords, guidance, turns) in enumerate(samples):
        lines = []
        for slot, words, mood in turns:
            count = (len(words) + max_line_chars - 1) // max_line_chars
            width, extra = divmod(len(words), count)
            chunks, offset = [], 0
            for chunk_index in range(count):
                end = offset + width + (chunk_index < extra)
                chunks.append(words[offset:end])
                offset = end
            for i, chunk in enumerate(chunks):
                lines.append(
                    {
                        "id": f"L{len(lines) + 1:03d}",
                        "slot": slot,
                        "words": chunk,
                        "mood": mood,
                        "pause_after_ms": 350 if i == len(chunks) - 1 else 150,
                    }
                )
        output = {
            "schema_version": "1.0",
            "template": "duo_dialogue",
            "meta": {},
            "cast": cast,
            "lines": lines,
        }
        examples.append(
            {
                "name": name,
                "input": {
                    "topic": topic,
                    "keywords": keywords,
                    "guidance": guidance,
                    "cast": cast,
                    "brief": "\n".join(f"{slot}：{words}" for slot, words, _ in turns)
                    if index == 0
                    else "",
                },
                "output": output,
            }
        )
    return examples


def structure_contract(max_line_chars: int) -> str:
    """Always append this contract and few-shot section, even with custom writing instructions."""
    rules = (
        "固定输出规范（创作要求只改变内容，不改变以下格式）：\n"
        "只返回一个完整 JSON 对象，不加 Markdown 代码围栏、注释、前后说明或省略号。\n"
        "顶层必须且只能有 schema_version、template、meta、cast、lines；"
        'schema_version="1.0"，template="duo_dialogue"。\n'
        "meta 必须为 {}；话题、原始草稿和提示词由程序保存，不在模型输出中重复。\n"
        "cast 必须恰好包含 p1、p2 各一次，每项只能有 slot 和 persona，保留输入人设。\n"
        "lines 必须非空，每项必须且只能有 id、slot、words、mood、pause_after_ms。\n"
        "id 用 L001、L002 这样唯一且稳定的字符串，只能包含 ASCII 字母、数字、下划线或横线，最长 30 字符。\n"
        "slot 只能为 p1 或 p2。words 是非空、非纯空白字符串，仅含实际说出口的台词；不含说话人前缀、标题或动作说明。\n"
        f"每行 words 至少 3 字、不超过 {max_line_chars} 字；这是单行限制，不是回答长度限制。"
        "长回答沿语义或句子边界拆成多行，保持同一 slot，保留解释和例子。\n"
        f"mood 只能使用：{mood_vocab_for_prompt()}；不确定时用 normal，不能用中文情绪名或自造词。\n"
        "pause_after_ms 必须是 0–10000 的整数，不接受字符串、小数、布尔值或负数；停顿按自然接话安排。\n"
        "不输出 role、voice、emotion、audio_path、pt_path 等音色或音频绑定信息。\n\n"
        "Few-shot：以下是完整的输入→合法 JSON 输出示例。"
        "学习字段、转义和拆行方式，不复制示例话题、台词或人设到本次结果。\n"
    )
    shots = "\n\n".join(
        f"示例 {index}：{example['name']}\n输入：\n"
        + json.dumps(example["input"], ensure_ascii=False, indent=2)
        + "\n输出：\n"
        + json.dumps(example["output"], ensure_ascii=False, indent=2)
        for index, example in enumerate(structure_examples(max_line_chars), 1)
    )
    return (
        DIALOGUE_QUALITY_CONTRACT
        + "\n\n"
        + rules
        + shots
        + "\n\n最终 JSON Schema：\n"
        + json.dumps(GeneratedScript.response_schema(max_line_chars), ensure_ascii=False, indent=2)
    )
