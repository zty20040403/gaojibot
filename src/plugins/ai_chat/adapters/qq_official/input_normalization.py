"""Trusted-dispatch text preparation, before any raw-body persistence.

Only the authenticated transport's Dispatch may be passed by the ingress/ingest
runtime. This structure is not proof of transport authentication and is never a
chat/tool input. Removing a mention grants no binding, owner or endpoint rights.
Control arguments live only in RAM; neither input_text nor a raw-body fallback
may be persisted for control/quarantine results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

from src.qq_official.events import Dispatch
from src.plugins.ai_chat.adapters.qq_official.binding import BindingCommand, parse_binding_command

NORMALIZATION_VERSION = 1
MAX_INPUT_BYTES = 262144
MAX_PREFIX_MENTIONS = 16
_PREFIX = re.compile(r"<@([^<>\r\n]{1,257})>")
_BINDING_MARKER = re.compile(r"(?:^|[\s>])[/!](?:bind|绑定)(?=$|\s)", re.IGNORECASE)
_WHOAMI = re.compile(r"^[/!]whoami(?=$|\s)", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedInput:
    kind: str  # message | control | quarantine
    input_text: str | None = field(default=None, repr=False)
    command: BindingCommand | None = field(default=None, repr=False)
    version: int = NORMALIZATION_VERSION
    stripped_mentions: int = 0
    reason: str = ""


def _native(value):
    try:
        return (isinstance(value, str) and bool(value.strip()) and len(value.encode()) <= 256
                and not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value))
    except UnicodeError:
        return False


def _trusted_tokens(mentions):
    """Do not turn a bot global ID into a group/C2C principal or infer from AppID.

    Explicit contradictory/malformed identity aliases and duplicate records block
    all affected tokens. A leading ! is a token syntax variant, never silently
    removed from an opaque ID: collisions with actual !-prefixed IDs are blocked.
    """
    records = {}
    for item in mentions:
        present = [item[key] for key in ("id", "member_openid", "user_openid") if key in item]
        candidates = {value for value in present if _native(value)}
        valid = (len(candidates) == 1 and all(_native(value) for value in present)
                 and item.get("is_you") is True and item.get("scope") in (None, "single")
                 and ("bot" not in item or item["bot"] is True))
        for native in candidates:
            records[native] = records.get(native, True) and valid
    by_token = {}
    for native, valid in records.items():
        for token in (f"<@{native}>", f"<@!{native}>"):
            by_token.setdefault(token, []).append((native, valid))
    return {token for token, values in by_token.items() if len(values) == 1 and values[0][1]}


def normalize_input(dispatch: Dispatch) -> NormalizedInput:
    """Pure, bounded preparation for TxA classification and TxB turn_text.

    TxA: control -> existing reducer only; quarantine -> body-free fixed-reason
    receipt only; message -> normal admission. TxB reruns the same function on
    the still-retained authenticated frame, verifies its source identity, and
    persists message input_text/version with ledger and canonical inbox pointer.
    It MUST reject control/quarantine, never fall back to raw content.
    """
    if not isinstance(dispatch, Dispatch) or dispatch.message is None:
        return NormalizedInput("quarantine", reason="input_not_message")
    text = dispatch.message.content
    try:
        valid = isinstance(text, str) and "\x00" not in text and len(text.encode()) <= MAX_INPUT_BYTES
    except UnicodeError:
        valid = False
    if not valid:
        return NormalizedInput("quarantine", reason="input_text_invalid")
    trusted = _trusted_tokens(dispatch.message.mentions)
    offset = 0
    count = 0
    while count < MAX_PREFIX_MENTIONS:
        leading = len(text[offset:]) - len(text[offset:].lstrip())
        match = _PREFIX.match(text, offset + leading)
        if match is None or match.group() not in trusted:
            break
        offset = match.end()
        count += 1
    normalized = text[offset:].lstrip() if count else text
    binding = parse_binding_command(normalized)
    if binding is not None:
        return NormalizedInput("control", command=binding, stripped_mentions=count)
    trimmed = normalized.lstrip()
    if _WHOAMI.match(trimmed):
        binding = (BindingCommand("whoami") if trimmed.casefold() in {"/whoami", "!whoami"}
                   else BindingCommand("invalid", valid=False))
        return NormalizedInput("control", command=binding, stripped_mentions=count)
    # A fake/unmatched prefix never authorizes a command. Privacy suppression is
    # deliberately broader: even a quoted/embedded binding command is withheld
    # from raw inbox/ledger/LLM so malformed addressing cannot disclose an OTP.
    if _BINDING_MARKER.search(text):
        return NormalizedInput("quarantine", stripped_mentions=count, reason="input_sensitive_control")
    return NormalizedInput("message", input_text=normalized, stripped_mentions=count)
