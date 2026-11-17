"""Bounded VM HTTP routes: say, send-file, action status, and read-only host tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from pathlib import PurePosixPath
import re
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from ...application.owned_sandbox import safe_filename, workspace_path
from ...background_tools import BackgroundPlatform, workspace_owner
from ...message_ir import MediaNode, MessageBody, TextNode, body_to_json
from ...platform_types import ActorKey, EndpointKey
from ...tools.history import HANDLERS as HISTORY_HANDLERS
from ...tools.sources import HANDLERS as SOURCE_HANDLERS
from ...tools.state import ToolContext, TurnState
from .file_upload import FileUploadError
from .identity_store import snapshot
from .image_formats import artifact_filename
from .lease import assert_epoch
from .plans import enqueue_plan
from .renderer import render_plan
from .send_types import OutboxInvariantError, digest, stable_json
from .vm_api import VMDenied

READ_TIMEOUT = 60
EXPORT_SECONDS = 60
ACTION_TTL = 300
REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
ROUTES = {"/vm/history": "history", "/vm/web-search": "web_search", "/vm/web-fetch": "web_fetch"}
_READ_HANDLERS = {
    handler.definition["function"]["name"]: handler
    for handler in (*HISTORY_HANDLERS, *SOURCE_HANDLERS)
    if handler.definition["function"]["name"] in set(ROUTES.values())
}


def vm_plan_idem(account_key: str, endpoint_key: str, request_id: str) -> str:
    return "qqp1:" + digest(stable_json(["qq-plan-v1", account_key, endpoint_key, "vm:" + request_id]))


def vm_delivery_denial(cursor, *, token, delivery, admission) -> str | None:
    plan = cursor.execute("SELECT * FROM delivery_plans WHERE plan_id=?", (delivery["plan_id"],)).fetchone()
    if plan is None or digest(plan["manifest_json"]) != plan["manifest_digest"]:
        return "vm_source_unavailable"
    manifest = json.loads(plan["manifest_json"])
    meta = manifest.get("vm_request")
    if not isinstance(meta, dict):
        return "vm_source_unavailable"
    endpoint = EndpointKey.parse(delivery["endpoint_key"])
    author = snapshot(cursor, endpoint.account)
    if author is None or delivery["requester_actor_key"] != author.bot_actor.key:
        return "vm_source_unavailable"
    session = cursor.execute(
        """SELECT 1 FROM qq_official_vm_sessions
        WHERE endpoint_key=? AND sandbox_id=? AND token_generation=? AND revoked_at IS NULL""",
        (endpoint.key, meta.get("sandbox_id"), meta.get("token_generation")),
    ).fetchone()
    if session is None:
        return "vm_token_revoked"
    if endpoint.kind == "private":
        peer = ActorKey(endpoint.account, "c2c_user", "", endpoint.native_conversation_id)
        if not callable(admission) or not admission(cursor, endpoint, peer):
            return "vm_endpoint_unavailable"
    return None


def _status_payload(cursor, plan) -> dict[str, object]:
    meta = json.loads(plan["manifest_json"])["vm_request"]
    delivery = cursor.execute(
        "SELECT status, wait_reason, last_error FROM deliveries WHERE plan_id=? ORDER BY part_index LIMIT 1",
        (plan["plan_id"],),
    ).fetchone()
    raw_status = delivery["status"] if delivery is not None else "failed"
    state = {
        "committed": "completed",
        "pending": "queued",
        "sending": "sending",
        "failed": "failed",
        "cancelled": "cancelled",
        "ambiguous": "ambiguous",
    }.get(raw_status, "queued")
    error_code = ""
    if state in {"failed", "cancelled", "ambiguous"} and delivery is not None:
        error_code = delivery["wait_reason"] or delivery["last_error"] or state
    return {
        "action_id": plan["plan_id"],
        "request_id": meta["request_id"],
        "kind": meta["kind"],
        "state": state,
        "error_code": error_code,
    }


def _http_code(state: str) -> int:
    return {
        "completed": 200,
        "pending": 202,
        "queued": 202,
        "sending": 202,
        "ambiguous": 409,
        "cancelled": 409,
        "failed": 502,
    }.get(state, 202)


class VMReads:
    def __init__(self, app):
        self.app, self.active = app, {}

    async def handle(self, request):
        scope = request.state.channel
        endpoint = scope.identity.endpoint
        count = self.active.get(endpoint.key, 0)
        if count >= 4 or sum(self.active.values()) >= 16:
            return JSONResponse({"error": "program_reads_busy"}, 429)
        self.active[endpoint.key] = count + 1
        try:
            body = bytearray()
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 16384:
                        return JSONResponse({"error": "program_request_too_large"}, 413)
                    body.extend(chunk)
            arguments = json.loads(body)
            if type(arguments) is not dict:
                raise ValueError
            capabilities = {"context"}
            if self.app.settings.search_enabled:
                capabilities.add("web")
            context = ToolContext(
                SimpleNamespace(context=self.app),
                platform=BackgroundPlatform(endpoint),
                capabilities=frozenset(capabilities),
            )
            name = ROUTES[request.url.path]
            handler = _READ_HANDLERS.get(name)
            if handler is None or not handler.available(context):
                return JSONResponse({"error": "program_read_disabled"}, 403)
            schema = handler.definition["function"]["parameters"]
            if request.query_params or not Draft202012Validator(schema).is_valid(arguments):
                return JSONResponse({"error": "invalid_program_arguments"}, 400)
            await scope.current()
            async with asyncio.timeout(READ_TIMEOUT):
                result = await self.execute(request, handler, TurnState(context), arguments)
            await scope.current()
            parsed = json.loads(result)
            return JSONResponse(parsed, 200 if parsed.get("ok") else (400 if name == "history" else 502))
        except ClientDisconnect:
            return JSONResponse({"error": "program_client_disconnected"}, 499)
        except PermissionError:
            return JSONResponse({"error": "vm_source_revoked"}, 403)
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_program_request"}, 400)
        except TimeoutError:
            return JSONResponse({"error": "program_read_timeout"}, 408)
        except Exception as error:
            self.app.logger.warning("VM read failed error=%s", type(error).__name__)
            return JSONResponse({"error": "program_read_unavailable"}, 503)
        finally:
            remaining = self.active[endpoint.key] - 1
            if remaining:
                self.active[endpoint.key] = remaining
            else:
                self.active.pop(endpoint.key)

    async def execute(self, request, handler, turn, arguments):
        async def disconnected():
            while (await request.receive())["type"] != "http.disconnect":
                pass

        from .billing import scoped

        with scoped(self.app, request.state.channel.identity.endpoint, "semantic_index", current=request.state.channel.current):
            work = asyncio.create_task(handler.implementation(turn, arguments))
        closed = asyncio.create_task(disconnected())
        try:
            await asyncio.wait((work, closed), return_when=asyncio.FIRST_COMPLETED)
            if closed.done():
                closed.result()
                raise ClientDisconnect()
            return work.result()
        finally:
            for task in (work, closed):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(work, closed, return_exceptions=True)


class VMHTTP:
    def __init__(self, lifecycle, runtime, app, logger):
        self.lifecycle, self.sessions, self.manager = lifecycle, lifecycle.store, lifecycle.manager
        self.runtime, self.calls, self.logger = runtime, lifecycle.calls, logger
        self.reads = VMReads(app)
        self.active_exports: dict[str, int] = {}
        self.inflight: set[tuple[str, str]] = set()

    def _validate_request_id(self, request_id) -> str:
        if type(request_id) is not str or REQUEST.fullmatch(request_id) is None:
            raise ValueError("invalid_request_id")
        return request_id

    def get_action(self, current, identifier: str, *, by_request: bool = False) -> dict[str, object]:
        with self.sessions.transaction() as cursor:
            assert_epoch(cursor, self.sessions.lease)
            self.sessions._route(cursor, current.endpoint)
            if by_request:
                self._validate_request_id(identifier)
                idem = vm_plan_idem(self.sessions.lease.account_key, current.endpoint.key, identifier)
                plan = cursor.execute(
                    "SELECT * FROM delivery_plans WHERE idempotency_key=? AND endpoint_key=?",
                    (idem, current.endpoint.key),
                ).fetchone()
            else:
                if type(identifier) is not str or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
                    raise VMDenied("vm_action_not_found", 404)
                plan = cursor.execute(
                    "SELECT * FROM delivery_plans WHERE plan_id=? AND account_key=? AND endpoint_key=?",
                    (identifier, self.sessions.lease.account_key, current.endpoint.key),
                ).fetchone()
            if plan is None or "vm_request" not in json.loads(plan["manifest_json"]):
                raise VMDenied("vm_action_not_found", 404)
            return _status_payload(cursor, plan)

    def _existing_or_conflict(self, current, request_id: str, expected: dict[str, object]) -> dict[str, object] | None:
        with self.sessions.transaction() as cursor:
            assert_epoch(cursor, self.sessions.lease)
            idem = vm_plan_idem(self.sessions.lease.account_key, current.endpoint.key, request_id)
            plan = cursor.execute(
                "SELECT * FROM delivery_plans WHERE idempotency_key=? AND endpoint_key=?",
                (idem, current.endpoint.key),
            ).fetchone()
            if plan is None:
                return None
            stored = json.loads(plan["manifest_json"]).get("vm_request") or {}
            if {k: stored.get(k) for k in expected} != expected:
                raise VMDenied("vm_request_conflict", 409)
            return _status_payload(cursor, plan)

    def _enqueue_say(self, current, *, generation: int, request_id: str, text: str):
        if type(text) is not str or not text.strip() or len(text) > 1800:
            raise ValueError("invalid_vm_text")
        rendered = render_plan(MessageBody((TextNode(0, text),)), current.endpoint)
        if rendered.status != "ready" or len(rendered.parts) != 1:
            raise ValueError("invalid_vm_text")
        meta = {
            "kind": "say",
            "request_id": request_id,
            "text": text,
            "sandbox_id": current.sandbox_id,
            "token_generation": current.token_generation,
        }
        with self.sessions.transaction() as cursor:
            self.sessions.check(cursor, current)
            now = int(self.sessions.clock())
            author = snapshot(cursor, current.endpoint.account)
            if author is None:
                raise VMDenied("vm_identity_unavailable", 503)
            ref = enqueue_plan(
                cursor,
                token=self.sessions.lease,
                rendered=rendered,
                requester=author.bot_actor,
                logical_key="vm:" + request_id,
                source_scope_key=current.scope_key,
                expires_at=now + ACTION_TTL,
                now=now,
                purpose="unsolicited",
                admission=self.sessions.private_admission,
                source_generation=generation,
                vm_request=meta,
            )
            return ref.plan_id, ref.delivery_ids[0]

    def _enqueue_file(self, current, *, generation: int, request_id: str, path: str, filename: str, content: bytes, uploaded):
        meta = {
            "kind": "send_file",
            "request_id": request_id,
            "path": path,
            "filename": filename,
            "sandbox_id": current.sandbox_id,
            "token_generation": current.token_generation,
        }
        rendered = render_plan(MessageBody((TextNode(0, "文件：" + filename),)), current.endpoint)
        mime = "audio/silk" if filename.lower().endswith(".silk") else (mimetypes.guess_type(filename)[0] or "application/octet-stream")
        kind = "audio" if mime == "audio/silk" else "file"
        sha = hashlib.sha256(content).hexdigest()
        with self.sessions.transaction() as cursor:
            self.sessions.check(cursor, current)
            now = int(self.sessions.clock())
            author = snapshot(cursor, current.endpoint.account)
            if author is None:
                raise VMDenied("vm_identity_unavailable", 503)
            ref = enqueue_plan(
                cursor,
                token=self.sessions.lease,
                rendered=rendered,
                requester=author.bot_actor,
                logical_key="vm:" + request_id,
                source_scope_key=current.scope_key,
                expires_at=min(now + ACTION_TTL, uploaded.expires_at),
                now=now,
                purpose="unsolicited",
                admission=self.sessions.private_admission,
                source_generation=generation,
                vm_request=meta,
            )
            if ref.created:
                manifest = stable_json({
                    "v": 1,
                    "media": {"file_info": uploaded.file_info},
                    "vm_request": meta,
                    "source_native_message_id": "",
                })
                body = body_to_json(MessageBody((
                    MediaNode(0, kind, name=filename, mime=mime, source_type="qq_official_file",
                              raw_data={"sha256": sha, "size": len(content)}),
                )))
                cursor.execute(
                    "UPDATE delivery_plans SET manifest_json=?, manifest_digest=? WHERE plan_id=?",
                    (manifest, digest(manifest), ref.plan_id),
                )
                cursor.execute("UPDATE deliveries SET body_json=? WHERE plan_id=?", (body, ref.plan_id))
            return ref.plan_id, ref.delivery_ids[0]

    async def _export_bounded(self, scope, path: str, cap: int) -> bytes:
        current = scope.identity
        await scope.current()
        pending = asyncio.create_task(
            self.manager.export_artifact(workspace_owner(current.endpoint), current.sandbox_id, path, file_only=True, max_bytes=cap)
        )
        try:
            while not pending.done():
                await scope.current()
                await asyncio.wait((pending,), timeout=0.25)
            content, archive = await pending
            if archive is not False or type(content) is not bytes or not 0 < len(content) <= cap:
                raise VMDenied("vm_file_export_failed", 502)
            return content
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def _submit_file(self, scope, *, path, request_id: str, filename=None):
        path = workspace_path(path)
        filename = artifact_filename(safe_filename(filename if filename is not None else PurePosixPath(path).name), draft=False)
        current = scope.identity
        expected = {"kind": "send_file", "request_id": request_id, "path": path, "filename": filename}
        existing = await self.calls(self._existing_or_conflict, current, request_id, expected)
        if existing is not None:
            return existing
        service = getattr(self.runtime, "file_service", None)
        sender = getattr(self.runtime, "sender", None)
        cap = getattr(self.manager, "max_file_bytes", None)
        if service is None or sender is None or type(cap) is not int or cap <= 0:
            raise VMDenied("vm_files_unavailable", 503)
        cap = min(cap, service.config.max_file_bytes, 100 * 1024 * 1024)
        key = current.endpoint.key
        inflight_key = (key, request_id)
        if inflight_key in self.inflight:
            raise VMDenied("vm_request_in_progress", 409)
        if self.active_exports.get(key, 0) >= 2 or sum(self.active_exports.values()) >= 4:
            raise VMDenied("vm_file_exports_busy", 429)
        self.active_exports[key] = self.active_exports.get(key, 0) + 1
        self.inflight.add(inflight_key)
        try:
            try:
                async with asyncio.timeout(EXPORT_SECONDS):
                    content = await self._export_bounded(scope, path, cap)
            except VMDenied:
                raise
            except PermissionError:
                raise
            except Exception as error:
                self.logger.warning("VM file export failed error=%s", type(error).__name__)
                raise VMDenied("vm_file_export_failed", 502) from None
            try:
                uploaded = await service.uploader.upload(current.endpoint, content, filename, current=scope.current)
            except FileUploadError as error:
                raise VMDenied(str(error), 502) from None
            plan_id, delivery_id = await self.calls(
                self._enqueue_file, current, generation=scope.generation,
                request_id=request_id, path=path, filename=filename, content=content, uploaded=uploaded,
            )
        finally:
            self.inflight.discard(inflight_key)
            remaining = self.active_exports[key] - 1
            if remaining:
                self.active_exports[key] = remaining
            else:
                self.active_exports.pop(key)
        try:
            async with asyncio.timeout(25):
                await sender.send_delivery(delivery_id)
        except TimeoutError:
            pass
        return await self.calls(self.get_action, current, plan_id)

    async def handle(self, request):
        if request.url.path in ROUTES:
            return await self.reads.handle(request)
        if request.url.path not in {"/vm/say", "/vm/send-file", "/vm/actions"} and not request.url.path.startswith("/vm/actions/"):
            return JSONResponse({"error": "vm_route_not_found"}, 404)
        scope = request.state.channel
        try:
            if request.method == "GET":
                action = request.path_params.get("action_id")
                if action is None and (set(request.query_params) != {"request_id"} or not request.query_params["request_id"]):
                    raise ValueError
                result = await self.calls(self.get_action, scope.identity, action or request.query_params["request_id"], by_request=action is None)
                return JSONResponse(result)
            sender = getattr(self.runtime, "sender", None)
            if sender is None:
                return JSONResponse({"error": "vm_sender_unavailable"}, 503)
            body = bytearray()
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 16384:
                        return JSONResponse({"error": "vm_request_too_large"}, 413)
                    body.extend(chunk)
            await scope.current()
            data = json.loads(body)
            if type(data) is not dict:
                raise ValueError
            if request.url.path == "/vm/send-file":
                if not {"path", "request_id"} <= data.keys() or data.keys() - {"path", "request_id", "program", "filename"}:
                    raise ValueError
                request_id = self._validate_request_id(data["request_id"])
                result = await self._submit_file(scope, path=data["path"], request_id=request_id, filename=data.get("filename"))
                return JSONResponse(result, _http_code(result["state"]))
            if not {"text", "request_id"} <= data.keys() or data.keys() - {"text", "request_id", "program"}:
                raise ValueError
            request_id = self._validate_request_id(data["request_id"])
            expected = {"kind": "say", "request_id": request_id, "text": data["text"]}
            existing = await self.calls(self._existing_or_conflict, scope.identity, request_id, expected)
            if existing is not None:
                return JSONResponse(existing, _http_code(existing["state"]))
            plan_id, delivery_id = await self.calls(
                self._enqueue_say, scope.identity, generation=scope.generation, request_id=request_id, text=data["text"],
            )
            try:
                async with asyncio.timeout(25):
                    await sender.send_delivery(delivery_id)
            except TimeoutError:
                pass
            result = await self.calls(self.get_action, scope.identity, plan_id)
            return JSONResponse(result, _http_code(result["state"]))
        except VMDenied as error:
            return JSONResponse({"error": str(error)}, error.status)
        except PermissionError:
            return JSONResponse({"error": "vm_source_revoked"}, 403)
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_vm_request"}, 400)
        except OutboxInvariantError:
            return JSONResponse({"error": "vm_delivery_unavailable"}, 409)
        except TimeoutError:
            return JSONResponse({"error": "vm_request_timeout"}, 408)
        except Exception as error:
            self.logger.warning("VM HTTP request failed error=%s", type(error).__name__)
            return JSONResponse({"error": "vm_request_unavailable"}, 503)
