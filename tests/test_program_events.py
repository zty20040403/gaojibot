"""Event projection and cursor snapshots against real PostgreSQL transactions."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from tests.qq_official_outbox_fixture import OutboxFixture
from tests.test_vm_sessions import BOOT
from src.plugins.ai_chat.adapters.qq_official.program_events import ProgramEvents
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.message_ir import MessageBody, TextNode
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey


class ProgramEventTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.ledger = MessageLedger(self.database); self.addCleanup(self.ledger.close)
        self.sessions = VMSessions(self.tx,self.token,private_admission=lambda *_:True,clock=lambda:self.now)
        self.current = self.sessions.issue(self.endpoint,'s123456',BOOT,expected_route=(1,1)).source
        self.events = ProgramEvents(self.sessions)

    def record(self, name, *, endpoint=None, scope=None, direction='inbound', **fields):
        endpoint = endpoint or self.endpoint
        actor = ActorKey(self.account,'bot','','TEST_BOT_OPENID') if direction=='outbound' else ActorKey(
            self.account,'group_member',endpoint.native_conversation_id,'TEST_MEMBER_OPENID')
        return self.ledger.record_message(scope or endpoint.scope,native_message_id='TEST_NATIVE_'+name,
            sender_native_user_id=actor.native_user_id,sender_display='TEST_DISPLAY',
            body=MessageBody((TextNode(0,name),)),occurred_at=self.now,actor=actor,source_endpoint=endpoint,direction=direction,**fields)

    def test_projection_excludes_transport_ids_and_shared_alias_sources(self):
        first = self.record('TEST_FIRST',raw_event={'by':'program'})
        self.record('TEST_BOT',direction='outbound',reply_to_native_message_id=first.native_message_id)
        self.record('TEST_PROGRAM',direction='outbound',raw_event={'by':'program','program':'TEST_UNIT'})
        self.record('TEST_COMMAND_SECRET',message_kind='command')
        other = EndpointKey(self.account,'group','TEST_OTHER'); self.provision(other)
        self.record('TEST_HIDDEN',endpoint=other)
        self.record('TEST_SHARED_ALIAS_HIDDEN',endpoint=other,scope=self.endpoint.scope)
        rows = self.events.page(self.current,0)
        self.assertEqual([row['text'] for row in rows],['TEST_FIRST','TEST_BOT','TEST_PROGRAM'])
        self.assertNotIn('by',rows[0]); self.assertEqual(rows[1]['by'],'bot'); self.assertEqual(rows[2]['by'],'program')
        self.assertEqual(rows[1]['reply_to'],rows[0]['id'])
        self.assertIsInstance(rows[0]['person'],int)
        self.assertTrue(rows[0]['at'].endswith('+08:00'))
        encoded = json.dumps(rows)
        for secret in ('TEST_MEMBER_OPENID','TEST_BOT_OPENID','TEST_NATIVE','actor:v1','TEST_COMMAND_SECRET','HIDDEN'):
            self.assertNotIn(secret,encoded)
        self.assertEqual(set(rows[0]),{'id','at','sender','person','reply_to','text'})

    def test_pagination_has_no_duplicates_and_clear_hides_old_rows_and_reply_links(self):
        first = self.record('TEST_FIRST')
        second = self.record('TEST_SECOND',reply_to_native_message_id=first.native_message_id)
        self.assertEqual([row['id'] for row in self.events.page(self.current,0,limit=1)],[first.canonical_message_id])
        self.assertEqual([row['id'] for row in self.events.page(self.current,first.canonical_message_id)], [second.canonical_message_id])
        self.query('UPDATE conversation_visibility SET min_canonical_message_id=?',(second.canonical_message_id,))
        self.query('INSERT INTO conversation_settings(scope_key,stopped,programs_enabled) VALUES (?,TRUE,FALSE)',(self.current.scope_key,))
        rows = self.events.page(self.current,0)
        self.assertEqual([row['text'] for row in rows],['TEST_SECOND'])
        self.assertIsNone(rows[0]['reply_to'])
        self.assertEqual(self.events.head(self.current),second.canonical_message_id)
        for after in (-1,2**63,True,'TEST'):
            with self.assertRaises(ValueError):
                self.events.page(self.current,after)
        self.sessions.revoke_vm(self.current.sandbox_id)
        with self.assertRaises(PermissionError):
            self.events.page(self.current,0)

    def test_cursor_snapshot_waits_for_uncommitted_ledger_id(self):
        entered,release = threading.Event(),threading.Event()
        owner = {}
        def writer():
            with self.tx() as cursor:
                owner['pid'] = cursor.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
                row = self.record('TEST_UNCOMMITTED',transaction_cursor=cursor)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('TEST ledger barrier timed out')
                return row.canonical_message_id
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(writer)
            try:
                self.assertTrue(entered.wait(3))
                reading = pool.submit(self.events.head,self.current)
                deadline = time.monotonic()+3
                while not self.query('SELECT pid FROM pg_stat_activity WHERE ?=ANY(pg_blocking_pids(pid))',(owner['pid'],)):
                    if time.monotonic()>=deadline:
                        self.fail('snapshot did not wait on the allocating ledger transaction')
                    time.sleep(0.01)
                self.assertFalse(reading.done())
            finally:
                release.set()
            self.assertEqual(reading.result(timeout=3),writing.result(timeout=3))
        self.assertEqual([row['text'] for row in self.events.page(self.current,0)],['TEST_UNCOMMITTED'])
