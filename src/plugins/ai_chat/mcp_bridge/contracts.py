"""Bounded, data-only registration contracts independent of transport code."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from ..conversation_scope import ConversationScope

SERVER_NAME = re.compile(r"[a-z][a-z0-9-]{0,19}\Z")
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
SECRET_REFERENCE = re.compile(r"GAOJI_MCP_SECRET_[A-Z0-9_]{1,64}\Z")
MAX_TOOLS = 64


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def group_scope(value: str) -> ConversationScope:
    if not isinstance(value, str) or not re.fullmatch(r"onebot-v11:group:[1-9][0-9]{0,19}", value):
        raise ValueError("MCP requires one explicit QQ group scope")
    return ConversationScope("onebot-v11", "group", value.rsplit(":", 1)[1])


def server_config(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"transport", "url", "profile", "credential_ref"}:
        raise ValueError("Unknown MCP configuration field")
    transport = value.get("transport")
    if transport == "http":
        url = value.get("url", "")
        if not isinstance(url, str) or not 1 <= len(url) <= 2048 or any(ord(c) <= 32 for c in url):
            raise ValueError("Invalid MCP endpoint")
        parsed = urlsplit(url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or value.get("profile")):
            raise ValueError("MCP endpoint cannot contain credentials, a query or a fragment")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid MCP port")
        reference = value.get("credential_ref") or ""
        if not isinstance(reference, str) or (reference and not SECRET_REFERENCE.fullmatch(reference)):
            raise ValueError("MCP credentials must use an operator-provided GAOJI_MCP_SECRET_ reference")
        return {"transport": transport, "url": url, "credential_ref": reference}
    if transport == "stdio":
        profile = value.get("profile", "")
        if not isinstance(profile, str) or not SERVER_NAME.fullmatch(profile) or value.get("url") or value.get("credential_ref"):
            raise ValueError("stdio requires an operator-defined isolated launcher profile")
        return {"transport": transport, "profile": profile}
    raise ValueError("MCP transport must be http or stdio")


def _schema(value: object) -> dict:
    if not isinstance(value, dict) or len(canonical(value).encode()) > 32768:
        raise ValueError("MCP schema must be an object of at most 32 KiB")
    nodes = 0

    def walk(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 16 or nodes > 2000:
            raise ValueError("MCP schema is too complex")
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    # Resolving arbitrary remote schemas would be an SSRF path.
                    if not isinstance(child, str) or not child.startswith("#/"):
                        raise ValueError("Only local MCP schema references are allowed")
                if key == "$id":
                    raise ValueError("MCP schemas cannot change their resolution base")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
    walk(value)
    Draft202012Validator.check_schema(value)
    return value


def tool_definition(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not TOOL_NAME.fullmatch(value["name"]):
        raise ValueError("MCP tool name must contain 1-32 ASCII identifier characters")
    description = value.get("description") or ""
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("MCP tool description is too long")
    parameters = _schema(value.get("inputSchema"))
    if parameters.get("type") != "object":
        raise ValueError("MCP tool input must be an object")
    result = {"name": value["name"], "description": description, "inputSchema": parameters}
    if value.get("outputSchema") is not None:
        result["outputSchema"] = _schema(value["outputSchema"])
    # Remote readOnlyHint/destructiveHint are not an authorization policy.
    return json.loads(canonical(result))


def tool_name(server: str, remote: str) -> str:
    if not SERVER_NAME.fullmatch(server) or not TOOL_NAME.fullmatch(remote):
        raise ValueError("Invalid namespaced MCP tool")
    return f"mcp__{server}__{remote}"
