"""Host-owned, single-stage upload adapter. No queue, schema or message send.

Every stage is independently durable in the caller's existing job/manifest store.
Cancellation propagates: a caller must reconcile its already-started attempt as
unknown, never replay automatically. Bytes/handles/URLs never enter diagnostics.
"""
from __future__ import annotations

from .project_authority import RecipientEvidenceUnavailable
from ...agent.control import LeaseLost

import asyncio
import hashlib
import json
import re
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from src.plugins.ai_chat.platform_types import EndpointKey
from src.qq_official.errors import QQAPIError
from src.qq_official.http import scoped_request_guard

from .native_media import (
    ArtifactFingerprint, NativeLimits, NativeProtocolError, PreparedUpload,
    UploadPart, UploadRequest, _endpoint, build_small_upload,
    build_prepare, build_part_finish, build_complete, parse_prepare,
    parse_upload_receipt, confirm_part_finish, validate_part_url,
)

_ID = re.compile(r"[a-f0-9]{32}\Z")


class UploadGuardError(PermissionError):
    def __init__(self):
        super().__init__("upload_authority_unavailable")


@dataclass(frozen=True, repr=False)
class UploadIntent:
    """Manifest description, not authority. Host closures hold exact job lease."""
    artifact_id: str
    endpoint: EndpointKey
    artifact: ArtifactFingerprint
    deadline_at: int

    def __post_init__(self):
        if (not isinstance(self.artifact_id, str) or not _ID.fullmatch(self.artifact_id)
                or not isinstance(self.artifact, ArtifactFingerprint)
                or type(self.deadline_at) is not int or not 0 <= self.deadline_at <= 2**63 - 1):
            raise NativeProtocolError("invalid_upload_intent")
        _endpoint(self.endpoint)

    def __repr__(self):
        return "UploadIntent(<protected>)"


@dataclass(frozen=True, repr=False)
class StageRequest:
    intent: UploadIntent
    operation: str
    request_commitment: str
    part_index: int | None = None

    def __repr__(self):
        return f"StageRequest(operation={self.operation!r}, <protected>)"


@dataclass(frozen=True)
class _PermitProof:
    request: StageRequest = field(repr=False)
    stage_attempt_id: str = field(repr=False)
    check_current: Callable[[], None] = field(repr=False)


_permits = weakref.WeakKeyDictionary()


class UploadPermit:
    """Locally issued, once-only stage proof; constructing one issues nothing."""
    __slots__ = ("__weakref__",)

    def __repr__(self):
        return "UploadPermit(<protected>)"


def issue_upload_permit(request: StageRequest, *, stage_attempt_id: str,
                        check_current: Callable[[], None]) -> UploadPermit:
    """Host-only: call after atomic source+job+stage-attempt commit.

    This function does not grant user authority. The trusted host must bind its
    exact current job lease, artifact source, route and capability in the checks.
    It must refuse to issue another attempt for unresolved/confirmed stages.
    """
    if (not isinstance(request, StageRequest) or not isinstance(stage_attempt_id, str)
            or not _ID.fullmatch(stage_attempt_id) or not callable(check_current)):
        raise UploadGuardError()
    permit = UploadPermit()
    _permits[permit] = _PermitProof(request, stage_attempt_id, check_current)
    return permit


def _proof(permit, request):
    proof = _permits.get(permit) if isinstance(permit, UploadPermit) else None
    if proof is None or proof.request != request:
        raise UploadGuardError()
    return proof


def _consume(permit, request):
    proof = _proof(permit, request)
    del _permits[permit]
    proof.check_current()


@dataclass(frozen=True)
class StageResult:
    operation: str
    disposition: str  # confirmed / confirmed_rejected / unknown / proven_unsent
    stage_attempt_id: str | None = field(default=None, repr=False)
    request_commitment: str = field(default="", repr=False)
    category: str = ""
    http_status: int | None = None
    code: int | None = None
    message_evidence: bool = False
    value: object = field(default=None, repr=False)


class UploadSourceLost(LeaseLost):
    """A control interruption with bounded historical evidence, not a retry.

    Only the uploader knows whether its physical handoff occurred. The caller
    may persist that fact, then must retain LeaseLost control flow. Cancellation
    is deliberately not transformed by this exception.
    """
    def __init__(self, stage: StageRequest, result: StageResult):
        super().__init__('upload_source_unavailable')
        self.stage, self.result = stage, result


@dataclass(frozen=True)
class PutResponse:
    # A provider PUT response proves only this part, never finish/merge/message.
    status: int


class PartTransport(Protocol):
    async def put(self, part: UploadPart, content: bytes, *, timeout: float,
                  before_dispatch: Callable[[], None], ensure_current: Callable[[], Awaitable[None]]) -> PutResponse:
        """Host-selected transport: no QQ auth/cookies/proxies/redirects.

        Must resolve+pin approved public addresses with TLS verification and
        await ensure_current after DNS/other preparation, then consume
        before_dispatch immediately before its single physical request.
        No automatic retry. PinnedPartTransport is the production implementation.
        """
        ...


SourceCheck = Callable[[UploadIntent, str], Awaitable[None]]
BeginStage = Callable[[StageRequest], Awaitable[UploadPermit]]


class OfficialMediaUploader:
    def __init__(self, rest, *, ensure_current: SourceCheck, before_network: BeginStage,
                 clock=lambda: int(time.time()), limits=NativeLimits(),
                 allowed_part_hosts: frozenset[str] = frozenset(),
                 part_transport: PartTransport | None = None):
        if not callable(ensure_current) or not callable(before_network):
            raise UploadGuardError()
        self.rest = rest
        self.ensure_current, self.before_network = ensure_current, before_network
        self.clock, self.limits = clock, limits
        self.allowed_part_hosts = frozenset(allowed_part_hosts)
        self.part_transport = part_transport

    def _now(self):
        now = self.clock()
        if type(now) is not int or not 0 <= now <= 2**63 - 1:
            raise UploadGuardError()
        return now

    def _check_intent(self, intent):
        if not isinstance(intent, UploadIntent):
            raise UploadGuardError()
        if intent.artifact.size > self.limits.total_bytes or self._now() >= intent.deadline_at:
            raise UploadGuardError()
        credentials = self.rest.token_manager.credentials
        parsed = urlsplit(self.rest.base_url)
        host = parsed.hostname
        if self.rest.allow_test_server and (parsed.scheme != "http" or host not in {"127.0.0.1", "::1"}
                                           or not credentials.is_test):
            raise UploadGuardError()
        environment = ("test" if self.rest.allow_test_server else
                       "sandbox" if host == "sandbox.api.sgroup.qq.com" else "production")
        if (intent.endpoint.account.app_id != credentials.app_id
                or intent.endpoint.account.environment != environment):
            raise UploadGuardError()

    def _stage(self, intent, operation, payload, part_index=None):
        # Full namespace and immutable bytes included even if wire metadata omits
        # them. This is a commitment, never an upload-ID-derived permission.
        material = [intent.artifact_id, intent.endpoint.key, intent.artifact.sha256,
                    intent.artifact.size, intent.artifact.filename, intent.artifact.file_type,
                    intent.deadline_at, operation, part_index, payload]
        digest = hashlib.sha256(json.dumps(material, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return StageRequest(intent, operation, digest, part_index)

    async def _execute(self, intent, request: UploadRequest, parse, *, part_index=None):
        operation = request.stage
        stage = self._stage(intent, operation, [request.path, request.body], part_index)
        permit, attempt_id, started, observed_status = None, None, False, None
        def final_check():
            nonlocal started
            self._check_intent(intent)
            _consume(permit, stage)
            started = True
        async def guard(physical_stage):
            self._check_intent(intent)
            await self.ensure_current(intent, "authentication" if physical_stage == "token" else operation)
            self._check_intent(intent)
        try:
            self._check_intent(intent)
            # Even a cached-token path must first prove current source authority.
            await guard("token")
            with scoped_request_guard(guard):
                prepared = await self.rest.prepare_upload(request.path)
                self._check_intent(intent)
                permit = await self.before_network(stage)
                attempt_id = _proof(permit, stage).stage_attempt_id
                self._check_intent(intent)
                async with asyncio.timeout(intent.deadline_at - self._now()):
                    response = await self.rest.upload(request.path, request.body,
                        prepared=prepared, before_dispatch=final_check)
            observed_status = response.status
            value = parse(response.status, response.body)
            return StageResult(operation, "confirmed", attempt_id, stage.request_commitment,
                               http_status=response.status, value=value)
        except RecipientEvidenceUnavailable:
            # Only this invocation's pre-dispatch flag proves no physical upload.
            # If a future transport invokes authority after dispatch, retain unknown.
            return StageResult(operation, "unknown" if started else "proven_unsent", attempt_id,
                stage.request_commitment, "recipient_evidence_unavailable")
        except QQAPIError as error:
            if not started or error.category == "connection_failed":
                disposition = "proven_unsent"
            elif error.confirmed_not_accepted and error.status not in {201, 202} and (error.status or 0) < 500:
                disposition = "confirmed_rejected"
            else:
                disposition = "unknown"
            return StageResult(operation, disposition, attempt_id, stage.request_commitment,
                error.category, error.status, error.code,
                message_evidence=error.category == "unexpected_upload_message_evidence")
        except NativeProtocolError as error:
            return StageResult(operation, "unknown" if started else "proven_unsent", attempt_id,
                stage.request_commitment, error.reason, http_status=observed_status,
                message_evidence=error.message_evidence)
        except Exception:
            # Includes injected source/store/transport exceptions. No error text,
            # URL, bytes, credential or traceback is part of public diagnostics.
            return StageResult(operation, "unknown" if started else "proven_unsent", attempt_id,
                stage.request_commitment, "upload_unavailable")
        finally:
            # Unused permits never survive this invocation (including cancel).
            if isinstance(permit, UploadPermit):
                _permits.pop(permit, None)

    async def upload_small(self, intent, content: bytes):
        request = build_small_upload(intent.endpoint, intent.artifact, content, limits=self.limits)
        return await self._execute(intent, request, lambda status, body: parse_upload_receipt(
            status, body, endpoint=intent.endpoint, artifact=intent.artifact,
            now=self._now(), limits=self.limits))

    async def prepare(self, intent):
        # Do not allocate a server session unless a controlled PUT path exists.
        if self.part_transport is None or not self.allowed_part_hosts:
            return StageResult("upload_prepare", "proven_unsent", category="part_transport_unavailable")
        request = build_prepare(intent.endpoint, intent.artifact, limits=self.limits)
        return await self._execute(intent, request, lambda status, body: parse_prepare(
            status, body, endpoint=intent.endpoint, artifact=intent.artifact,
            allowed_hosts=self.allowed_part_hosts, limits=self.limits))

    def _prepared(self, intent, prepared):
        if (not isinstance(prepared, PreparedUpload) or prepared.endpoint != intent.endpoint
                or prepared.artifact != intent.artifact):
            raise NativeProtocolError("upload_session_binding_changed")
        # The root must additionally verify protected session provenance and its
        # exact prepare attempt. Reparse durable data before passing it here.
        return prepared

    async def finish_part(self, intent, prepared, part_index, content: bytes):
        prepared = self._prepared(intent, prepared)
        request = build_part_finish(prepared, part_index, content)
        return await self._execute(intent, request, confirm_part_finish, part_index=part_index)

    async def complete(self, intent, prepared):
        prepared = self._prepared(intent, prepared)
        # before_network must prove every exact part-finish confirmed in DB.
        request = build_complete(prepared)
        return await self._execute(intent, request, lambda status, body: parse_upload_receipt(
            status, body, endpoint=intent.endpoint, artifact=intent.artifact,
            now=self._now(), limits=self.limits))

    async def put_part(self, intent, prepared, part_index, content: bytes):
        prepared = self._prepared(intent, prepared)
        build_part_finish(prepared, part_index, content)  # index/size validation
        part = prepared.parts[part_index]
        validate_part_url(part.presigned_url, allowed_hosts=self.allowed_part_hosts)
        stage = self._stage(intent, "upload_put", [prepared.upload_id, part.presigned_url,
            part.offset, part.length, hashlib.sha256(content).hexdigest()], part_index)
        permit, attempt_id, started = None, None, False
        def final_check():
            nonlocal started
            self._check_intent(intent)
            _consume(permit, stage)
            started = True
        try:
            self._check_intent(intent)
            if self.part_transport is None:
                raise UploadGuardError()
            await self.ensure_current(intent, "upload_put")
            permit = await self.before_network(stage)
            attempt_id = _proof(permit, stage).stage_attempt_id
            await self.ensure_current(intent, "upload_put")
            self._check_intent(intent)
            timeout = min(30, intent.deadline_at - self._now())
            async with asyncio.timeout(timeout):
                response = await self.part_transport.put(part, content, timeout=timeout,
                    before_dispatch=final_check, ensure_current=lambda:self.ensure_current(intent,"upload_put"))
            if not started or not isinstance(response, PutResponse) or type(response.status) is not int:
                raise NativeProtocolError("invalid_part_transport_evidence")
            return StageResult("upload_put", "confirmed" if response.status == 200 else "unknown",
                attempt_id, stage.request_commitment, http_status=response.status)
        except LeaseLost:
            raise UploadSourceLost(stage, StageResult("upload_put", "unknown" if started else "proven_unsent",
                attempt_id, stage.request_commitment, "upload_source_unavailable")) from None
        except RecipientEvidenceUnavailable:
            return StageResult("upload_put", "unknown" if started else "proven_unsent", attempt_id,
                stage.request_commitment, "recipient_evidence_unavailable")
        except Exception:
            return StageResult("upload_put", "unknown" if started else "proven_unsent", attempt_id,
                stage.request_commitment, "part_upload_unavailable")
        finally:
            if isinstance(permit, UploadPermit):
                _permits.pop(permit, None)
