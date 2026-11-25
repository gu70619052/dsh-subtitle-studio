"""字幕文本的行内折行。

中日韩字幕没有空格可依赖，libass 的自动折行按字体度量切，切出来的位置常常
把词打断。这里按「字数」折：优先在标点处断，找不到就硬切。`subtitles.maxLineChars`
就是它的上限，0 / 留空表示不折（交给 libass）。
"""

BREAK_AFTER = "，。！？；、,.!?;:：—… "


def wrap_text(text, max_chars, joiner="\n"):
    """把一行文本折成若干行，每行不超过 max_chars 个字符。

    max_chars <= 0 或文本本身不超时原样返回。允许在超限后的 1.6 倍窗口内
    寻找最近的标点，避免把一个完整的词从中间切断。
    """
    if not text or max_chars is None:
        return text
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        return text
    if limit <= 0:
        return text
    body = text.strip()
    if len(body) <= limit:
        return body
    lines = []
    rest = body
    while len(rest) > limit:
        window = rest[: int(limit * 1.6)]
        cut = 0
        for index, char in enumerate(window):
            if index + 1 >= limit and char in BREAK_AFTER:
                cut = index + 1
                break
        if cut <= 0:
            cut = limit
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        lines.append(rest)
    if not lines:
        return body
    return joiner.join(lines)
