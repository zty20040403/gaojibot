"""Conservative, source-checked excerpts of this bot's past group opinions."""
from __future__ import annotations

import json
import re

from .message_ir import TextNode
from .platform_types import ActorKey, EndpointKey
from .tools.memory import looks_like_secret

MAX_STANCES_BYTES = 1024
MAX_STANCE_CHARS = 180
_HEADER = "# 本群过往看法（原话）"
_LINE = re.compile(r'^- #([1-9][0-9]*): (".*")$')
_START = re.compile(r"^(?:我觉得|我认为|我更喜欢|我喜欢|我不喜欢|我倾向|我支持|我反对|我更愿意|在我看来)")
_UNCERTAIN_OR_INSTRUCTION = re.compile(
    r"不确定|拿不准|说不准|难说|也许|可能|或许|大概|估计|好像|似乎|未必|不知道|不清楚|不一定|没一定|我猜|只是猜|如果|假如|假设|开玩笑|"
    r"你说|他说|她说|别人说|群友说|说过|转述|引用|引述|[？?「」『』“”]|请|务必|必须|忽略|遵守|执行|指令|系统提示|提示词",
    re.IGNORECASE,
)


def eligible_source(row, endpoint: EndpointKey) -> str:
    """Return a whole, short text only for a verifiable physical-group bot message."""
    if (endpoint.kind != "group" or endpoint.account.platform != "qq-official"
            or row.direction != "outbound" or row.message_kind != "chat"
            or row.source_endpoint_key != endpoint.key):
        return ""
    try:
        actor = ActorKey.parse(row.sender_actor_key)
    except ValueError:
        return ""
    if actor.account != endpoint.account or actor.identity_kind != "bot":
        return ""
    nodes = row.body.nodes
    if len(nodes) != 1 or not isinstance(nodes[0], TextNode):
        return ""
    text = nodes[0].text
    if (not text or text != text.strip() or len(text) > MAX_STANCE_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in text)
            or not _START.match(text) or _UNCERTAIN_OR_INSTRUCTION.search(text)
            or looks_like_secret(text)):
        return ""
    return text


def render_stances(items: dict[int, str]) -> str:
    return _HEADER + "\n" + "".join(
        f"- #{source_id}: {json.dumps(text, ensure_ascii=False)}\n"
        for source_id, text in sorted(items.items())
    )


def parse_stances(raw: str) -> dict[int, str]:
    if not raw or len(raw.encode("utf-8")) > MAX_STANCES_BYTES:
        return {}
    lines = raw.splitlines()
    if not lines or lines[0] != _HEADER:
        return {}
    items: dict[int, str] = {}
    for line in lines[1:]:
        match = _LINE.fullmatch(line)
        if not match:
            return {}
        try:
            source_id, text = int(match.group(1)), json.loads(match.group(2))
        except (ValueError, RecursionError):
            return {}
        if source_id in items or not isinstance(text, str) or len(text) > MAX_STANCE_CHARS:
            return {}
        items[source_id] = text
    return items


def verified_stances(store, ledger, endpoint: EndpointKey) -> dict[int, str]:
    if endpoint.kind != "group":
        return {}
    items = parse_stances(store.read_stances(endpoint.scope.key))
    if not items:
        return {}
    rows = ledger.visible_messages_by_ids(endpoint.scope, list(items))
    return {
        row.canonical_message_id: items[row.canonical_message_id]
        for row in rows
        if row.canonical_message_id in items
        and eligible_source(row, endpoint) == items[row.canonical_message_id]
    }


def apply_proposals(
    existing: dict[int, str], sources: dict[int, str], times: dict[int, int], proposals: object,
) -> dict[int, str]:
    if not isinstance(proposals, list):
        return existing
    result = dict(existing)
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        source_id = proposal.get("source_id")
        if type(source_id) is not int or source_id not in sources or source_id in result:
            continue
        replacements = proposal.get("replaces", [])
        topic = proposal.get("topic", "")
        if not isinstance(replacements, list) or not isinstance(topic, str):
            continue
        if any(type(old_id) is not int or old_id not in result or old_id >= source_id
               or times[source_id] < times[old_id] for old_id in replacements):
            continue
        if replacements:
            topic = topic.strip().casefold()
            if (not 2 <= len(topic) <= 40 or topic in {"我觉得", "我认为", "我喜欢", "我不喜欢"}
                    or topic not in sources[source_id].casefold()
                    or any(topic not in result[old_id].casefold() for old_id in replacements)):
                continue
        candidate = {key: value for key, value in result.items() if key not in replacements}
        candidate[source_id] = sources[source_id]
        if len(render_stances(candidate).encode("utf-8")) <= MAX_STANCES_BYTES:
            result = candidate
    return result
