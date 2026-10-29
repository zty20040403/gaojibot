"""Fixed-width durable queue lanes; complete identities remain in store rows."""
from __future__ import annotations

import hashlib

from src.plugins.ai_chat.platform_types import AccountKey


def account_job_kind(stage: str, account_key: str) -> str:
    if stage not in {"ingest", "turn", "subagent"} or AccountKey.parse(account_key).platform != "qq-official":
        raise ValueError("Invalid official account job stage")
    return "qq_official." + stage + "." + hashlib.sha256(account_key.encode("utf-8")).hexdigest()
