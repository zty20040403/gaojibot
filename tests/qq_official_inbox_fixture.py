"""Shared PostgreSQL inbox fixture for caption, control, and config tests."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import secrets
from types import SimpleNamespace
import unittest

from src.plugins.ai_chat.adapters.qq_official.inbox import record_pending_inbox
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from tests.postgres_fixture import postgres_for_test

NOW = 1_800_000_000


class QQOfficialIngestTests(unittest.TestCase):
    def setUp(self):
        self.database = postgres_for_test(self)
        self.ledger = MessageLedger(self.database)
        self.jobs = DurableJobStore(self.database)
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.jobs.close)
        self.account = AccountKey("qq-official", "TEST_APP", "test")
        self.endpoint = EndpointKey(self.account, "group", "TEST_GROUP")
        self.token = SimpleNamespace(account_key=self.account.key, epoch=1)
        self.provision(self.endpoint)

    @contextmanager
    def tx(self):
        connection = self.database.store_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    def query(self, sql, args=()):
        with self.tx() as cursor:
            cursor.execute(sql, args)
            return cursor.fetchall() if sql.lstrip().upper().startswith("SELECT") else []

    def provision(self, endpoint, *, binding=None, membership=None):
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_accounts
                (account_key,enabled,fencing_epoch,gateway_state,created_at,updated_at)
                VALUES (?,TRUE,1,'active',?,?) ON CONFLICT DO NOTHING""", (endpoint.account.key, NOW, NOW))
            cursor.execute("""INSERT INTO qq_official_endpoints
                (endpoint_key,account_key,kind,native_conversation_id,canonical_scope_key,
                 binding_state,membership_state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (endpoint.key, endpoint.account.key, endpoint.kind,
                    endpoint.native_conversation_id, endpoint.scope.key,
                    binding or ("active" if endpoint.kind == "group" else "unbound"),
                    membership or ("present" if endpoint.kind == "group" else "unknown"), NOW, NOW))

    def seed(self, *, endpoint=None, native=None, mention=False, reference=None, own_index=None,
             issued=NOW - 20, received=NOW, bot=False, author=None, body=None, generation=1, route=1):
        endpoint = endpoint or self.endpoint
        native = native or "TEST_MSG_" + secrets.token_hex(4)
        inbox = secrets.token_hex(16)
        author_id = author or ("TEST_MEMBER" if endpoint.kind == "group" else endpoint.native_conversation_id)
        raw = {"op": 0, "t": "GROUP_MESSAGE_CREATE" if endpoint.kind == "group" else "C2C_MESSAGE_CREATE",
               "s": 42, "id": "TEST_EVENT_" + inbox,
               "d": {"id": native, "content": body or "TEST conversation text",
                     "author": {"member_openid" if endpoint.kind == "group" else "user_openid": author_id,
                                "nickname": "TEST display", "bot": bot}}}
        if endpoint.kind == "group":
            raw["d"]["group_openid"] = endpoint.native_conversation_id
        if issued is not None:
            raw["d"]["timestamp"] = datetime.fromtimestamp(issued, timezone.utc).isoformat()
        if mention:
            raw["d"]["mentions"] = [{"member_openid": "TEST_BOT", "is_you": True}]
        ext = {}
        if reference is not None:
            ext["ref_msg_idx"] = reference
        if own_index is not None:
            ext["msg_idx"] = own_index
        raw["d"]["message_scene"] = {"ext": ext}
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_inbound_events
                (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,
                 native_message_id,event_id,payload_json,received_at,payload_expires_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (inbox, endpoint.account.key, endpoint.key, generation,
                    route, raw["t"], native, raw["id"], json.dumps(raw, ensure_ascii=False), received,
                    received + 86400, received, received))
        return inbox

    def guard(self, cursor, token):
        row = cursor.execute("""SELECT fencing_epoch,enabled FROM qq_official_accounts
            WHERE account_key=? FOR UPDATE""", (token.account_key,)).fetchone()
        if row is None or not row["enabled"] or row["fencing_epoch"] != token.epoch:
            raise LeaseLost("TEST stale epoch")

    def process(self, inbox, *, cursor=None, **kwargs):
        args = dict(inbox_id=inbox, lease_token=self.token, ledger=self.ledger,
                    epoch_guard=self.guard, admission=lambda cursor, endpoint, actor: True, now=NOW)
        args.pop("jobs", None)
        args.update(kwargs)
        args.pop("jobs", None)
        if cursor is not None:
            return record_pending_inbox(cursor, **args)
        with self.tx() as transaction:
            return record_pending_inbox(transaction, **args)

    def count(self, table):
        return self.query("SELECT COUNT(*) AS n FROM " + table)[0]["n"]
