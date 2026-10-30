from src.plugins.ai_chat.adapters.qq_official.actor_evidence import recent_actor_evidence
from src.plugins.ai_chat.ledger import MessageLedger
from src.plugins.ai_chat.platform_types import ActorKey
from tests.qq_official_outbox_fixture import OutboxFixture
from tests.qq_official_message_fixture import record_evidence


class ActorEvidenceTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.ledger=MessageLedger(self.database)
        self.addCleanup(self.ledger.close)

    def current(self, **changes):
        with self.tx() as cursor:
            return recent_actor_evidence(cursor,**(dict(endpoint=self.endpoint,actor=self.actor,now=self.now)|changes))

    def test_exact_recent_gateway_and_ledger_evidence_required(self):
        record_evidence(self,gateway=False)
        self.assertFalse(self.current())
        record_evidence(self,echo='possible_self')
        self.assertFalse(self.current())
        record_evidence(self)
        self.assertTrue(self.current())
        self.assertFalse(self.current(actor=ActorKey(self.account,'group_member',self.endpoint.native_conversation_id,'TEST_OTHER')))
        self.assertFalse(self.current(now=self.now+301))

    def test_late_replay_or_future_timestamp_does_not_refresh_presence(self):
        old=record_evidence(self,received_at=self.now-301)
        self.query('UPDATE qq_official_inbound_events SET received_at=? WHERE inbox_id=?',(self.now,old))
        record_evidence(self,received_at=self.now+1)
        self.assertFalse(self.current())

    def test_endpoint_generation_membership_and_limits_are_current(self):
        record_evidence(self)
        self.assertTrue(self.current())
        self.query('UPDATE qq_official_endpoints SET generation=generation+1')
        self.assertFalse(self.current())
        self.query("UPDATE qq_official_endpoints SET generation=1,membership_state='absent'")
        self.assertFalse(self.current())
        self.assertFalse(self.current(max_age_seconds=301))
        self.assertFalse(self.current(now=True))
