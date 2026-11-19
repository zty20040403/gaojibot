"""Transactional binding reducer; no network, commits, or message queue.

Gateway Tx A must deduplicate the receipt BEFORE calling apply, and commit the
decision with its body-free receipt/checkpoint. Normal rejections are values,
so failed verification counts survive. Secrets appear only in transient effects.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey

from .authority import OwnerAuthority, audit


_CHALLENGE = re.compile(r"BC-[0-9a-f]{32}\Z")
_CANDIDATE = re.compile(r"BN-[0-9a-f]{32}\Z")
_CODE = re.compile(r"[0-9]{6}\Z")
_LIVE = ("sending", "issued", "candidate")


@dataclass(frozen=True)
class BindingContext:
    endpoint: EndpointKey = field(repr=False)
    actor: ActorKey = field(repr=False)
    receipt_id: str = field(repr=False)
    received_at: int
    source_native_message_id: str = field(default="", repr=False)

    def __post_init__(self):
        if (self.endpoint.account.platform != "qq-official"
                or self.actor.identity_kind == "bot" or not self.actor.matches_endpoint(self.endpoint)
                or not isinstance(self.receipt_id, str) or not self.receipt_id
                or len(self.receipt_id) > 512 or len(self.endpoint.account.key.encode()) > 512
                or len(self.endpoint.key.encode()) > 1000 or len(self.actor.key.encode()) > 1280
                or type(self.received_at) is not int or self.received_at < 0):
            raise ValueError("Invalid binding context")


@dataclass(frozen=True)
class BindingCommand:
    action: str
    challenge_id: str = ""
    candidate_id: str = ""
    code: str = field(default="", repr=False)
    candidate_version: int = 0
    grant_submitter_owner: bool = False
    valid: bool = True


@dataclass(frozen=True)
class BindingEffect:
    """Transient output only. Never serialize this object into inbox or audit."""

    kind: str
    endpoint: EndpointKey = field(repr=False)
    challenge_id: str = ""
    candidate_id: str = ""
    candidate_version: int = 0
    expires_at: int = 0
    code: str = field(default="", repr=False)
    code_hmac: str = field(default="", repr=False)
    source_native_message_id: str = field(default="", repr=False)
    actual_group: EndpointKey | None = field(default=None, repr=False)
    actual_submitter: ActorKey | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BindingDecision:
    status: str
    message: str
    challenge_id: str = ""
    candidate_id: str = ""
    effect: BindingEffect | None = field(default=None, repr=False)


class BindingEpochLost(RuntimeError):
    pass


def parse_binding_command(text: str) -> BindingCommand | None:
    if not isinstance(text, str):
        return None
    # Check the command prefix without retaining an oversized secret argument.
    leading = text.lstrip()[:32]
    prefix = leading.split(None, 1)[0] if leading else ""
    if prefix.casefold() not in {"/绑定群", "!绑定群", "/bind", "!bind", "/绑定", "!绑定"}:
        return None
    if len(text) > 1024:
        return BindingCommand("invalid", valid=False)
    words = text.strip().split()[1:]
    if prefix.casefold() in {"/绑定群", "!绑定群"}:
        if not words or (len(words) == 1 and _CODE.fullmatch(words[0])):
            return BindingCommand("owner_entry", code=words[0] if words else "")
        return BindingCommand("invalid", valid=False)
    if len(words) == 1 and words[0].isdigit():
        return BindingCommand("create", code=words[0])
    if words == ["create"]:
        return BindingCommand("create")
    if words == ["whoami"]:
        return BindingCommand("whoami")
    if len(words) == 3 and words[0] == "submit" and _CHALLENGE.fullmatch(words[1]) and _CODE.fullmatch(words[2]):
        return BindingCommand("submit", challenge_id=words[1], code=words[2])
    if len(words) == 2 and words[0] in {"status", "cancel"} and _CHALLENGE.fullmatch(words[1]):
        return BindingCommand(words[0], challenge_id=words[1])
    if (len(words) in {3, 4} and words[0] in {"confirm", "reject"}
            and _CANDIDATE.fullmatch(words[1]) and re.fullmatch(r"[1-9][0-9]{0,8}", words[2])
            and (len(words) == 3 or (words[0] == "confirm" and words[3] == "--grant-owner"))):
        return BindingCommand(words[0], candidate_id=words[1], candidate_version=int(words[2]),
                              grant_submitter_owner=len(words) == 4)
    return BindingCommand("invalid", valid=False)


class BindingReducer:
    def __init__(self, pepper: bytes, *, epoch_guard: Callable,
                 authority: OwnerAuthority, clock: Callable[[], float] = time.time):
        if not isinstance(pepper, bytes) or len(pepper) < 32 or not callable(epoch_guard):
            raise ValueError("Binding requires a protected pepper and epoch guard")
        self._pepper = pepper
        self._epoch_guard = epoch_guard
        self.clock = clock
        self.authority = authority

    def __repr__(self):
        return "BindingReducer(<protected pepper>)"

    def _guard(self, cursor, token, account: AccountKey) -> None:
        # A lost fence is not a normal denial: Tx A must roll back completely.
        if self._epoch_guard(cursor, token) is False:
            raise BindingEpochLost("Binding account lease was lost")
        if getattr(token, "account_key", None) != account.key:
            raise BindingEpochLost("Binding account lease does not match")

    def _owner_code_hash(self, account: AccountKey, actor_key: str,
                         epoch: int, expires_at: int, code: str) -> str:
        fields = ["qq-owner-link:v1", account.key, actor_key, epoch, expires_at]
        return hmac.new(self._pepper, (json.dumps(fields, separators=(",", ":")) + ":" + code).encode(),
                        hashlib.sha256).hexdigest()

    def issue_owner_code(self, cursor, *, context: BindingContext, lease_token) -> BindingDecision:
        self._guard(cursor, lease_token, context.endpoint.account)
        if context.endpoint.kind != "private" or self.authority.configured_owner(context.actor) is None:
            return BindingDecision("denied", "只有已配置的 owner 可以申请群绑定码。")
        endpoint = cursor.execute("""SELECT membership_state,binding_state FROM qq_official_endpoints
            WHERE endpoint_key=? AND account_key=? FOR UPDATE""",
            (context.endpoint.key, context.endpoint.account.key)).fetchone()
        if endpoint is None or endpoint['membership_state'] != 'present' or endpoint['binding_state'] == 'revoked':
            return BindingDecision("denied", "当前私聊不可用。")
        now = int(self.clock())
        expires_at = now + 300
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hmac = self._owner_code_hash(context.endpoint.account, context.actor.key,
                                          lease_token.epoch, expires_at, code)
        cursor.execute("""UPDATE qq_official_accounts SET owner_link_actor_key=?,owner_link_code_hmac=?,
            owner_link_expires_at=?,owner_link_epoch=?,updated_at=? WHERE account_key=?""",
            (context.actor.key, code_hmac, expires_at, lease_token.epoch, now, context.endpoint.account.key))
        audit(cursor, account=context.endpoint.account, action="owner-code.issued",
              actor=context.actor.key, target="owner-link-code", outcome="issued", now=now)
        return BindingDecision("owner_code_issued", "群绑定码已创建，5 分钟内有效。",
            effect=BindingEffect("owner_link_code", context.endpoint, expires_at=expires_at,
                                 code=code, code_hmac=code_hmac,
                                 source_native_message_id=context.source_native_message_id))

    def consume_owner_code(self, cursor, *, context: BindingContext,
                           code: str, lease_token) -> BindingDecision:
        self._guard(cursor, lease_token, context.endpoint.account)
        if context.endpoint.kind != "group" or _CODE.fullmatch(code) is None:
            return BindingDecision("invalid", "群绑定码无效或已失效。")
        account = context.endpoint.account
        row = cursor.execute("""SELECT owner_link_actor_key,owner_link_code_hmac,
            owner_link_expires_at,owner_link_epoch FROM qq_official_accounts
            WHERE account_key=? FOR UPDATE""", (account.key,)).fetchone()
        if row["owner_link_code_hmac"] is None:
            return BindingDecision("invalid", "群绑定码无效或已失效。")
        now = int(self.clock())
        cursor.execute("""UPDATE qq_official_accounts SET owner_link_actor_key=NULL,
            owner_link_code_hmac=NULL,owner_link_expires_at=NULL,owner_link_epoch=NULL,updated_at=?
            WHERE account_key=? AND owner_link_code_hmac=? AND owner_link_epoch=?""",
            (now, account.key, row["owner_link_code_hmac"], row["owner_link_epoch"]))
        owner = ActorKey.parse(row["owner_link_actor_key"])
        expected = self._owner_code_hash(account, owner.key, row["owner_link_epoch"],
                                         row["owner_link_expires_at"], code)
        if (row["owner_link_epoch"] != lease_token.epoch or row["owner_link_expires_at"] <= now
                or self.authority.configured_owner(owner) is None
                or not hmac.compare_digest(row["owner_link_code_hmac"], expected)):
            return BindingDecision("invalid", "群绑定码无效或已失效。")
        endpoint = cursor.execute("""SELECT * FROM qq_official_endpoints
            WHERE endpoint_key=? AND account_key=? FOR UPDATE""",
            (context.endpoint.key, account.key)).fetchone()
        if (endpoint is None or endpoint["membership_state"] != "present"
                or endpoint['binding_state'] == 'revoked'):
            return BindingDecision("invalid", "群绑定码无效或已失效。")
        links = cursor.execute("""SELECT l.actor_key,coalesce(l.configured_owner_actor_key,g.c2c_actor_key) AS owner_key
            FROM qq_official_owner_actor_links l LEFT JOIN qq_official_owner_grants g
              ON g.grant_id=l.owner_grant_id AND g.account_key=l.account_key AND g.state='active' AND g.revision=l.owner_revision
            WHERE l.endpoint_key=? AND l.endpoint_generation=? AND l.state='active'
              AND coalesce(l.configured_owner_actor_key,g.c2c_actor_key)=ANY(?)""",
            (context.endpoint.key, endpoint['generation'], list(self.authority.owner_actor_keys))).fetchall()
        if (endpoint['binding_state'] == 'active' and not links) or any(
                (link['actor_key'], link['owner_key']) != (context.actor.key, owner.key) for link in links):
            return BindingDecision("conflict", "这个群已绑定其他 owner。")
        cursor.execute("""UPDATE qq_official_endpoints SET binding_state='active',
            route_epoch=route_epoch+1,updated_at=? WHERE endpoint_key=?""", (now, context.endpoint.key))
        cursor.execute("""INSERT INTO qq_official_owner_actor_links
            (endpoint_key,account_key,actor_key,configured_owner_actor_key,endpoint_generation,
             revision,state,created_at,updated_at) VALUES (?,?,?,?,?,1,'active',?,?)
            ON CONFLICT(endpoint_key,actor_key) DO UPDATE SET configured_owner_actor_key=excluded.configured_owner_actor_key,
            owner_grant_id=NULL,owner_revision=NULL,candidate_id=NULL,
            endpoint_generation=excluded.endpoint_generation,revision=qq_official_owner_actor_links.revision+1,
            state='active',updated_at=excluded.updated_at""",
            (context.endpoint.key, account.key, context.actor.key, owner.key,
             endpoint["generation"], now, now))
        audit(cursor, account=account, action="owner-code.consumed", actor=context.actor.key,
              target="owner-link-code", outcome="bound", now=now)
        return BindingDecision("confirmed", "群 owner 已绑定。")

    def apply(self, cursor, *, context: BindingContext, command: BindingCommand,
              lease_token) -> BindingDecision:
        self._guard(cursor, lease_token, context.endpoint.account)
        if not command.valid:
            return BindingDecision("syntax", "绑定命令参数无效。")
        if command.action == "whoami":
            return BindingDecision("identity", "当前身份：" + context.actor.native_user_id
                + ("\n当前群：" + context.endpoint.native_conversation_id if context.endpoint.kind == 'group' else '')
                + "\n此查询不授予管理权限。")
        if command.action == "owner_entry":
            if context.endpoint.kind == 'private':
                return (BindingDecision('syntax', '请在私聊单独发送 /绑定群。') if command.code
                        else self.issue_owner_code(cursor, context=context, lease_token=lease_token))
            if not command.code:
                return BindingDecision('guide', '请先由已配置的 owner 私聊机器人发送 /绑定群 取得验证码，再在本群发送验证码。')
            return self.consume_owner_code(cursor, context=context, code=command.code, lease_token=lease_token)
        return BindingDecision("retired", "旧绑定入口已停用。")

    def finish_owner_code(self, cursor, *, account: AccountKey, code_hmac: str,
                          outcome: str, lease_token) -> None:
        self._guard(cursor, lease_token, account)
        if outcome != 'success':
            cursor.execute("""UPDATE qq_official_accounts SET owner_link_actor_key=NULL,
                owner_link_code_hmac=NULL,owner_link_expires_at=NULL,owner_link_epoch=NULL
                WHERE account_key=? AND owner_link_code_hmac=?""", (account.key, code_hmac))

    def mark_issued(self, cursor, *, account: AccountKey, challenge_id: str,
                    outcome: str, lease_token) -> BindingDecision:
        self._guard(cursor, lease_token, account)
        row = cursor.execute("""SELECT * FROM qq_official_binding_challenges
            WHERE account_key=? AND challenge_id=? FOR UPDATE""", (account.key, challenge_id)).fetchone()
        if row is None or row["state"] != "sending":
            return BindingDecision("stale", "挑战发送状态已变化。")
        now = int(self.clock())
        grant = self.authority.c2c_grant(cursor, ActorKey.parse(row["issuer_actor_key"]))
        success = (outcome == "success" and row["expires_at"] > now and grant is not None
                   and grant.grant_id == row["owner_grant_id"] and grant.revision == row["owner_revision"]
                   and row["delivery_epoch"] == lease_token.epoch)
        state = "issued" if success else "cancelled"
        cursor.execute("""UPDATE qq_official_binding_challenges SET state=?,updated_at=?,
            code_hmac=CASE WHEN ? THEN code_hmac ELSE '' END WHERE challenge_id=?""",
            (state, now, success, challenge_id))
        audit(cursor, account=account, action="challenge.delivery", actor=row["issuer_actor_key"],
              target=challenge_id, outcome=state, now=now)
        return BindingDecision(state, "挑战已发出。" if success else "挑战已撤销，请重新申请。", challenge_id)

    def recover_sending(self, cursor, *, account: AccountKey, lease_token) -> int:
        """Cancel every pre-crash issue effect; never reconstruct/send its code."""
        self._guard(cursor, lease_token, account)
        now = int(self.clock())
        rows = cursor.execute("""UPDATE qq_official_binding_challenges SET state='cancelled',code_hmac='',updated_at=?
            WHERE account_key=? AND state='sending' RETURNING challenge_id,issuer_actor_key""", (now, account.key)).fetchall()
        for row in rows:
            audit(cursor, account=account, action="challenge.recovered", actor=row["issuer_actor_key"],
                  target=row["challenge_id"], outcome="cancelled", now=now)
        return len(rows)

    def _rate(self, cursor, name: tuple, *, maximum: int, seconds: int, now: int) -> bool:
        bucket = hashlib.sha256(json.dumps(name, separators=(",", ":")).encode()).hexdigest()
        cursor.execute("""INSERT INTO qq_official_binding_rate_limits(bucket,window_start,hits)
            VALUES (?,?,0) ON CONFLICT(bucket) DO NOTHING""", (bucket, now))
        row = cursor.execute("SELECT window_start,hits FROM qq_official_binding_rate_limits WHERE bucket=? FOR UPDATE",
                             (bucket,)).fetchone()
        reset = int(row["window_start"]) + seconds <= now
        hits = 1 if reset else min(int(row["hits"]) + 1, maximum + 1)
        cursor.execute("UPDATE qq_official_binding_rate_limits SET window_start=?,hits=? WHERE bucket=?",
                       (now if reset else row["window_start"], hits, bucket))
        return hits <= maximum

    def _status_or_cancel(self, cursor, context, command, now):
        if context.endpoint.kind != "private":
            return BindingDecision("denied", "请在原 owner 私聊查询或取消。")
        grant = self.authority.c2c_grant(cursor, context.actor, lock=True)
        self._lock_endpoints(cursor, [context.endpoint.key])
        row = cursor.execute("SELECT * FROM qq_official_binding_challenges WHERE account_key=? AND challenge_id=? FOR UPDATE",
                             (context.endpoint.account.key, command.challenge_id)).fetchone()
        if (row is None or row["issuer_actor_key"] != context.actor.key
                or row["issuer_endpoint_key"] != context.endpoint.key or not self._valid_grant(grant, row)):
            return self._invalid()
        if row["expires_at"] <= now and row["state"] in _LIVE:
            self._terminal(cursor, row, "expired", now)
            return BindingDecision("expired", "挑战已过期。", row["challenge_id"])
        if command.action == "cancel":
            if row["state"] not in _LIVE:
                return BindingDecision("stale", "挑战已结束。", row["challenge_id"])
            self._terminal(cursor, row, "cancelled", now)
            audit(cursor, account=context.endpoint.account, action="challenge.cancelled", actor=context.actor.key,
                  target=row["challenge_id"], outcome="cancelled", now=now)
            return BindingDecision("cancelled", "挑战已取消，原码失效。", row["challenge_id"])
        candidate = cursor.execute("SELECT * FROM qq_official_binding_candidates WHERE challenge_id=?",
                                   (row["challenge_id"],)).fetchone()
        effect = None
        if candidate:
            effect = BindingEffect("candidate_preview", context.endpoint, challenge_id=row["challenge_id"],
                candidate_id=candidate["candidate_id"], candidate_version=candidate["version"],
                expires_at=candidate["expires_at"], actual_group=EndpointKey.parse(candidate["endpoint_key"]),
                actual_submitter=ActorKey.parse(candidate["actor_key"]))
        return BindingDecision(row["state"], "绑定状态已读取；群身份授予 owner 默认关闭。", row["challenge_id"],
                               candidate["candidate_id"] if candidate else "", effect=effect)

    def _decide_candidate(self, cursor, account, command, *, context, now):
        candidate = cursor.execute("SELECT * FROM qq_official_binding_candidates WHERE account_key=? AND candidate_id=?",
                                   (account.key, command.candidate_id)).fetchone()
        if candidate is None:
            return self._invalid()
        row = cursor.execute("SELECT * FROM qq_official_binding_challenges WHERE challenge_id=?",
                             (candidate["challenge_id"],)).fetchone()
        grant = self.authority.c2c_grant(cursor, ActorKey.parse(row["issuer_actor_key"]), lock=True)
        endpoints = self._lock_endpoints(cursor, [row["issuer_endpoint_key"], candidate["endpoint_key"]])
        row = cursor.execute("SELECT * FROM qq_official_binding_challenges WHERE challenge_id=? FOR UPDATE",
                             (row["challenge_id"],)).fetchone()
        candidate = cursor.execute("SELECT * FROM qq_official_binding_candidates WHERE candidate_id=? FOR UPDATE",
                                   (candidate["candidate_id"],)).fetchone()
        if (context.endpoint.kind != "private" or context.endpoint.key != row["issuer_endpoint_key"]
                or context.actor.key != row["issuer_actor_key"]):
            return self._invalid()
        confirmer = context.actor.key
        if not self._valid_grant(grant, row):
            self._terminal(cursor, row, "cancelled", now)
            return self._invalid()
        # Retry after a successful commit is safe only for the original exact
        # decision. Changing false to true never becomes an implicit new grant.
        if candidate["state"] == "confirmed":
            if (command.action == "confirm" and candidate["grant_submitter_owner"] is command.grant_submitter_owner
                    and candidate["version"] == command.candidate_version + 1):
                return BindingDecision("already_confirmed", "该候选已按原选择确认。", row["challenge_id"], candidate["candidate_id"])
            return BindingDecision("conflict", "候选确认内容已变化。")
        if candidate["version"] != command.candidate_version or candidate["state"] != "pending" or row["state"] != "candidate":
            return BindingDecision("conflict", "候选版本或状态已变化。")
        if candidate["expires_at"] <= now or row["expires_at"] <= now:
            self._terminal(cursor, row, "expired", now)
            return BindingDecision("expired", "候选已过期，请重新申请。")
        endpoint = endpoints.get(candidate["endpoint_key"])
        if (endpoint is None or endpoint["account_key"] != account.key
                or endpoint["generation"] != candidate["endpoint_generation"]
                or endpoint["membership_state"] != "present"):
            self._terminal(cursor, row, "cancelled", now)
            return BindingDecision("stale", "群状态已变化，请重新申请。")
        if command.action == "reject":
            self._terminal(cursor, row, "rejected", now)
            cursor.execute("UPDATE qq_official_binding_candidates SET confirmed_by=?,consumed_at=? WHERE candidate_id=?",
                           (confirmer, now, candidate["candidate_id"]))
            audit(cursor, account=account, action="candidate.rejected", actor=confirmer,
                  target=candidate["candidate_id"], outcome="rejected", now=now)
            return BindingDecision("rejected", "候选已拒绝，原码失效。", row["challenge_id"], candidate["candidate_id"])
        other = cursor.execute("SELECT owner_grant_id FROM qq_official_owner_actor_links WHERE endpoint_key=? AND state='active'", (endpoint['endpoint_key'],)).fetchall()
        if any(link['owner_grant_id'] != grant.grant_id for link in other):
            return BindingDecision('conflict', '这个群已绑定其他 owner。')
        target = EndpointKey.parse(candidate["endpoint_key"])
        submitter = ActorKey.parse(candidate["actor_key"])
        if target.kind != "group" or not submitter.matches_endpoint(target):
            self._terminal(cursor, row, "cancelled", now)
            return self._invalid()
        cursor.execute("""UPDATE qq_official_endpoints SET binding_state='active',canonical_scope_key=?,
            route_epoch=route_epoch+1,updated_at=? WHERE endpoint_key=?""",
            (row["source_scope_key"] or endpoint["canonical_scope_key"], now, target.key))
        if command.grant_submitter_owner:
            cursor.execute("""INSERT INTO qq_official_owner_actor_links
                (endpoint_key,account_key,actor_key,owner_grant_id,owner_revision,endpoint_generation,revision,state,
                 candidate_id,created_at,updated_at) VALUES (?,?,?,?,?,?,1,'active',?,?,?)
                ON CONFLICT(endpoint_key,actor_key) DO UPDATE SET owner_grant_id=excluded.owner_grant_id,
                owner_revision=excluded.owner_revision,endpoint_generation=excluded.endpoint_generation,
                revision=qq_official_owner_actor_links.revision+1,state='active',candidate_id=excluded.candidate_id,
                updated_at=excluded.updated_at""",
                (target.key, account.key, submitter.key, grant.grant_id, grant.revision, endpoint["generation"], candidate["candidate_id"], now, now))
        cursor.execute("""UPDATE qq_official_binding_candidates SET state='confirmed',version=version+1,
            confirmed_by=?,grant_submitter_owner=?,consumed_at=?,updated_at=? WHERE candidate_id=?""",
            (confirmer, command.grant_submitter_owner, now, now, candidate["candidate_id"]))
        cursor.execute("UPDATE qq_official_binding_challenges SET state='confirmed',code_hmac='',updated_at=? WHERE challenge_id=?",
                       (now, row["challenge_id"]))
        audit(cursor, account=account, action="candidate.confirmed", actor=confirmer,
              target=candidate["candidate_id"], outcome="bound_owner_granted" if command.grant_submitter_owner else "bound_only", now=now)
        return BindingDecision("confirmed", "群已绑定。" + ("已接入旧群历史。" if row["source_scope_key"] else ""),
                               row["challenge_id"], candidate["candidate_id"])

    @staticmethod
    def _lock_endpoints(cursor, keys):
        rows = {}
        for key in sorted(set(keys)):
            row = cursor.execute("SELECT * FROM qq_official_endpoints WHERE endpoint_key=? FOR UPDATE", (key,)).fetchone()
            if row is not None:
                rows[key] = row
        return rows

    @staticmethod
    def _valid_grant(grant, row):
        return grant is not None and grant.grant_id == row["owner_grant_id"] and grant.revision == row["owner_revision"]

    @staticmethod
    def _invalid():
        return BindingDecision("invalid", "挑战无效、已结束或不属于当前身份。")

    @staticmethod
    def _terminal(cursor, row, state, now):
        if row["state"] not in _LIVE:
            return
        cursor.execute("UPDATE qq_official_binding_challenges SET state=?,code_hmac='',updated_at=? WHERE challenge_id=?",
                       (state, now, row["challenge_id"]))
        candidate_state = state if state in {"expired", "rejected"} else "cancelled"
        cursor.execute("""UPDATE qq_official_binding_candidates SET state=?,version=version+1,updated_at=?
            WHERE challenge_id=? AND state='pending'""", (candidate_state, now, row["challenge_id"]))


def control_authorized(cursor, inbox_id, endpoint, actor):
    row = cursor.execute("SELECT 1 FROM qq_official_inbound_events WHERE inbox_id=? AND endpoint_key=? AND control_actor_key=? AND reason_code='binding_control' AND status='processed'",
        (inbox_id, endpoint.key, actor.key)).fetchone()
    return row is not None


def control_for_delivery(cursor, delivery):
    row = cursor.execute('SELECT manifest_json FROM delivery_plans WHERE plan_id=?', (delivery['plan_id'],)).fetchone()
    control = json.loads(row['manifest_json']).get('control') if row else None
    return control.get('control_id') if isinstance(control, dict) else None
