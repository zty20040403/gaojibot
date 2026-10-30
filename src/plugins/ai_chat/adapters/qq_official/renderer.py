"""Pure, bounded official-message plans. No transport, storage or credentials.

Native markup is created only from authorized structured mentions. Persistence
must retain this manifest and restore it with current trusted endpoint/mention
authority; a hash checks integrity, not authority. OTP bodies must never persist
via ``to_json`` (their sender uses ephemeral payloads and a keyed digest).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from itertools import chain
import json
import re
import unicodedata

from src.plugins.ai_chat.message_ir import (
    CardNode, EmoteNode, ForwardNode, MediaNode, MentionNode, MessageBody, TextNode,
)
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from src.qq_official.outgoing import escape_platform_tags
from .sections import normalize_markdown, section_spans

VERSION = "qq-render-v1"
MAX_SOURCE_CHARS = 262144
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_SEAL = object()
_NATIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_NATIVE_TAG = re.compile(r'<qqbot-at-user id="([A-Za-z0-9_-]{1,256})" />')
_UNSAFE_PREFIX = re.compile(r"<(?:/?qqbot(?:-|\b)|@|/?faceType\s*=|/?emoji(?::|\b))", re.I)
_FENCE_LINE = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n|\Z)")


class RenderError(ValueError):
    """Fixed reason only; never include message text or native identities."""


@dataclass(frozen=True, init=False)
class RenderedPart:
    index: int
    markdown_content: str = field(repr=False)
    text_content: str = field(repr=False)
    canonical_text: str = field(repr=False)
    mention_actors: tuple[ActorKey, ...] = field(repr=False)
    markdown_hash: str = field(repr=False)
    text_hash: str = field(repr=False)
    part_hash: str = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    @property
    def canonical_body(self) -> MessageBody:
        # No raw_data dict can mutate a plan after its digest is frozen.
        return MessageBody((TextNode(0, self.canonical_text),))


@dataclass(frozen=True, init=False)
class RenderedPlan:
    endpoint: EndpointKey = field(repr=False)
    status: str
    primary_format: str
    parts: tuple[RenderedPart, ...]
    reason: str
    warnings: tuple[str, ...]
    max_chars: int
    max_parts: int
    max_bytes: int | None
    math_mode: str
    source_hash: str = field(repr=False)
    plan_hash: str = field(repr=False)
    version: str
    _seal: object = field(repr=False, compare=False)

    def to_json(self) -> str:
        _require_sealed(self)
        return _json(_plan_dict(self))


@dataclass(frozen=True)
class _Fence:
    marker: str
    opener: str


@dataclass(frozen=True)
class _Unit:
    markdown: str
    text: str
    canonical: str
    before: _Fence | None
    after: _Fence | None
    actors: tuple[ActorKey, ...] = ()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new(cls, **values):
    result = object.__new__(cls)
    for key, value in values.items():
        object.__setattr__(result, key, value)
    object.__setattr__(result, "_seal", _SEAL)
    return result


def _require_sealed(value):
    if not isinstance(value, RenderedPlan) or getattr(value, "_seal", None) is not _SEAL:
        raise RenderError("unvalidated_plan")


def _valid_text(value):
    if not isinstance(value, str) or "\x00" in value:
        raise RenderError("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise RenderError("invalid_unicode") from None
    return value


def _limits(max_chars, max_parts, max_bytes):
    if type(max_chars) is not int or not 1 <= max_chars <= 20000:
        raise RenderError("invalid_max_chars")
    if type(max_parts) is not int or not 1 <= max_parts <= 16:
        raise RenderError("invalid_max_parts")
    if max_bytes is not None and (type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024):
        raise RenderError("invalid_max_bytes")


def _authorized(endpoint, allowed):
    if not isinstance(endpoint, EndpointKey) or endpoint.account.platform != "qq-official":
        raise RenderError("invalid_endpoint")
    if not isinstance(allowed, frozenset) or len(allowed) > 256 or any(not isinstance(a, ActorKey) for a in allowed):
        raise RenderError("invalid_mention_authority")
    return {
        actor.native_user_id: actor for actor in allowed
        if endpoint.kind == "group" and actor.identity_kind == "group_member"
        and actor.matches_endpoint(endpoint) and _NATIVE_ID.fullmatch(actor.native_user_id)
    }


def _flatten(body, authorized):
    if not isinstance(body, MessageBody) or len(body.nodes) > 4096:
        raise RenderError("invalid_body")
    pieces, spans, warnings = [], {}, set()
    size = 0
    unsupported = False
    for node in body.nodes:
        actor = None
        if isinstance(node, TextNode):
            text = node.text
        elif isinstance(node, MentionNode):
            text = "@" + (node.display or "群成员")
            actor = authorized.get(node.native_user_id)
            if actor is None:
                warnings.add("mention_as_text")
        elif isinstance(node, EmoteNode):
            text = "[" + (node.name or "表情" + node.native_id) + "]"
        elif isinstance(node, MediaNode):
            text = "[媒体尚未附送]"
            unsupported = True
        elif isinstance(node, CardNode):
            text = "[卡片摘要：" + " - ".join(value for value in (node.title, node.url) if value) + "]"
            warnings.add("card_as_text")
        elif isinstance(node, ForwardNode):
            text = "[合并转发摘要：" + (node.description or "内容未提供") + "]"
            warnings.add("forward_as_text")
        else:
            raise RenderError("unsupported_node")
        _valid_text(text)
        if actor is not None:
            spans[size] = (size + len(text), actor)
        pieces.append(text)
        size += len(text)
        if size > MAX_SOURCE_CHARS:
            return "", {}, warnings, "source_limit"
    return "".join(pieces), spans, warnings, "media_requires_attachment" if unsupported else ""


def _hangul(char):
    value = ord(char)
    if 0x1100 <= value <= 0x115F or 0xA960 <= value <= 0xA97C:
        return "L"
    if 0x1160 <= value <= 0x11A7 or 0xD7B0 <= value <= 0xD7C6:
        return "V"
    if 0x11A8 <= value <= 0x11FF or 0xD7CB <= value <= 0xD7FB:
        return "T"
    if 0xAC00 <= value <= 0xD7A3:
        return "LV" if (value - 0xAC00) % 28 == 0 else "LVT"
    return ""


def _cluster_end(text, start):
    """Conservative no-split units, not an assertion of complete UAX #29.

    Keep marks, emoji modifiers/tags, ZWJ/ZWNJ sequences, RI flag pairs, CRLF,
    Hangul syllables and virama conjuncts together. Over-grouping is safe: an
    oversized unit requests an attachment instead of severing a character.
    """
    end, regional, conjunct = start + 1, int(0x1F1E6 <= ord(text[start]) <= 0x1F1FF), False
    while end < len(text):
        prev, char = text[end - 1], text[end]
        code, category = ord(char), unicodedata.category(char)
        left, right = _hangul(prev), _hangul(char)
        mark = (category in {"Mn", "Mc", "Me"} or code in {0xFF9E, 0xFF9F}
                or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F)
        is_ri = 0x1F1E6 <= code <= 0x1F1FF
        hangul = ((left == "L" and right in {"L", "V", "LV", "LVT"})
                  or (left in {"LV", "V"} and right in {"V", "T"})
                  or (left in {"LVT", "T"} and right == "T"))
        # Non-format prepend characters; format characters are deliberately
        # over-grouped too, avoiding a boundary next to an unknown control.
        prepend = ord(prev) in {0x0D4E, 0x111C2, 0x111C3, 0x1193F, 0x11941, 0x11A3A, 0x11D46, 0x11F02}
        prepend = prepend or 0x11A84 <= ord(prev) <= 0x11A89
        join = (mark or prev in {"\u200d", "\u200c"} or char in {"\u200d", "\u200c"}
                or (prev == "\r" and char == "\n") or hangul
                or unicodedata.category(prev) == "Cf" or prepend or conjunct
                or (is_ri and regional % 2 == 1))
        if not join:
            break
        conjunct = unicodedata.combining(char) == 9 or (conjunct and mark)
        regional = regional + 1 if is_ri else 0
        end += 1
    return end


def _inline_end(source, start):
    run = len(source[start:]) - len(source[start:].lstrip("`"))
    close = re.compile(r"(?<!`)" + re.escape("`" * run) + r"(?!`)")
    match = close.search(source, start + run)
    return match.end() if match else None


def _math_end(source, start, opening, closing):
    pos = start + len(opening)
    while pos < len(source):
        if source.startswith(closing, pos):
            return pos + len(closing)
        if source[pos] == "\\":
            pos += 2
        else:
            pos += 1
    return None


def _units(source, spans, math_mode, warnings):
    pos, fence, indented_until = 0, None, 0
    while pos < len(source):
        if pos == 0 or source[pos - 1] == "\n":
            if fence is None and (source.startswith("    ", pos) or source.startswith("\t", pos)):
                newline = source.find("\n", pos)
                indented_until = len(source) if newline == -1 else newline + 1
                raw = source[pos:indented_until]
                safe = escape_platform_tags(raw)
                yield _Unit(safe, safe, raw, None, None)
                pos = indented_until
                continue
            match = _FENCE_LINE.match(source, pos)
            if match:
                marker, info = match.group(1), match.group(2)
                close = fence is not None and marker[0] == fence.marker[0] and len(marker) >= len(fence.marker) and not info.strip()
                opening = fence is None and not (marker[0] == "`" and "`" in info)
                if close or opening:
                    raw = match.group(0)
                    safe = escape_platform_tags(raw)
                    after = None if close else _Fence(marker, safe if safe.endswith("\n") else safe + "\n")
                    yield _Unit(safe, safe, raw, fence, after)
                    fence, pos = after, match.end()
                    continue
        if pos in spans:
            end, actor = spans[pos]
            raw = source[pos:end]
            safe = escape_platform_tags(raw)
            native = f'<qqbot-at-user id="{actor.native_user_id}" />'
            active = fence is None and pos >= indented_until
            yield _Unit(native if active else safe, safe, raw, fence, fence, (actor,) if active else ())
            pos = end
            continue
        if fence is None and pos >= indented_until and source[pos] == "`":
            end = _inline_end(source, pos)
            if end is not None:
                raw = source[pos:end]
                safe = escape_platform_tags(raw)
                # CommonMark inline code renders line endings as spaces. Keep
                # it on one wire line so a newly split message cannot turn a
                # three-backtick inline span into an unterminated code fence.
                rendered = safe.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
                yield _Unit(rendered, safe, raw, None, None)
                pos = end
                continue
            end = pos + len(source[pos:]) - len(source[pos:].lstrip("`"))
            raw = source[pos:end]
            yield _Unit("\\`" * len(raw), raw, raw, None, None)
            pos = end
            continue
        if fence is None and pos >= indented_until and source.startswith("~~~", pos):
            end = pos + len(source[pos:]) - len(source[pos:].lstrip("~"))
            raw = source[pos:end]
            yield _Unit("\\~" * len(raw), raw, raw, None, None)
            pos = end
            continue
        if fence is None and pos >= indented_until:
            matched = False
            for opening, closing, replacement in (("\\(", "\\)", "$"), ("\\[", "\\]", "$$"), ("$$", "$$", "$$"), ("$", "$", "$")):
                if not source.startswith(opening, pos):
                    continue
                end = _math_end(source, pos, opening, closing)
                if end is None:
                    continue
                raw = source[pos:end]
                inner = raw[len(opening):-len(closing)]
                converted = raw
                if math_mode == "dollar_delimiters" and opening.startswith("\\") and "$" not in inner:
                    converted = replacement + inner + replacement
                yield _Unit(escape_platform_tags(converted), escape_platform_tags(raw), raw, None, None)
                pos, matched = end, True
                break
            if matched:
                continue
        end = _cluster_end(source, pos)
        if fence is None and pos >= indented_until and source[pos] == "\\" and end < len(source):
            end = _cluster_end(source, end)
        raw = source[pos:end]
        safe = escape_platform_tags(raw)
        # Prefix detection uses the full source, even if a token ends at '<'.
        for offset in range(len(raw) - 1, -1, -1):
            if raw[offset] == "<" and _UNSAFE_PREFIX.match(source, pos + offset):
                safe = safe[:offset] + "&lt;" + safe[offset + 1:]
        yield _Unit(safe, safe, raw, fence, fence)
        pos = end
    if fence is not None:
        warnings.add("closed_unterminated_fence")


def _unicode_units(source, units):
    # Semantic token boundaries (e.g. the end of inline code) must not split a
    # following combining mark either. Glue until an actual Unicode boundary.
    boundaries, end = set(), 0
    while end < len(source):
        end = _cluster_end(source, end)
        boundaries.add(end)
    pending, consumed = [], 0
    for unit in units:
        pending.append(unit)
        consumed += len(unit.canonical)
        if consumed not in boundaries:
            continue
        yield _Unit("".join(item.markdown for item in pending), "".join(item.text for item in pending),
                    "".join(item.canonical for item in pending), pending[0].before, pending[-1].after,
                    tuple(actor for item in pending for actor in item.actors))
        pending.clear()
    if pending:
        raise RenderError("invalid_unicode_boundaries")


def _content(units):
    start, end = units[0].before, units[-1].after
    markdown = (start.opener if start else "") + "".join(unit.markdown for unit in units)
    if end:
        markdown += ("" if markdown.endswith("\n") else "\n") + end.marker
    return normalize_markdown(markdown), "".join(unit.text for unit in units), "".join(unit.canonical for unit in units)


def _fits(text, max_chars, max_bytes):
    return len(text) <= max_chars and (max_bytes is None or len(text.encode()) <= max_bytes)


def _part_dict(part):
    return {"index": part.index, "markdown_content": part.markdown_content,
            "text_content": part.text_content, "canonical_text": part.canonical_text,
            "mention_actors": [actor.key for actor in part.mention_actors],
            "markdown_hash": part.markdown_hash, "text_hash": part.text_hash,
            "part_hash": part.part_hash}


def _make_part(index, markdown, text, canonical, actors):
    values = dict(index=index, markdown_content=markdown, text_content=text, canonical_text=canonical,
                  mention_actors=tuple(sorted(set(actors), key=lambda actor: actor.key)),
                  markdown_hash=_digest(_json({"msg_type": 2, "markdown": {"content": markdown}})),
                  text_hash=_digest(_json({"msg_type": 0, "content": text})))
    encoded = {**values, "mention_actors": [actor.key for actor in values["mention_actors"]]}
    return _new(RenderedPart, **values, part_hash=_digest(_json(encoded)))


def _plan_dict(plan):
    return {"version": plan.version, "endpoint": plan.endpoint.key, "status": plan.status,
            "primary_format": plan.primary_format, "parts": [_part_dict(part) for part in plan.parts],
            "reason": plan.reason, "warnings": list(plan.warnings), "max_chars": plan.max_chars,
            "max_parts": plan.max_parts, "max_bytes": plan.max_bytes, "math_mode": plan.math_mode,
            "source_hash": plan.source_hash, "plan_hash": plan.plan_hash}


def _make_plan(endpoint, parts, *, reason, warnings, markdown, max_chars, max_parts, max_bytes, math_mode, source_hash):
    plan = _new(RenderedPlan, endpoint=endpoint, status="needs_attachment" if reason else "ready",
                primary_format="markdown" if markdown else "text", parts=tuple(parts), reason=reason,
                warnings=tuple(sorted(warnings)), max_chars=max_chars, max_parts=max_parts, max_bytes=max_bytes,
                math_mode=math_mode, source_hash=source_hash, plan_hash="", version=VERSION)
    values = _plan_dict(plan)
    values.pop("plan_hash")
    object.__setattr__(plan, "plan_hash", _digest(_json(values)))
    return plan


def _paragraph_sources(source, spans, math_mode, warnings, max_chars):
    # Reuse the renderer's token boundaries so mentions, inline code, math and
    # graphemes cannot become paragraph control syntax. Code blocks keep the
    # same safe fence handling when a single block needs length fragmentation.
    protected, blocks, position, block_start = [], [], 0, None
    for unit in _unicode_units(source, _units(source, spans, math_mode, warnings)):
        end = position + len(unit.canonical)
        if unit.before is None and unit.after is not None:
            block_start = position
        if unit.before is not None and unit.after is None:
            blocks.append((block_start, end))
            block_start = None
        if (unit.before is None and unit.after is None
                and len(unit.canonical) > 1 and unit.canonical != "\r\n"):
            protected.append((position, end))
        position = end
    if block_start is not None:
        blocks.append((block_start, len(source)))
    for start, end in section_spans(source, protected=protected, blocks=blocks, max_chars=max_chars):
        yield source[start:end], {offset - start: (stop - start, actor)
            for offset, (stop, actor) in spans.items() if start <= offset and stop <= end}


def render_plan(body: MessageBody, endpoint: EndpointKey, *, max_chars=2000, max_parts=4,
                max_bytes=None, markdown=True, allowed_mentions=frozenset(), math_mode="preserve",
                paragraphs=False, paragraph_parts=None) -> RenderedPlan:
    """Freeze wire parts once; only new conversational replies opt into paragraphs.

    paragraph_parts is a soft budget for natural boundaries, not a smaller
    wire-part limit. Remaining paragraphs share the last natural slot; length
    fragmentation can still use max_parts. If natural boundaries waste needed
    capacity, an explicit soft budget permits one fully packed replan. Hard
    overflow never yields a partial plan. Recovery restores frozen manifests.
    """
    _limits(max_chars, max_parts, max_bytes)
    authorized = _authorized(endpoint, allowed_mentions)
    if (type(markdown) is not bool or type(paragraphs) is not bool
            or math_mode not in {"preserve", "dollar_delimiters"}):
        raise RenderError("invalid_render_options")
    if paragraph_parts is not None and (type(paragraph_parts) is not int
            or not 1 <= paragraph_parts <= max_parts):
        raise RenderError("invalid_render_options")
    natural_parts = max_parts if paragraph_parts is None else paragraph_parts
    source, spans, warnings, reason = _flatten(body, authorized)
    if not source.strip() and not reason:
        raise RenderError("empty_body")
    parts, current = [], []
    canonical_source = source

    def flush():
        md, plain, canonical = _content(current)
        parts.append(_make_part(len(parts), md, plain, canonical, (a for u in current for a in u.actors)))
        current.clear()

    if not reason:
        groups = _paragraph_sources(source, spans, math_mode, warnings, max_chars) if paragraphs else ((source, spans),)
        for text, mentions in groups:
            units = _unicode_units(text, _units(text, mentions, math_mode, warnings))
            if current:
                if len(parts) < natural_parts - 1:
                    flush()
                else:
                    # Keep the overflow instead of silently dropping paragraphs.
                    units = chain((_Unit("\n\n", "\n\n", "\n\n", None, None),), units)
            for unit in units:
                candidate = [*current, unit]
                md, plain, canonical = _content(candidate)
                if len(canonical) <= max_chars and _fits(md, max_chars, max_bytes) and _fits(plain, max_chars, max_bytes):
                    current = candidate
                    continue
                if not current:
                    reason = "indivisible_unit_limit"
                    break
                flush()
                if len(parts) >= max_parts:
                    reason = "part_limit"
                    break
                current = [unit]
                md, plain, _ = _content(current)
                if not _fits(md, max_chars, max_bytes) or not _fits(plain, max_chars, max_bytes):
                    reason = "indivisible_unit_limit"
                    break
            if reason:
                break
        if not reason and current:
            flush()
        if not reason and not parts:
            raise RenderError("empty_body")
        if paragraphs and not reason:
            canonical_source = "".join(part.canonical_text for part in parts)
    if paragraphs and paragraph_parts is not None and paragraph_parts > 1 and reason == "part_limit":
        # This is still pure planning, before any durable plan or send exists.
        # Pack the whole normalized answer to retain the old hard capacity.
        return render_plan(body, endpoint, max_chars=max_chars, max_parts=max_parts,
            max_bytes=max_bytes, markdown=markdown, allowed_mentions=allowed_mentions,
            math_mode=math_mode, paragraphs=True, paragraph_parts=1)
    return _make_plan(endpoint, () if reason else parts, reason=reason, warnings=warnings, markdown=markdown,
                      max_chars=max_chars, max_parts=max_parts, max_bytes=max_bytes, math_mode=math_mode,
                      source_hash=_digest(canonical_source))


def _check_markup(content, actors):
    remaining = {actor.native_user_id for actor in actors}
    observed = set()
    for prefix in _UNSAFE_PREFIX.finditer(content):
        match = _NATIVE_TAG.match(content, prefix.start())
        if match is None or match.group(1) not in remaining:
            raise RenderError("untrusted_markup")
        observed.add(match.group(1))
    if observed != remaining:
        raise RenderError("mention_proof_mismatch")


def restore_plan(serialized: str, *, endpoint: EndpointKey, allowed_mentions=frozenset(),
                 max_chars=2000, max_parts=4, max_bytes=None) -> RenderedPlan:
    """Revalidate stored hashes, bounds and current mention authority.

    The database is trusted for the original plan's text. Recomputed SHA256 is
    not a signature and cannot authorize newly supplied endpoint/actor values.
    """
    _limits(max_chars, max_parts, max_bytes)
    authorized = _authorized(endpoint, allowed_mentions)
    _valid_text(serialized)
    if len(serialized.encode()) > MAX_MANIFEST_BYTES:
        raise RenderError("manifest_limit")
    try:
        data = json.loads(serialized)
        if data["endpoint"] != endpoint.key or data["version"] != VERSION:
            raise RenderError("manifest_identity_mismatch")
        _limits(data["max_chars"], data["max_parts"], data["max_bytes"])
        if (data["max_chars"] > max_chars or data["max_parts"] > max_parts
                or (max_bytes is not None and (data["max_bytes"] is None or data["max_bytes"] > max_bytes))):
            raise RenderError("manifest_limits_changed")
        if (data["primary_format"] not in {"markdown", "text"}
                or data["math_mode"] not in {"preserve", "dollar_delimiters"}
                or data["status"] != "ready" or data["reason"] != ""
                or not isinstance(data["parts"], list) or not 1 <= len(data["parts"]) <= data["max_parts"]):
            raise RenderError("manifest_not_sendable")
        parts = []
        for index, raw in enumerate(data["parts"]):
            if type(raw["index"]) is not int or raw["index"] != index:
                raise RenderError("invalid_part_order")
            actors = tuple(ActorKey.parse(value) for value in raw["mention_actors"])
            if len(actors) > 256 or any(authorized.get(actor.native_user_id) != actor for actor in actors):
                raise RenderError("mention_authority_changed")
            for name in ("markdown_content", "text_content", "canonical_text"):
                _valid_text(raw[name])
            if len(raw["canonical_text"]) > data["max_chars"]:
                raise RenderError("invalid_canonical_size")
            for name in ("markdown_content", "text_content"):
                if not raw[name] or not _fits(raw[name], data["max_chars"], data["max_bytes"]):
                    raise RenderError("invalid_part_size")
            _check_markup(raw["markdown_content"], actors)
            _check_markup(raw["text_content"], ())
            part = _make_part(index, raw["markdown_content"], raw["text_content"], raw["canonical_text"], actors)
            if _part_dict(part) != raw:
                raise RenderError("part_integrity_mismatch")
            parts.append(part)
        if not re.fullmatch(r"[a-f0-9]{64}", data["source_hash"]):
            raise RenderError("invalid_source_hash")
        if data["source_hash"] != _digest("".join(part.canonical_text for part in parts)):
            raise RenderError("source_integrity_mismatch")
        if not isinstance(data["warnings"], list) or any(value not in {
            "mention_as_text", "card_as_text", "forward_as_text", "closed_unterminated_fence",
        } for value in data["warnings"]):
            raise RenderError("invalid_warnings")
        plan = _make_plan(endpoint, parts, reason="", warnings=data["warnings"], markdown=data["primary_format"] == "markdown",
                          max_chars=data["max_chars"], max_parts=data["max_parts"], max_bytes=data["max_bytes"],
                          math_mode=data["math_mode"], source_hash=data["source_hash"])
        if _plan_dict(plan) != data:
            raise RenderError("plan_integrity_mismatch")
        return plan
    except RenderError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise RenderError("invalid_manifest") from None


def content_payload(plan: RenderedPlan, part_index: int, *, plain_fallback=False) -> dict:
    """Copy content only from a sealed plan; never accept caller-supplied markup.

    Sender owns fallback permission, acceptance evidence, msg_id/event_id,
    reference validation and allocated msg_seq. This function never retries.
    Do not pass its content through M0 markdown_payload: that intentionally
    escapes all native tags, including this plan's authorized mentions.
    """
    _require_sealed(plan)
    if (plan.status != "ready" or type(part_index) is not int or not 0 <= part_index < len(plan.parts)
            or type(plain_fallback) is not bool):
        raise RenderError("invalid_payload_selection")
    part = plan.parts[part_index]
    if plain_fallback or plan.primary_format == "text":
        return {"msg_type": 0, "content": part.text_content}
    return {"msg_type": 2, "markdown": {"content": part.markdown_content}}
