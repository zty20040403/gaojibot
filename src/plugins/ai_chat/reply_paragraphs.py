"""Pure reply boundaries, with caller-provided atomic/code ranges.

Blank lines and [split] end ordinary paragraphs. Fenced blocks are separate
paragraphs; their contents and structured/semantic atoms are never inspected as
control syntax. Returned offsets retain the caller's identity-bearing spans.
This only plans text: it grants no mention, quote or transport authority.
"""
from __future__ import annotations

from bisect import bisect_right
import re

_SEPARATOR = re.compile(r"\[split\]|\r?\n[^\S\r\n]*\r?\n|\r[^\S\r\n]*\r")


def paragraph_spans(source: str, *, protected=(), blocks=()) -> tuple[tuple[int, int], ...]:
    """Return nonempty spans, trimming only non-atomic surrounding whitespace.

    The renderer supplies sorted, disjoint ranges from its existing tokenizer;
    this helper never interprets model text as an identity or platform tag.
    """
    protected = tuple(protected)
    ends = tuple(end for _, end in protected)
    result = []

    def overlaps(start, end):
        index = bisect_right(ends, start)
        return index < len(protected) and protected[index][0] < end

    def add(start, end):
        while start < end and source[start].isspace() and not overlaps(start, start + 1):
            start += 1
        while end > start and source[end - 1].isspace() and not overlaps(end - 1, end):
            end -= 1
        if start < end:
            result.append((start, end))

    def ordinary(start, end):
        for match in _SEPARATOR.finditer(source, start, end):
            if overlaps(match.start(), match.end()):
                continue
            add(start, match.start())
            start = match.end()
        add(start, end)

    position = 0
    for start, end in blocks:
        ordinary(position, start)
        add(start, end)
        position = end
    ordinary(position, len(source))
    return tuple(result)
