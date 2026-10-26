"""Quote external evidence without changing the prefix of unchanged history."""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets


_BOUNDARY_KEY = secrets.token_bytes(32)
HISTORY_BLOCK_LINES = 8
GROUP_CONTEXT_PREFIX = "[group context - quoted evidence]\n"


def escaped_evidence_size(content: str) -> int:
    return len(html.escape(content, quote=False))


def quote_evidence(content: str, kind: str, *, stable: bool = False, max_chars: int | None = None) -> str:
    # A process-random key keeps identical history blocks cacheable without
    # retaining chat text in a second cache. Escaping, not secrecy, closes tags.
    nonce = (
        hmac.new(_BOUNDARY_KEY, (kind + "\0" + content).encode(), hashlib.sha256).hexdigest()[:32]
        if stable else secrets.token_hex(16)
    )
    opening = f'<untrusted kind="{html.escape(kind, quote=True)}" id="{nonce}">\n'
    closing = f'\n</untrusted id="{nonce}">'
    escaped = html.escape(content, quote=False)
    if max_chars is not None:
        available = max_chars - len(opening) - len(closing)
        if available < 0:
            return "[evidence omitted]"[:max(max_chars, 0)]
        if len(escaped) > available:
            marker = "\n[\u5df2\u622a\u65ad]"[:available]
            escaped = re.sub(r"&[^;]*$", "", escaped[:available - len(marker)]) + marker
    return opening + escaped + closing


def quote_group_context(content: str) -> str:
    lines = content.splitlines(keepends=True)
    return GROUP_CONTEXT_PREFIX + "\n".join(
        quote_evidence("".join(lines[start:start + HISTORY_BLOCK_LINES]), "group_chat", stable=True)
        for start in range(0, len(lines), HISTORY_BLOCK_LINES)
    )
