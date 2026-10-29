"""Pure M6 adapter codec: no I/O, persistence, retries, permission or sequence allocation.

Schema follows the public QQ autogen pages captured in evidence/. These values
are protocol data, never authority; an adapter must bind them to its immutable
artifact, actual endpoint, live account/job fence and verified capability.
"""
from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import re
import unicodedata
from urllib.parse import quote, urlsplit

from src.plugins.ai_chat.platform_types import EndpointKey

CONTRACT = "qq-autogen-2026-08-10-zero-v1"
HASH_PREFIX_BYTES = 10_002_432
MAX_BASE64_BYTES = 5_000_000


class NativeProtocolError(ValueError):
    def __init__(self, reason: str, *, stage="upload", message_evidence=False):
        self.reason, self.stage = reason, stage
        self.message_evidence = message_evidence
        super().__init__(reason)


def _int(value, minimum, maximum, reason):
    if type(value) is not int or not minimum <= value <= maximum:
        raise NativeProtocolError(reason)
    return value


def _text(value, maximum, reason):
    try:
        valid = (isinstance(value, str) and bool(value) and len(value.encode()) <= maximum
                 and not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise NativeProtocolError(reason)
    return value


def _name(value):
    _text(value, 255, "invalid_file_name")
    if value in {".", ".."} or value != value.strip() or any(c in value for c in '/\\:*?"<>|') or value.endswith('.'):
        raise NativeProtocolError("invalid_file_name")
    return value


def _endpoint(endpoint):
    if (not isinstance(endpoint, EndpointKey) or endpoint.account.platform != "qq-official"
            or endpoint.kind not in {"group", "private"}):
        raise NativeProtocolError("invalid_media_endpoint")
    # Same bounded opaque identities as the adapter: preserve original text,
    # including Unicode/punctuation; do not invent an alphabet for platform IDs.
    _text(endpoint.native_conversation_id, 256, "invalid_native_target")
    _text(endpoint.account.app_id, 256, "invalid_account_id")
    _text(endpoint.account.key, 512, "invalid_account_key")
    _text(endpoint.key, 1000, "invalid_endpoint_key")
    return endpoint


def _path(endpoint, operation):
    _endpoint(endpoint)
    collection = "groups" if endpoint.kind == "group" else "users"
    native = endpoint.native_conversation_id
    # Path separators/dot-only segments are rejected at this transport boundary,
    # not rewritten into another identity. Other opaque text is UTF-8 encoded
    # exactly once as one segment; percent literals become %25, never decoded.
    if native in {".", ".."} or "/" in native or "\\" in native:
        raise NativeProtocolError("unsafe_target_route_segment")
    return f"/v2/{collection}/{quote(native, safe='')}/{operation}"


@dataclass(frozen=True)
class NativeLimits:
    # Conservative local policy, NOT a declaration of Tencent's hard limits.
    small_bytes: int = 3_500_000
    total_bytes: int = 100 * 1024 * 1024
    max_parts: int = 10_000
    max_parallel: int = 4
    max_cache_seconds: int = 86_400
    expiry_margin_seconds: int = 5

    def __post_init__(self):
        _int(self.small_bytes, 1, 5_000_000, "invalid_small_limit")
        _int(self.total_bytes, self.small_bytes, 200_000_000, "invalid_total_limit")
        _int(self.max_parts, 1, 10_000, "invalid_part_limit")
        _int(self.max_parallel, 1, 10, "invalid_parallel_limit")
        _int(self.max_cache_seconds, 1, 604_800, "invalid_cache_limit")
        _int(self.expiry_margin_seconds, 0, 60, "invalid_expiry_margin")


@dataclass(frozen=True)
class ArtifactFingerprint:
    filename: str = field(repr=False)
    file_type: int
    size: int
    sha256: str = field(repr=False)
    md5: str = field(repr=False)
    sha1: str = field(repr=False)
    md5_10m: str = field(repr=False)

    def __post_init__(self):
        _name(self.filename)
        _int(self.file_type, 1, 4, "invalid_file_type")
        _int(self.size, 1, 200_000_000, "invalid_file_size")
        for value, length in ((self.sha256, 64), (self.md5, 32), (self.sha1, 40), (self.md5_10m, 32)):
            if not isinstance(value, str) or re.fullmatch(f"[a-f0-9]{{{length}}}", value) is None:
                raise NativeProtocolError("invalid_artifact_digest")


def fingerprint(chunks: Iterable[bytes], *, filename: str, file_type: int,
                limits=NativeLimits()) -> ArtifactFingerprint:
    """One bounded streaming pass; MD5/SHA1 are wire checksums, not identity."""
    _name(filename)
    _int(file_type, 1, 4, "invalid_file_type")
    sha, md5, sha1, first = hashlib.sha256(), hashlib.md5(), hashlib.sha1(), hashlib.md5()
    size = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise NativeProtocolError("invalid_artifact_chunk")
        if size + len(chunk) > limits.total_bytes:
            raise NativeProtocolError("artifact_exceeds_local_limit")
        if size < HASH_PREFIX_BYTES:
            first.update(chunk[:HASH_PREFIX_BYTES - size])
        for hasher in (sha, md5, sha1):
            hasher.update(chunk)
        size += len(chunk)
    return ArtifactFingerprint(filename, file_type, size, sha.hexdigest(), md5.hexdigest(), sha1.hexdigest(), first.hexdigest())


@dataclass(frozen=True)
class UploadRequest:
    stage: str
    path: str = field(repr=False)
    json_text: str = field(repr=False)

    @property
    def body(self):
        return json.loads(self.json_text)


def _request(stage, endpoint, operation, body):
    return UploadRequest(stage, _path(endpoint, operation), json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def build_small_upload(endpoint, artifact, content: bytes, *, limits=NativeLimits()):
    if (not isinstance(content, bytes) or len(content) != artifact.size
            or hashlib.sha256(content).hexdigest() != artifact.sha256):
        raise NativeProtocolError("artifact_changed")
    if artifact.size > limits.small_bytes:
        raise NativeProtocolError("chunked_upload_required")
    if 4 * ((artifact.size + 2) // 3) > MAX_BASE64_BYTES:
        raise NativeProtocolError("encoded_upload_exceeds_local_limit")
    encoded = base64.b64encode(content).decode("ascii")
    if len(encoded) > MAX_BASE64_BYTES:
        raise NativeProtocolError("encoded_upload_exceeds_local_limit")
    return _request("upload_small", endpoint, "files", {
        "file_type": artifact.file_type, "file_name": artifact.filename,
        "file_data": encoded, "srv_send_msg": False,
    })


def build_prepare(endpoint, artifact, *, limits=NativeLimits()):
    if artifact.size > limits.total_bytes:
        raise NativeProtocolError("artifact_exceeds_local_limit")
    return _request("upload_prepare", endpoint, "upload_prepare", {
        "file_type": artifact.file_type, "file_name": artifact.filename, "file_size": str(artifact.size),
        "md5": artifact.md5, "sha1": artifact.sha1, "md5_10m": artifact.md5_10m,
    })


def _response(status, body, stage):
    # M0's dict-only helper currently loses HTTP status. Integration must retain
    # it: 201/202 and malformed success bodies are not upload completion proof.
    if type(status) is not int or status != 200 or not isinstance(body, Mapping):
        raise NativeProtocolError("upload_response_unconfirmed", stage=stage)
    if any(type(body[key]) is not int or body[key] != 0 for key in ("code", "err_code") if key in body):
        raise NativeProtocolError("upload_response_rejected", stage=stage)


def _decimal(value, maximum, reason):
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,9}", value) is None:
        raise NativeProtocolError(reason)
    return _int(int(value), 1, maximum, reason)


def validate_part_url(value, *, allowed_hosts: frozenset[str]):
    """Syntax/host policy only. Transport MUST pin public DNS/IP and deny redirects.

    The supplied hosts come from trusted deployment/verified provider evidence,
    never from this response's own host field. No default wildcard is guessed.
    """
    _text(value, 16384, "invalid_upload_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise NativeProtocolError("invalid_upload_url") from None
    if (parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
            or parsed.fragment or port not in {None, 443} or host not in allowed_hosts
            or "\\" in value or host == "localhost" or host.endswith((".localhost", ".local"))):
        raise NativeProtocolError("unapproved_upload_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise NativeProtocolError("literal_upload_host_forbidden")
    return value


@dataclass(frozen=True)
class UploadPart:
    index: int
    offset: int
    length: int
    presigned_url: str = field(repr=False)


@dataclass(frozen=True)
class PreparedUpload:
    endpoint: EndpointKey = field(repr=False)
    artifact: ArtifactFingerprint
    upload_id: str = field(repr=False)
    block_size: int
    parts: tuple[UploadPart, ...]
    concurrency: int
    retry_timeout_seconds: int
    retry_delay_seconds: int
    contract: str = CONTRACT


def parse_prepare(status, body, *, endpoint, artifact, allowed_hosts, limits=NativeLimits()):
    _response(status, body, "upload_prepare")
    _endpoint(endpoint)
    if artifact.size > limits.total_bytes:
        raise NativeProtocolError("artifact_exceeds_local_limit")
    upload_id = _text(body.get("upload_id"), 1024, "invalid_upload_id")
    block = _decimal(body.get("block_size"), limits.total_bytes, "invalid_block_size")
    count = (artifact.size + block - 1) // block
    parts = body.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) == count <= limits.max_parts:
        raise NativeProtocolError("invalid_upload_parts")
    indexes, parsed = set(), []
    for item in parts:
        if not isinstance(item, Mapping):
            raise NativeProtocolError("invalid_upload_part")
        index = _int(item.get("index"), 0, count - 1, "invalid_part_index")
        length = min(block, artifact.size - index * block)
        if index in indexes or _decimal(item.get("block_size"), block, "invalid_part_size") != length:
            raise NativeProtocolError("invalid_part_coverage")
        indexes.add(index)
        parsed.append(UploadPart(index, index * block, length,
            validate_part_url(item.get("presigned_url"), allowed_hosts=frozenset(allowed_hosts))))
    config = body.get("upload_config", {})
    if not isinstance(config, Mapping) or any(key in body for key in ("concurrency", "retry_timeout", "retry_delay")):
        raise NativeProtocolError("unsupported_upload_schema")
    concurrency = _int(config.get("concurrency", 1), 1, 100, "invalid_upload_concurrency")
    timeout = _int(config.get("retry_timeout", 300), 1, 86400, "invalid_upload_retry_timeout")
    delay = _int(config.get("retry_delay", 1), 1, 3600, "invalid_upload_retry_delay")
    return PreparedUpload(endpoint, artifact, upload_id, block, tuple(sorted(parsed, key=lambda p: p.index)),
        min(concurrency, limits.max_parallel), min(timeout, 600), min(delay, 60))


def build_part_finish(prepared, part_index, content: bytes):
    _int(part_index, 0, len(prepared.parts) - 1, "invalid_part_index")
    part = prepared.parts[part_index]
    if not isinstance(content, bytes) or len(content) != part.length:
        raise NativeProtocolError("part_size_changed")
    # Caller must read immutable hash-verified storage; length alone cannot
    # prove a chunk belongs to its artifact. Persist each intended part digest.
    return _request("upload_part_finish", prepared.endpoint, "upload_part_finish", {
        "upload_id": prepared.upload_id, "part_index": part.index,
        "block_size": str(part.length), "md5": hashlib.md5(content).hexdigest(),
    })


def confirm_part_finish(status, body):
    _response(status, body, "upload_part_finish")
    return True


def build_complete(prepared):
    # The route is /files, not /complete_upload. Completion requires every
    # persisted part-finish confirmation; the pure codec cannot certify that.
    return _request("upload_complete", prepared.endpoint, "files", {
        "upload_id": prepared.upload_id, "file_type": prepared.artifact.file_type,
        "file_name": prepared.artifact.filename, "srv_send_msg": False,
    })


@dataclass(frozen=True)
class UploadedMedia:
    endpoint: EndpointKey = field(repr=False)
    artifact: ArtifactFingerprint
    file_info: str = field(repr=False)
    file_uuid: str = field(repr=False)
    received_at: int
    remote_ttl_seconds: int
    reusable_until: int
    contract: str = CONTRACT

    @property
    def cache_key(self):
        # Filename is part of media metadata: identical bytes with a different
        # requested name must not reuse the wrong remote filename.
        identity = [self.endpoint.account.key, self.endpoint.key, self.artifact.sha256,
                    self.artifact.file_type, self.artifact.filename, self.contract]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def parse_upload_receipt(status, body, *, endpoint, artifact, now, limits=NativeLimits()):
    _response(status, body, "upload_complete")
    _endpoint(endpoint)
    _int(now, 0, 2**63 - 604801, "invalid_upload_time")
    if body.get("id") is not None and body.get("id") != "":
        # An id contradicts our forced srv_send_msg=false contract. Never
        # blindly add a separate M3 send after possible implicit delivery.
        raise NativeProtocolError("unexpected_upload_message_evidence", message_evidence=True)
    info = _text(body.get("file_info"), 65536, "invalid_file_info")
    uuid = _text(body.get("file_uuid"), 1024, "invalid_file_uuid")
    ttl = _int(body.get("ttl"), 0, 2**31 - 1, "invalid_file_info_ttl")
    local_ttl = min(ttl if ttl else limits.max_cache_seconds, limits.max_cache_seconds)
    expiry = now + max(0, local_ttl - limits.expiry_margin_seconds)
    # raw_url is deliberately discarded: it is not our authorized download
    # route, and file_type4 does not receive it under the current contract.
    return UploadedMedia(endpoint, artifact, info, uuid, now, ttl, expiry)


def build_media_payload(media, *, endpoint, now, msg_id=None, event_id=None, msg_seq=None, content=""):
    if type(now) is not int or not 0 <= now <= 2**63 - 1:
        raise NativeProtocolError("invalid_media_time", stage="send")
    if endpoint != media.endpoint or now >= media.reusable_until:
        raise NativeProtocolError("media_target_or_expiry_changed", stage="send")
    if msg_id is not None and event_id is not None:
        raise NativeProtocolError("conflicting_reply_credentials", stage="send")
    if msg_id is not None or event_id is not None:
        _text(msg_id if msg_id is not None else event_id, 256, "invalid_reply_credential")
        _int(msg_seq, 0, 65535, "invalid_msg_seq")
    elif msg_seq is not None:
        _int(msg_seq, 0, 65535, "invalid_msg_seq")
    try:
        valid_caption = (isinstance(content, str) and len(content) <= 2000 and len(content.encode()) <= 8000
            and not any(unicodedata.category(c) in {"Cf", "Cs"} or c == "\x00" for c in content)
            and "<qqbot-" not in content.casefold() and "<@" not in content)
    except UnicodeError:
        valid_caption = False
    if not valid_caption:
        raise NativeProtocolError("invalid_media_caption", stage="send")
    body = {"msg_type": 7, "media": {"file_info": media.file_info}}
    for key, value in (("msg_id", msg_id), ("event_id", event_id), ("msg_seq", msg_seq)):
        if value is not None:
            body[key] = value
    if content:
        body["content"] = content
    return body
