import copy
import json
import unittest
from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration,HostConfigurationError
from src.plugins.ai_chat.adapters.qq_official.wiki_configuration import WikiCollectionEvidence,WikiConfiguration
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey


class WikiHostConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.account=AccountKey('qq-official','TEST_HOST_BUSINESS','test')
        self.endpoint=EndpointKey(self.account,'group','TEST_GROUP')
        self.base=dict(version=1,account=self.account.key,
            onebot_accounts=[dict(account=AccountKey('onebot-v11','12345','production').key,deployment_digest='a'*64)])
        self.evidence=dict(endpoint=self.endpoint.key,endpoint_generation=1,route_epoch=1,
            evidence_inbox_id='TEST_INBOX',verified_at=100,expires_at=200)

    def test_default_off_and_explicit_all_native_configuration(self):
        plain=HostConfiguration.parse(json.dumps(self.base))
        self.assertIsNone(plain.wiki);self.assertIsNone(plain.native_schedule)
        raw=self.base|dict(native_background=dict(wiki=dict(enabled=True,profile='wiki',collection_evidence=[self.evidence]),
            schedules=dict(enabled=True,profile='routine',policy_id='TEST_POLICY',revision=1,authorization_until=2000),
            proactive=dict(enabled=True,profile='proactive',percent=20,max_checks_per_hour=2)))
        config=HostConfiguration.parse(json.dumps(raw))
        self.assertEqual(config.wiki.collection_evidence[0].endpoint,self.endpoint)
        self.assertEqual(config.native_schedule.authorization_until,2000)
        self.assertEqual(config.proactive.max_checks_per_hour,2)
        self.assertNotIn('TEST_GROUP',repr(config))

    def test_missing_profile_cross_account_unknown_and_temporal_evidence_rejected(self):
        for change in [dict(profile=None),dict(enabled='true'),dict(arbitrary_callback='module.fn'),
                       dict(collection_evidence=[self.evidence|dict(endpoint=EndpointKey(AccountKey('qq-official','TEST_OTHER','test'),'group','TEST_GROUP').key)]),
                       dict(collection_evidence=[self.evidence|dict(expires_at=100)]),
                       dict(collection_evidence=[self.evidence|dict(endpoint_generation=True)]),
                       dict(collection_evidence=[self.evidence,self.evidence])]:
            raw=copy.deepcopy(self.base);raw['native_background']=dict(wiki=dict(enabled=True,profile='wiki')|change)
            with self.subTest(change=change),self.assertRaises(HostConfigurationError):HostConfiguration.parse(json.dumps(raw))

    def test_collection_dto_never_alone_proves_collection_or_membership(self):
        evidence=WikiCollectionEvidence(**(self.evidence|dict(endpoint=self.endpoint)))
        class Cursor:
            calls=0
            def execute(self,*_):self.calls+=1;return self
            def fetchone(self):return None
        cursor=Cursor();self.assertFalse(evidence.current(cursor,self.endpoint,100));self.assertEqual(cursor.calls,1)
        for now in (99,200,True,float('nan')):self.assertFalse(evidence.current(cursor,self.endpoint,now))
        self.assertEqual(cursor.calls,1)
        self.assertFalse(WikiConfiguration().allowed(cursor,self.endpoint,100))
