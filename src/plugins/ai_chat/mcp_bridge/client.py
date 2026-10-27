"""One-shot official-SDK calls; policy and durable ownership stay with the host."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json

from mcp import Client
from mcp_types import CallToolRequest, CallToolRequestParams, CallToolResult, InputRequiredResult
from pydantic import TypeAdapter

from ..agent.evidence import redact
from ..prompt_context import quote_evidence
from .contracts import MAX_TOOLS, canonical, fingerprint, server_config, tool_definition

_CALL_RESULT = TypeAdapter(CallToolResult | InputRequiredResult)
MAX_CATALOG_BYTES = 512 * 1024
MAX_ARGUMENT_BYTES = 32768
MAX_RESULT_CHARS = 24000


class McpCallError(RuntimeError):
    def __init__(self, code: str, *, may_have_run: bool = False):
        super().__init__(code)
        self.code, self.may_have_run = code, may_have_run


def _local_error(error: Exception) -> McpCallError | None:
    # AnyIO wraps exceptions leaving a transport task group. Unwrap only our own
    # single local failure; remote/network exceptions remain generic and redacted.
    while isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error if isinstance(error, McpCallError) else None


def _secret_forms(secret: str) -> set[str]:
    values = set()
    if secret:
        for _ in range(4):
            values.add(secret)
            secret = json.dumps(secret, ensure_ascii=False)[1:-1]
    return values


async def list_catalog(client: Client, *, secret: str = "") -> list[dict]:
    cursor, seen_cursors, names, result, size = None, set(), set(), [], 0
    for _ in range(8):
        page = await client.list_tools(cursor=cursor, cache_mode="refresh")
        for raw in page.tools:
            definition = tool_definition(raw.model_dump(by_alias=True, mode="json", exclude_none=True))
            encoded = canonical(definition)
            if any(value in encoded for value in _secret_forms(secret)):
                raise McpCallError("catalog_contains_credential")
            size += len(encoded.encode())
            if definition["name"] in names or len(result) >= MAX_TOOLS or size > MAX_CATALOG_BYTES:
                raise McpCallError("catalog_limit_or_duplicate")
            names.add(definition["name"])
            result.append(definition)
        cursor = page.next_cursor
        if cursor is None:
            return result
        if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen_cursors:
            raise McpCallError("invalid_catalog_cursor")
        seen_cursors.add(cursor)
    raise McpCallError("catalog_page_limit")


def project_result(result: CallToolResult, *, secret: str = "") -> dict:
    # Binary/resource payloads are never auto-downloaded or imported into the host.
    text = [item.text for item in result.content if item.type == "text"]
    value = {"text": text, "structured": result.structured_content}
    secret_forms = sorted(_secret_forms(secret), key=len, reverse=True)
    def scrub(item):
        if isinstance(item, str):
            for form in secret_forms:
                item = item.replace(form, "[REDACTED]")
            return item
        if isinstance(item, dict):
            return {scrub(key): scrub(child) for key, child in item.items()}
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item
    encoded = canonical(redact(scrub(value)))
    return {"ok": not result.is_error, "source": "external_mcp", "untrusted": True,
        "content": quote_evidence(encoded, "external_mcp_tool", max_chars=MAX_RESULT_CHARS),
        "truncated": len(encoded) > MAX_RESULT_CHARS,
        "omitted_attachments": sum(item.type != "text" for item in result.content)}


class McpWireClient:
    def __init__(self, sessions, *, timeout_seconds: float = 60, max_concurrent: int = 4):
        if not 1 <= timeout_seconds <= 120 or not 1 <= max_concurrent <= 16:
            raise ValueError("Invalid MCP client limits")
        self.sessions, self.timeout = sessions, timeout_seconds
        self._capacity = asyncio.Semaphore(max_concurrent)

    async def discover(self, config: dict) -> list[dict]:
        config = server_config(config)
        try:
            async with asyncio.timeout(self.timeout), self._capacity:
                async with Client(self.sessions.connect(config), cache=None, read_timeout_seconds=self.timeout) as client:
                    return await list_catalog(client, secret=self.sessions.credential(config))
        except asyncio.CancelledError:
            raise
        except McpCallError:
            raise
        except Exception as exc:
            local = _local_error(exc)
            raise McpCallError(local.code if local else "mcp_discovery_failed") from None

    async def call(self, config: dict, definition: dict, arguments: dict, *,
                   before_call: Callable[[], Awaitable[None]]) -> dict:
        config, definition = server_config(config), tool_definition(definition)
        if not isinstance(arguments, dict) or len(canonical(arguments).encode()) > MAX_ARGUMENT_BYTES:
            raise McpCallError("invalid_tool_arguments")
        # Freeze caller-owned arguments before any await/approval check.
        arguments = json.loads(canonical(arguments))
        may_have_run = False
        try:
            async with asyncio.timeout(self.timeout), self._capacity:
                async with Client(self.sessions.connect(config), cache=None, read_timeout_seconds=self.timeout) as client:
                    catalog = await list_catalog(client, secret=self.sessions.credential(config))
                    current = next((item for item in catalog if item["name"] == definition["name"]), None)
                    if current is None or fingerprint(current) != fingerprint(definition):
                        raise McpCallError("tool_definition_changed")
                    # The host rechecks policy, group, approval and ownership and records dispatch here.
                    await before_call()
                    may_have_run = True
                    # Use the public one-round SDK API. Client.call_tool would re-list/retry on
                    # HEADER_MISMATCH and could run an unreviewed schema or start input rounds.
                    # Do not execute remote outputSchema regexes on the bot's event loop.
                    result = await client.session.send_request(
                        CallToolRequest(params=CallToolRequestParams(name=definition["name"], arguments=arguments)),
                        _CALL_RESULT, request_read_timeout_seconds=self.timeout)
                    if isinstance(result, InputRequiredResult) or result.result_type != "complete":
                        raise McpCallError("interactive_result_not_supported", may_have_run=True)
                    return project_result(result, secret=self.sessions.credential(config))
        except asyncio.CancelledError:
            # Durable dispatch remains unresolved; cancellation must never trigger replay.
            raise
        except McpCallError as exc:
            raise McpCallError(exc.code, may_have_run=may_have_run or exc.may_have_run) from None
        except Exception as exc:
            local = _local_error(exc)
            raise McpCallError(local.code if local else "mcp_call_unconfirmed" if may_have_run else "mcp_unavailable",
                               may_have_run=may_have_run or bool(local and local.may_have_run)) from None
