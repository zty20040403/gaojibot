"""Shared turn execution fixture over real PostgreSQL leases and storage."""
from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.bot_storage import DatabaseCalls
from src.plugins.ai_chat.adapters.qq_official.identity_store import observe_ready
from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
from src.plugins.ai_chat.adapters.qq_official.turn_platform import TransportState
from src.plugins.ai_chat.adapters.qq_official.turns import (
    OfficialTurnConsumer, begin_execution, claim_execution,
)
from src.plugins.ai_chat.application import chat_orchestrator as orchestrator_module
from src.plugins.ai_chat.application.commands import CommandServices
from src.plugins.ai_chat.delivery import DeliveryStore
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.storage.jobs import DurableJobStore
from src.plugins.ai_chat.tasks import RunningTaskRegistry
from src.plugins.ai_chat.turn_journal import TurnJournal
from tests.qq_official_outbox_fixture import OutboxFixture

PDF = b"%PDF-1.4\n% TEST controlled fixture\n%%EOF\n"
KEY = hashlib.sha256(b"TEST_TOOL_CALL").hexdigest()
fixture = sys.modules[__name__]


class ExecutionFixture(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.ledger = MessageLedger(self.database)
        self.journal = TurnJournal(self.database)
        self.jobs = DurableJobStore(self.database)
        for store in (self.ledger, self.journal, self.jobs):
            self.addCleanup(store.close)
        self.message = self.ledger.record_message(
            self.endpoint.scope, native_message_id="TEST_INPUT",
            sender_native_user_id=self.actor.native_user_id, sender_display="test",
            body=MessageBody((TextNode(0, "TEST question"),)), occurred_at=self.now - 5,
            actor=self.actor, source_endpoint=self.endpoint,
        )
        self.inbox = "TEST_INBOX"
        self.job = self.inbox
        with self.tx() as cursor:
            cursor.execute("""INSERT INTO qq_official_inbound_events
                (inbox_id,account_key,endpoint_key,endpoint_generation,route_epoch,event_type,native_message_id,
                 status,echo_state,trigger_requested,canonical_message_id,received_at,created_at,updated_at,turn_text,turn_text_version)
                VALUES (?,?,?,1,1,'GROUP_AT_MESSAGE_CREATE','TEST_INPUT','processed','not_echo',TRUE,?,?,?,?,'TEST question',1)""",
                (self.inbox, self.account.key, self.endpoint.key, self.message.canonical_message_id,
                 self.now - 3, self.now - 3, self.now - 3))
        self.deadline = self.now + 3600
        self.admitted = True

    def admission(self, *_):
        return self.admitted

    def args(self, handle):
        return dict(token=self.token, handle=handle, now=self.now, admission=self.admission)

    def claim(self, purpose="final", kind="chat"):
        with self.tx() as cursor:
            return claim_execution(
                cursor, token=self.token, inbox_id=self.inbox, now=self.now,
                deadline_at=self.deadline, admission=self.admission,
                result_purpose=purpose, execution_kind=kind,
            )

    def begin(self, handle):
        turn = self.journal.start_turn(
            self.endpoint.scope, trigger_canonical_message_id=self.message.canonical_message_id,
            objective="test", provider="test", model="test", started_at=self.now,
        )
        with self.tx() as cursor:
            begin_execution(cursor, **self.args(handle), journal_turn_id=turn.turn_id)
        return turn.turn_id


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixture.ExecutionFixture("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.deliveries = DeliveryStore(self.f.database, recover_interrupted=False)
        self.journal = TurnJournal(self.f.database)
        self.addCleanup(self.deliveries.close)
        self.addCleanup(self.journal.close)
        with self.f.tx() as cursor:
            observe_ready(cursor, self.f.token, {"op": 0, "t": "READY", "d": {
                "session_id": "TEST_SESSION", "user": {"id": "TEST_BOT", "bot": True}}}, self.f.now)
        self.outbox = OfficialSendStore(
            self.deliveries, admission=self.f.admission,
            pepper=b"TEST_CONSUMER_PEPPER" * 3, clock=lambda: self.f.now,
        )
        self.transport = TransportState(self.f.account, True, True)
        self.agent = AsyncMock(return_value="TEST final")
        self.profile = SimpleNamespace(provider_identity="TEST", model="TEST_MODEL", name="TEST_PROFILE")

    async def asyncSetUp(self):
        self.calls = DatabaseCalls()
        ports = orchestrator_module.ChatPorts(
            conversation_id=lambda inbound: inbound.endpoint.key, endpoint_enabled=lambda _: True,
            group_default_profile=lambda _: None, reply_target_turn=lambda _: None,
            record_trigger=Mock(), current_turn_context=lambda *_: "", drain_feedback=lambda _: [],
            ask_agent=self.agent, is_silence_reply=lambda answer: answer == "TEST_SILENCE",
            journal_reply_text=str,
        )
        self.tasks = RunningTaskRegistry()
        self.orchestrator = orchestrator_module.ChatOrchestrator(
            ports=ports, running_tasks=self.tasks,
            model_preferences=SimpleNamespace(get_explicit=lambda _: None),
            model_catalog=SimpleNamespace(resolve_preference=lambda _: self.profile),
            logger=Mock(), prompt_version="TEST", db_call=self.calls,
        )
        self.consumer = OfficialTurnConsumer(
            token=self.f.token, store=SimpleNamespace(admitted=self.f.admission),
            outbox=self.outbox, journal=self.journal, database_calls=self.calls,
            orchestrator=self.orchestrator, command_services=CommandServices(running_tasks=self.tasks),
            transport_state=lambda: self.transport, clock=lambda: self.f.now,
        )

    async def asyncTearDown(self):
        await self.calls.close()
