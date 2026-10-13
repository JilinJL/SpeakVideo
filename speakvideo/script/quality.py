"""Small deterministic guards for spoken Chinese, without another model call."""


def question_particles(text: str) -> str:
    """Complete unquoted questions with 呢 when a spoken ending is missing."""
    result = []
    quotes = []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    for char in text:
        if char in pairs:
            quotes.append(pairs[char])
        elif char == '"':
            if quotes and quotes[-1] == char:
                quotes.pop()
            else:
                quotes.append(char)
        elif quotes and char == quotes[-1]:
            quotes.pop()
        if char in "?？" and not quotes:
            previous = next((item for item in reversed(result) if not item.isspace()), "")
            lexical_me = previous == "么" and "".join(result).rstrip().endswith(
                ("什么", "怎么", "这么", "那么")
            )
            if previous and (previous not in "吗么呢吧啊嘛?？" or lexical_me):
                result.append("呢")
        result.append(char)
    return "".join(result)
