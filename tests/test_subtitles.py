import pytest

from speakvideo.subtitles import subtitle_cues, text_chunks, timed_parts


@pytest.mark.parametrize(
    "text",
    [
        "第一句。第二句！第三句？",
        "没有句号但是很长的连续中文" * 12,
        "轻资产、低门槛，主要靠信息差赚钱，比如早期的币圈、元宇宙房产、某些AI工具，那卖课几乎是必然的终局。",
        "Example 1.5 keeps decimal punctuation, and long English text remains present." * 3,
    ],
)
def test_short_chunks_keep_all_text_and_punctuation(text):
    chunks = text_chunks(text)
    assert chunks and max(map(len, chunks)) <= 32
    assert "".join("".join(chunks).split()) == "".join(text.split())


def test_subtitle_cues_preserve_author_lines_and_cover_group_interval():
    row = {
        "words": "第一句。第二句。",
        "subtitle_texts": ["第一句。", "第二句。"],
        "start": 2.0,
        "end": 7.0,
    }
    cues = subtitle_cues(row)
    assert len(cues) == 2
    assert cues[0] == (2.0, 4.5, "第一句。")
    assert cues[1] == (4.5, 7.0, "第二句。")
    pages = timed_parts(2, 7, [["第一行", "第二行"], ["第三行"]])
    assert pages[0][0] == 2 and pages[-1][1] == 7
    assert pages[0][1] == pages[1][0]
    assert timed_parts(0, 1, []) == []
