from __future__ import annotations

import asyncio
import re
import secrets
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from .store import SecurityError, SecurityStore, canonical, digest

Executor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Sender = Callable[[str, str, str], Awaitable[None]]
current_principal: ContextVar[dict[str, Any] | None] = ContextVar("admin_principal", default=None)
approved_request: ContextVar[dict[str, Any] | None] = ContextVar("approved_request", default=None)


def principal() -> dict[str, Any]:
    value = current_principal.get()
    if not value:
        raise SecurityError("需要账户登录", 401)
    return value


def actor() -> str:
    return "admin:" + principal()["username"]


def audit_actor() -> str:
    return "account:" + principal()["account_id"] + ":" + principal()["username"]


def approval_command(text: str, *, private: bool = False) -> bool:
    return bool(re.match(r"^\s*/?(?:确认|取消|重发|审批状态|复核|撤销复核|confirm|cancel|resend|review)\s+AP-", text, re.I)
                or (private and re.fullmatch(r"[0-9]{6}", text.strip())))


class MobileAuthorization:
    def __init__(self, store: SecurityStore, *, sender: Sender, bot_selector: Callable[[], str | None], official_sender=None):
        self.store = store
        self.sender = sender
        self.bot_selector = bot_selector
        self.executors: dict[str, Executor] = {"account": self._account_change}
        self.owner = secrets.token_hex(16)
        self.wake = asyncio.Event()
        self.pollers: list[Callable[[], Awaitable[None]]] = []
        self.official_sender = official_sender

    async def _account_change(self, request: dict[str, Any]) -> dict[str, Any]:
        result = await asyncio.to_thread(self.store.apply_account_change, request["account"], request["payload"])
        return {"ok": True, "account": result}

    async def propose(self, account: dict[str, Any], *, kind: str, payload: dict[str, Any], summary: str, reference: str = "", quorum: int = 1, transport_context=None) -> dict[str, Any]:
        bot_id = self.bot_selector()
        request, code = await asyncio.to_thread(self.store.propose, account, bot_id=bot_id, kind=kind, payload=payload, summary=summary, reference=reference, quorum=quorum, transport_context=transport_context)
        if code is not None:
            await self._send_code(request, code)
        result = await asyncio.to_thread(self.store.get, request["approval_id"], account["account_id"])
        message = "在绑定 QQ 的机器人私聊中核对内容，回复 6 位验证码。"
        if result["quorum"] > 1:
            message += f"此操作须两位不同管理员确认；另一位私聊发送「复核 {request['approval_id']}」，核对后输入自己的验证码。"
        else:
            message += "授权后自动继续，无需再确认。"
        return {**result, "executed": False, "message": message}

    async def propose_qq(self, qq_id: str, *, kind: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
        account = await asyncio.to_thread(self.store.account_for_qq, qq_id)
        if account is None:
            raise SecurityError("仅绑定 QQ 的管理员可以执行管理变更")
        return await self.propose(account, kind=kind, payload=payload, summary=summary)

    @staticmethod
    def code_text(request: dict[str, Any], code: str) -> str:
        review = (f"\n高风险操作须两位不同管理员批准。另一位请私聊：复核 {request['approval_id']}\n"
                  if request["quorum"] > 1 else f"\n复核原操作：{request['parent_approval_id']}；记一票，不单独执行命令。\n" if request.get("parent_approval_id") else "")
        return (f"待确认操作：{request['approval_id']}\n{request['summary']}\n\n"
                f"一次性口令：{code}\n3 分钟有效，仅本人可用，成功一次立即失效。\n"
                f"直接回复 {code} 记录本人的批准；达到所需人数才会执行。{review}\n"
                f"取消请回复：取消 {request['approval_id']}\n"
                f"重发请回复：重发 {request['approval_id']}")

    async def _send_code(self, request: dict[str, Any], code: str) -> None:
        text = self.code_text(request, code)
        if request.get('identity_transport') == 'qq_official':
            outcome = 'failed'
            try:
                if self.official_sender is not None:
                    async with asyncio.timeout(30):
                        outcome = await self.official_sender.send_code(request, code, text)
            except asyncio.CancelledError:
                settle = asyncio.create_task(asyncio.to_thread(self.store.delivered,
                    request['approval_id'], request['code_generation'], False))
                while not settle.done():
                    try:
                        await asyncio.shield(settle)
                    except asyncio.CancelledError:
                        continue
                settle.result()
                raise
            except Exception:
                outcome = 'unknown'
            await asyncio.to_thread(self.store.delivered, request['approval_id'], request['code_generation'], outcome == 'success')
            if outcome != 'success':
                raise SecurityError('official_approval_delivery_unavailable', 503)
            return
        try:
            await self.sender(request["bot_id"], request["qq_id"], text)
        except Exception:
            await asyncio.to_thread(self.store.delivered, request["approval_id"], request["code_generation"], False)
            raise SecurityError("QQ 私聊发送失败，操作未获批准；请确认机器人在线且可以私聊，再重发口令", 503) from None
        await asyncio.to_thread(self.store.delivered, request["approval_id"], request["code_generation"], True)

    async def handle_message(self, *, qq_id: str, bot_id: str, text: str, private: bool) -> str:
        if not private:
            return "操作确认只接受管理员 QQ 私聊，请勿在群里发送口令。"
        if re.fullmatch(r"[0-9]{6}", text.strip()):
            try:
                request = await asyncio.to_thread(self.store.confirm, None, qq_id, bot_id, text.strip())
                self.wake.set()
                return self.confirmation_text(request)
            except SecurityError as exc:
                return str(exc)
        parts = text.strip().lstrip("/").split()
        if len(parts) < 2:
            return "直接回复收到的 6 位验证码即可授权。"
        verb, approval_id = parts[0].lower(), parts[1].upper()
        try:
            if verb in {"确认", "confirm"} and len(parts) == 3:
                request = await asyncio.to_thread(self.store.confirm, approval_id, qq_id, bot_id, parts[2])
                self.wake.set()
                return self.confirmation_text(request)
            if verb in {"复核", "review"} and len(parts) == 2:
                request, code = await asyncio.to_thread(self.store.request_review, approval_id, qq_id, bot_id)
                if code is not None:
                    await self._send_code(request, code)
                return f"{request['approval_id']}：请核对私聊中的原始操作，回复你自己的验证码；重复申请不会增加票数。"
            if verb == "撤销复核" and len(parts) == 2:
                await asyncio.to_thread(self.store.revoke_review, approval_id, qq_id, bot_id)
                return f"{approval_id} 复核已撤销，未开始的操作不会继续。"
            if verb in {"取消", "cancel"} and len(parts) == 2:
                await asyncio.to_thread(self.store.cancel, approval_id, qq_id, bot_id)
                return f"{approval_id} 已取消，口令已失效。"
            if verb in {"重发", "resend"} and len(parts) == 2:
                request, code = await asyncio.to_thread(self.store.resend, approval_id, qq_id, bot_id)
                await self._send_code(request, code)
                return "已发送新口令，旧口令已失效。"
            if verb == "审批状态" and len(parts) == 2:
                account = await asyncio.to_thread(self.store.account_for_qq, qq_id)
                if not account:
                    raise SecurityError("需要绑定的管理员 QQ")
                request = await asyncio.to_thread(self.store.get, approval_id, account["account_id"])
                return f"{approval_id}：{request['status']}\n{canonical(request['result'])}"
            return "直接回复 6 位验证码即可授权；取消/重发 AP-操作编号。"
        except SecurityError as exc:
            return str(exc)

    @staticmethod
    def confirmation_text(request):
        count, quorum = request.get("approved_count", 1), request.get("quorum", 1)
        if request["status"] == "queued":
            return f"{request['approval_id']} 已授权（{count}/{quorum}），验证码已失效，将自动继续执行。"
        return f"{request['approval_id']} 已记录本人批准（{count}/{quorum}），尚未执行，仍需另一位管理员确认。"

    async def run_once(self) -> bool:
        request = await asyncio.to_thread(self.store.claim, self.owner)
        if request is None:
            return False
        approval_id = request["approval_id"]

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(15)
                if not await asyncio.to_thread(self.store.renew, approval_id, self.owner):
                    return

        renewal = asyncio.create_task(heartbeat())
        identity = current_principal.set(request["account"])
        authorization = approved_request.set(request)
        try:
            executor = self.executors.get(request["kind"])
            if executor is None:
                raise SecurityError("该操作执行器不可用，请重新发起", 503)
            result = await executor(request)
            status = "needs_attention" if result.get("needs_attention") else "succeeded" if result.get("ok", True) else "failed"
        except SecurityError as exc:
            result, status = {"ok": False, "error": str(exc), "status_code": exc.status}, "failed"
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.finish, approval_id, self.owner,
                                    {"ok": False, "error": "执行被中断，需核对实际结果；不会自动重放"}, status="needs_attention")
            raise
        except Exception:
            result, status = {"ok": False, "error": "执行回执不确定，请检查操作记录；不会自动重放"}, "needs_attention"
        finally:
            current_principal.reset(identity)
            approved_request.reset(authorization)
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        await asyncio.to_thread(self.store.finish, approval_id, self.owner, result, status=status)
        await self.send_receipts()
        return True

    async def send_receipts(self) -> None:
        for request in await asyncio.to_thread(self.store.unsent_receipts):
            approval_id, status = request["approval_id"], request["status"]
            result = request["result"]
            try:
                if request.get('identity_transport') == 'qq_official':
                    if self.official_sender is None:
                        continue
                    if await self.official_sender.send_receipt(request) == 'success':
                        await asyncio.to_thread(self.store.receipt_sent, approval_id)
                    continue
                labels = {"succeeded": "授权操作已完成", "failed": "执行失败", "needs_attention": "需要核对执行结果"}
                if result.get("submitted"):
                    labels["succeeded"] = "已提交，正在等待服务器执行结果"
                await self.sender(request["bot_id"], request["qq_id"], f"{approval_id}：{labels[status]}\n{canonical(result)[:3500]}")
                await asyncio.to_thread(self.store.receipt_sent, approval_id)
            except Exception:
                # A retry can duplicate a receipt, but never repeats the operation.
                continue

    async def run_forever(self) -> None:
        while True:
            self.wake.clear()
            try:
                await self.run_once()
                await self.send_receipts()
                for poller in self.pollers:
                    await poller()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Never log payloads or exception locals containing credentials.
                import traceback
                frames = ' > '.join(f'{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(error.__traceback__)[-6:])
                logging.getLogger(__name__).warning("Mobile authorization background check failed: %s at %s", type(error).__name__, frames)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass


def assert_approved(kind: str, payload: dict[str, Any]) -> None:
    request = approved_request.get()
    if not request or request["kind"] != kind or request["payload_hash"] != digest(canonical(payload)):
        raise SecurityError("这项操作尚未通过手机口令确认")
