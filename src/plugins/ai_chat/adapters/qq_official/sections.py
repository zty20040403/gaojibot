"""QQ reply boundaries and Markdown display fixes."""
import re

from ...reply_paragraphs import paragraph_spans

_HEADING = re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+\S")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def code_ranges(text):
    start, marker, position = None, "", 0
    ranges = []
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line.rstrip("\r\n"))
        if match:
            run, rest = match.groups()
            if start is None:
                start, marker = position, run
            elif run[0] == marker[0] and len(run) >= len(marker) and not rest.strip():
                ranges.append((start, position + len(line)))
                start = None
        position += len(line)
    if start is not None:
        ranges.append((start, len(text)))
    return ranges


def section_spans(text, *, protected=(), blocks=(), max_chars=None):
    blocked = (*protected, *blocks)
    headings = [m.start() for m in _HEADING.finditer(text)
                if not any(start <= m.start() < end for start, end in blocked)]
    intro_end = headings[0] if headings else len(text)
    spans = list(paragraph_spans(text[:intro_end], protected=protected,
                                blocks=[pair for pair in blocks if pair[0] < intro_end]))
    for start, end in zip(headings, [*headings[1:], len(text)]):
        if max_chars is not None and end - start > max_chars:
            spans.extend((start + a, start + b) for a, b in paragraph_spans(
                text[start:end],
                protected=[(a-start, b-start) for a, b in protected if start <= a < end],
                blocks=[(a-start, b-start) for a, b in blocks if start <= a < end]))
        else:
            spans.append((start, end))
    result = []
    for start, end in spans:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end-1].isspace():
            end -= 1
        if start < end and text[start:end] != "---":
            result.append((start, end))
    return result


def ready_section_prefix(text):
    # Hold the last section until another heading arrives. Introductory text
    # can go out at a blank line. Never treat code/math as a heading.
    if re.match(r"^\s*\[(?:silence|沉默)(?:[:：][^\]]+)?\]", text):
        return "", text
    blocks = code_ranges(text)
    protected = [(m.start(), m.end()) for m in re.finditer(
        r"`[^`]+`|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]", text)]
    spans = section_spans(text, protected=protected, blocks=blocks)
    if len(spans) < 2:
        return "", text
    boundary = spans[-1][0]
    return text[:boundary], text[boundary:]


def normalize_markdown(text):
    blocks = code_ranges(text)
    result, position = [], 0
    for line in text.splitlines(keepends=True):
        in_code = any(a <= position < b for a, b in blocks)
        position += len(line)
        if in_code:
            result.append(line)
            continue
        if line.strip() == "---":
            continue
        line = re.sub(r"[ \t]+(?=\r?$|\r?\n)", "", line)
        # QQ swallows an ordinary space after bold; NBSP keeps it visible.
        line = re.sub(r"(\*\*[^*\n]+\*\*) +", lambda m: m[1] + "\u00a0", line)
        result.append(line)
    return "".join(result)


class ReplyStream:
    ready_prefix = staticmethod(ready_section_prefix)

    def __init__(self, platform):
        self.platform = platform
        self.index = 0

    async def __call__(self, text):
        await self.platform.send_fragment(text, self.index)
        self.index += 1
