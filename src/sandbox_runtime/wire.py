"""Bounded JSON framing shared by private runtime processes."""

import asyncio
import json
import struct

from .errors import SandboxError

HEADER_LIMIT = 1024 * 1024


class ProtocolError(SandboxError):
    pass


def encoded(value: dict) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid runtime JSON") from None


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(value)


async def read_header(reader, *, limit=HEADER_LIMIT, timeout=15) -> dict:
    async def receive():
        size = struct.unpack("!I", await reader.readexactly(4))[0]
        if not 0 < size <= limit:
            raise ProtocolError("Runtime frame exceeds its size limit")
        try:
            value = json.loads(await reader.readexactly(size),
                               object_pairs_hook=_unique_pairs,
                               parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("Invalid runtime JSON") from None
        if not isinstance(value, dict):
            raise ProtocolError("Runtime frame must be an object")
        return value
    return await asyncio.wait_for(receive(), timeout)


async def write_frame(writer, header: dict, payload=b"", *, timeout=30):
    if header.get("payload_size", 0) != len(payload):
        raise ProtocolError("Runtime payload size mismatch")
    body = encoded(header)
    if len(body) > HEADER_LIMIT:
        raise ProtocolError("Runtime response exceeds its size limit")

    async def send():
        writer.write(struct.pack("!I", len(body)) + body)
        await writer.drain()
        for offset in range(0, len(payload), 65536):
            writer.write(payload[offset:offset + 65536])
            await writer.drain()
    await asyncio.wait_for(send(), timeout)
