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
from dataclasses import dataclass, field, replace
from typing import Callable

from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey

from .authority import ConsoleAuthorization, OwnerAuthority, audit


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
    if prefix.casefold() not in {"/bind", "!bind", "/绑定", "!绑定"}:
        return None
    if len(text) > 1024:
        return BindingCommand("invalid", valid=False)
    words = text.strip().split()[1:]
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
                 authority: OwnerAuthority | None = None, clock: Callable[[], float] = time.time):
        if not isinstance(pepper, bytes) or len(pepper) < 32 or not callable(epoch_guard):
            raise ValueError("Binding requires a protected pepper and epoch guard")
        self._pepper = pepper
        self._epoch_guard = epoch_guard
        self.clock = clock
        self.authority = authority or OwnerAuthority(clock=clock)

    def __repr__(self):
        return "BindingReducer(<protected pepper>)"

    def _guard(self, cursor, token, account: AccountKey) -> None:
        # A lost fence is not a normal denial: Tx A must roll back completely.
        if self._epoch_guard(cursor, token) is False:
            raise BindingEpochLost("Binding account lease was lost")
        if getattr(token, "account_key", None) != account.key:
            raise BindingEpochLost("Binding account lease does not match")

    def apply(self, cursor, *, context: BindingContext, command: BindingCommand,
              lease_token) -> BindingDecision:
        self._guard(cursor, lease_token, context.endpoint.account)
        now = int(self.clock())
        valid = self._valid_command(command)
        limited_command = command if valid else BindingCommand("invalid", valid=False)
        if not self._admit_attempt(cursor, context, limited_command, now):
            return BindingDecision("limited", "绑定请求过于频繁，请稍后重试。")
        if not valid:
            return BindingDecision("syntax", "绑定命令参数无效。")
        if command.action == "whoami":
            return BindingDecision("identity", "当前身份：" + context.actor.native_user_id
                + ("\n当前群：" + context.endpoint.native_conversation_id if context.endpoint.kind == 'group' else '')
                + "\n此查询不授予管理权限。")
        if command.action == "create":
            if context.endpoint.kind == 'group' and command.code:
                return self._submit(cursor, context, replace(command, action='submit'), now)
            return self._create(cursor, context, lease_token, now, old_group=command.code)
        if command.action == "submit":
            return self._submit(cursor, context, command, now)
        if command.action in {"status", "cancel"}:
            return self._status_or_cancel(cursor, context, command, now)
        if command.action in {"confirm", "reject"}:
            return self._decide_candidate(cursor, context.endpoint.account, command,
                                          context=context, authorization=None, now=now)
        return BindingDecision("syntax", "绑定命令参数无效。")

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

    def confirm_console(self, cursor, *, account: AccountKey, authorization: ConsoleAuthorization,
                        candidate_id: str, candidate_version: int,
                        grant_submitter_owner: bool) -> BindingDecision:
        if (not self.authority.lock_account(cursor, account)
                or not self.authority.console_current(cursor, authorization)):
            return BindingDecision("denied", "需要有效的管理控制台登录。")
        if (type(grant_submitter_owner) is not bool or not isinstance(candidate_id, str) or not _CANDIDATE.fullmatch(candidate_id)
                or type(candidate_version) is not int or candidate_version < 1):
            return BindingDecision("syntax", "候选确认参数无效。")
        command = BindingCommand("confirm", candidate_id=candidate_id, candidate_version=candidate_version,
                                 grant_submitter_owner=grant_submitter_owner)
        return self._decide_candidate(cursor, account, command, context=None,
                                      authorization=authorization, now=int(self.clock()))

    def reject_console(self, cursor, *, account: AccountKey, authorization: ConsoleAuthorization,
                       candidate_id: str, candidate_version: int) -> BindingDecision:
        if (not self.authority.lock_account(cursor, account)
                or not self.authority.console_current(cursor, authorization)):
            return BindingDecision("denied", "需要有效的管理控制台登录。")
        if (not isinstance(candidate_id, str) or not _CANDIDATE.fullmatch(candidate_id) or type(candidate_version) is not int
                or candidate_version < 1):
            return BindingDecision("syntax", "候选确认参数无效。")
        command = BindingCommand("reject", candidate_id=candidate_id, candidate_version=candidate_version)
        return self._decide_candidate(cursor, account, command, context=None,
                                      authorization=authorization, now=int(self.clock()))

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

    @staticmethod
    def _valid_command(command) -> bool:
        if (not isinstance(command, BindingCommand) or not command.valid
                or type(command.grant_submitter_owner) is not bool):
            return False
        if command.action in {"create", "whoami"}:
            return True
        if command.action in {"status", "cancel", "submit"}:
            if not isinstance(command.challenge_id, str) or not _CHALLENGE.fullmatch(command.challenge_id):
                return False
            return command.action != "submit" or (isinstance(command.code, str) and _CODE.fullmatch(command.code) is not None)
        if command.action in {"confirm", "reject"}:
            return (isinstance(command.candidate_id, str) and _CANDIDATE.fullmatch(command.candidate_id) is not None
                    and type(command.candidate_version) is int and 1 <= command.candidate_version < 1_000_000_000
                    and (command.action == "confirm" or not command.grant_submitter_owner))
        return False

    def _admit_attempt(self, cursor, context, command, now) -> bool:
        account = context.endpoint.account.key
        # Global/account limits run before allocating attacker-chosen bucket keys.
        if not self._rate(cursor, ("qq-binding:v1", "global"), maximum=600, seconds=60, now=now):
            return False
        if not self._rate(cursor, ("qq-binding:v1", "account", account), maximum=120, seconds=60, now=now):
            return False
        okay = self._rate(cursor, ("qq-binding:v1", "actor", account, context.actor.key), maximum=15, seconds=60, now=now)
        okay = self._rate(cursor, ("qq-binding:v1", "endpoint", context.endpoint.key), maximum=30, seconds=60, now=now) and okay
        if command.action == "submit":
            okay = self._rate(cursor, ("qq-binding:v1", "attempt", account, context.actor.key,
                                      context.endpoint.key, command.challenge_id), maximum=5, seconds=600, now=now) and okay
        return okay

    def _code_hash(self, row, code: str) -> str:
        fields = ["qq-binding:v1", row["account_key"], row["challenge_id"], row["issuer_actor_key"],
                  row["owner_grant_id"], row["owner_revision"], row["expires_at"]]
        return hmac.new(self._pepper, (json.dumps(fields, separators=(",", ":")) + ":" + code).encode(),
                        hashlib.sha256).hexdigest()

    def _create(self, cursor, context, token, now, old_group="") -> BindingDecision:
        if context.endpoint.kind != "private":
            return BindingDecision("denied", "请由已配置的 owner 在原私聊申请绑定。")
        grant = self.authority.c2c_grant(cursor, context.actor, lock=True)
        if grant is None:
            return BindingDecision("denied", "当前身份没有绑定管理权限。")
        scope = None
        if old_group:
            from ...conversation_scope import ConversationScope
            scope = ConversationScope('onebot-v11', 'group', old_group).key
            if cursor.execute('SELECT 1 FROM conversations WHERE scope_key=?', (scope,)).fetchone() is None:
                return BindingDecision('invalid', '没有找到这个旧群的历史，请核对群号。')
        self._lock_endpoints(cursor, [context.endpoint.key])
        self._expire_owner(cursor, grant.grant_id, now)
        previous = cursor.execute("""SELECT * FROM qq_official_binding_challenges
            WHERE owner_grant_id=? AND state IN ('sending','issued','candidate') FOR UPDATE""",
            (grant.grant_id,)).fetchone()
        if previous is not None:
            self._terminal(cursor, previous, 'cancelled', now)
        if not self._rate(cursor, ("qq-binding:v1", "issue-owner", grant.grant_id), maximum=3, seconds=600, now=now):
            return BindingDecision("limited", "挑战申请过于频繁，请稍后重试。")
        if not self._rate(cursor, ("qq-binding:v1", "issue-account", context.endpoint.account.key), maximum=20, seconds=600, now=now):
            return BindingDecision("limited", "挑战申请过于频繁，请稍后重试。")
        row = dict(challenge_id="BC-" + secrets.token_hex(16), account_key=context.endpoint.account.key,
                   issuer_actor_key=context.actor.key, issuer_endpoint_key=context.endpoint.key,
                   owner_grant_id=grant.grant_id, owner_revision=grant.revision, expires_at=now + 600)
        code = f"{secrets.randbelow(1_000_000):06d}"
        cursor.execute("""INSERT INTO qq_official_binding_challenges
            (challenge_id,account_key,issuer_actor_key,issuer_endpoint_key,owner_grant_id,owner_revision,
             code_hmac,expires_at,state,attempts,created_at,updated_at,delivery_epoch)
            VALUES (?,?,?,?,?,?,?,?,'sending',0,?,?,?)""",
            (row["challenge_id"], row["account_key"], row["issuer_actor_key"], row["issuer_endpoint_key"],
             grant.grant_id, grant.revision, self._code_hash(row, code), row["expires_at"], now, now, token.epoch))
        cursor.execute('UPDATE qq_official_binding_challenges SET source_scope_key=? WHERE challenge_id=?', (scope, row['challenge_id']))
        audit(cursor, account=context.endpoint.account, action="challenge.created", actor=context.actor.key,
              target=row["challenge_id"], outcome="sending", now=now)
        return BindingDecision("sending", "挑战已创建，等待私聊发送确认。", row["challenge_id"],
            effect=BindingEffect("issue_code", context.endpoint, challenge_id=row["challenge_id"],
                                 expires_at=row["expires_at"], code=code))

    def _submit(self, cursor, context, command, now) -> BindingDecision:
        if context.endpoint.kind != "group":
            return BindingDecision("denied", "绑定码只能在待绑定的群内提交。")
        if command.challenge_id:
            row = cursor.execute("SELECT * FROM qq_official_binding_challenges WHERE account_key=? AND challenge_id=?",
                                 (context.endpoint.account.key, command.challenge_id)).fetchone()
        else:
            candidates = cursor.execute("SELECT * FROM qq_official_binding_challenges WHERE account_key=? AND state='issued' AND expires_at>?",
                (context.endpoint.account.key, now)).fetchall()
            row = next((value for value in candidates if hmac.compare_digest(value['code_hmac'], self._code_hash(value, command.code))), None)
            if row is not None: command = replace(command, challenge_id=row['challenge_id'])
        if row is None:
            return self._invalid()
        grant = self.authority.c2c_grant(cursor, ActorKey.parse(row["issuer_actor_key"]), lock=True)
        endpoints = self._lock_endpoints(cursor, [context.endpoint.key, row["issuer_endpoint_key"]])
        row = cursor.execute("SELECT * FROM qq_official_binding_challenges WHERE challenge_id=? FOR UPDATE",
                             (command.challenge_id,)).fetchone()
        endpoint = endpoints.get(context.endpoint.key)
        if (endpoint is None or endpoint["account_key"] != context.endpoint.account.key
                or endpoint["membership_state"] != "present"):
            return self._invalid()
        if not self._valid_grant(grant, row):
            self._terminal(cursor, row, "cancelled", now)
            return self._invalid()
        if row["expires_at"] <= now:
            self._terminal(cursor, row, "expired", now)
            return self._invalid()
        if row["state"] == "candidate":
            candidate = cursor.execute("SELECT * FROM qq_official_binding_candidates WHERE challenge_id=? FOR UPDATE",
                                       (row["challenge_id"],)).fetchone()
            if candidate and candidate["endpoint_key"] == context.endpoint.key and candidate["actor_key"] == context.actor.key:
                return BindingDecision("pending", "候选已固定，等待原 owner 确认。", row["challenge_id"], candidate["candidate_id"])
            return self._invalid()
        if row["state"] != "issued":
            return self._invalid()
        if not _CODE.fullmatch(command.code) or not hmac.compare_digest(row["code_hmac"], self._code_hash(row, command.code)):
            attempts = int(row["attempts"]) + 1
            cursor.execute("UPDATE qq_official_binding_challenges SET attempts=?,updated_at=? WHERE challenge_id=?",
                           (attempts, now, row["challenge_id"]))
            if attempts >= 5:
                self._terminal(cursor, row, "locked", now)
            audit(cursor, account=context.endpoint.account, action="code.failed", actor=context.actor.key,
                  target=row["challenge_id"], outcome="invalid", now=now)
            return self._invalid()
        candidate_id = "BN-" + secrets.token_hex(16)
        cursor.execute("""INSERT INTO qq_official_binding_candidates
            (candidate_id,challenge_id,account_key,endpoint_key,actor_key,endpoint_generation,version,state,
             expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,1,'pending',?,?,?)""",
            (candidate_id, row["challenge_id"], context.endpoint.account.key, context.endpoint.key,
             context.actor.key, endpoint["generation"], row["expires_at"], now, now))
        cursor.execute("""UPDATE qq_official_binding_challenges SET state='candidate',code_hmac='',
            consumed_at=?,updated_at=? WHERE challenge_id=?""", (now, now, row["challenge_id"]))
        audit(cursor, account=context.endpoint.account, action="candidate.created", actor=context.actor.key,
              target=candidate_id, outcome="pending", now=now)
        owner = BindingContext(EndpointKey.parse(row['issuer_endpoint_key']), ActorKey.parse(row['issuer_actor_key']),
            context.receipt_id, context.received_at)
        return self._decide_candidate(cursor, context.endpoint.account,
            BindingCommand('confirm', candidate_id=candidate_id, candidate_version=1, grant_submitter_owner=True),
            context=owner, authorization=None, now=now)

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

    def _decide_candidate(self, cursor, account, command, *, context, authorization, now):
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
        if context is not None:
            if (context.endpoint.kind != "private" or context.endpoint.key != row["issuer_endpoint_key"]
                    or context.actor.key != row["issuer_actor_key"]):
                return self._invalid()
            confirmer = context.actor.key
        else:
            if authorization is None or not self.authority.console_current(cursor, authorization):
                return self._invalid()
            confirmer = "admin-account:" + authorization.account_id
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

    def _expire_owner(self, cursor, grant_id, now):
        rows = cursor.execute("""SELECT * FROM qq_official_binding_challenges
            WHERE owner_grant_id=? AND state IN ('sending','issued','candidate') AND expires_at<=? FOR UPDATE""",
            (grant_id, now)).fetchall()
        for row in rows:
            self._terminal(cursor, row, "expired", now)


def control_authorized(cursor, inbox_id, endpoint, actor):
    row = cursor.execute("SELECT 1 FROM qq_official_inbound_events WHERE inbox_id=? AND endpoint_key=? AND control_actor_key=? AND reason_code='binding_control' AND status='processed'",
        (inbox_id, endpoint.key, actor.key)).fetchone()
    return row is not None


def control_for_delivery(cursor, delivery):
    row = cursor.execute('SELECT manifest_json FROM delivery_plans WHERE plan_id=?', (delivery['plan_id'],)).fetchone()
    control = json.loads(row['manifest_json']).get('control') if row else None
    return control.get('control_id') if isinstance(control, dict) else None
