"""Pure physical management/consent with real PG; test IDs are not QQ receipts."""
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.link_commands import parse_link_command
from src.plugins.ai_chat.adapters.qq_official.link_management import LinkManagement
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests import test_linking as fixture


class LinkManagementTests(fixture.LinkReducerTests):
    def setUp(self):
        super().setUp(); self.management = LinkManagement(self.reducer); self.receipt = 0

    def manage(self,text,*,endpoint=None,message=None):
        message = message or self.message(endpoint=endpoint,content=text)
        with self.tx() as cursor:
            self.auth.observe(cursor,self.token,message)
            return self.management.apply(cursor,self.token,message,parse_link_command(text))

    def active(self,*,source=None,target=None):
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):
            issued = self.command('/link',endpoint=source)
        self.receipt += 1; self.accepted(issued,native='TEST_MANAGEMENT_ACCEPTED_'+str(self.receipt))
        result = self.command('/link '+issued.code,endpoint=target or self.target)
        self.assertEqual(result.status,'active')
        return result

    def edge(self,result):return self.query('SELECT * FROM qq_official_conversation_links WHERE link_id=?',(result.link_id,))[0]
    def current_edge(self,result):
        with self.tx() as cursor:return self.management.current(cursor,self.token,self.edge(result))

    def test_established_receipt_backed_consent_survives_otp_deadline_and_epoch(self):
        result = self.active(); self.now += 601
        self.account_lease.close(); self.token = self.lease().acquire()
        self.assertTrue(self.current_edge(result))
        listed = self.manage('/link list'); self.assertEqual(listed.status,'listed')
        self.assertEqual((listed.entries[0].link_id,listed.entries[0].direction),(result.link_id,'outgoing'))
        self.assertNotIn(self.target.native_conversation_id,str(listed.entries))
        with self.tx() as cursor:self.assertFalse(self.reducer.current(cursor,self.token,self.row(result)))

    def test_either_side_can_revoke_one_direction_but_not_reverse_or_repeat_generation(self):
        forward = self.active(); reverse = self.active(source=self.target,target=self.endpoint)
        self.assertEqual(self.manage('/unlink '+forward.link_id,endpoint=self.target).status,'revoked')
        self.assertEqual((self.edge(forward)['state'],self.edge(forward)['generation']),('revoked',2))
        self.assertEqual((self.edge(reverse)['state'],self.edge(reverse)['generation']),('active',1))
        self.assertEqual(self.manage('/unlink '+forward.link_id,endpoint=self.endpoint).status,'revoked')
        self.assertEqual(self.edge(forward)['generation'],2)
        entries = self.manage('/link list',endpoint=self.target).entries
        self.assertEqual({item.direction for item in entries},{'incoming','outgoing'})

    def test_uninvolved_group_private_peer_and_full_account_scope_are_not_authorities(self):
        result = self.active(); outsider = EndpointKey(self.account,'group','TEST_MANAGEMENT_OUTSIDER'); self.provision(outsider)
        self.assertEqual(self.manage('/link list',endpoint=outsider).entries,())
        for text in ('/unlink '+result.link_id,'/link status '+result.challenge_id,'/link cancel '+result.challenge_id):
            self.assertEqual(self.manage(text,endpoint=outsider).status,'invalid')
        private = EndpointKey(self.account,'private','TEST_NOT_LINK_OWNER'); self.provision(private)
        self.assertEqual(self.manage('/unlink '+result.link_id,endpoint=private).status,'denied')
        account = AccountKey(self.account.platform,self.account.app_id,'sandbox')
        endpoint = EndpointKey(account,'group',self.endpoint.native_conversation_id); self.provision(endpoint)
        original = self.token; self.token = self.lease(account).acquire()
        try:
            self.assertEqual(self.manage('/unlink '+result.link_id,endpoint=endpoint).status,'invalid')
            self.assertEqual(self.manage('/link list',endpoint=endpoint).entries,())
        finally:self.token = original
        self.assertEqual(self.edge(result)['state'],'active')

    def test_confirming_party_cancels_code_and_late_facts_do_not_recreate_edge(self):
        issued = self.issue(); self.confirm(issued)
        self.assertEqual(self.manage('/link status '+issued.challenge_id,endpoint=self.target).status,'confirming')
        self.assertEqual(self.manage('/link cancel '+issued.challenge_id,endpoint=self.target).status,'cancelled')
        self.assertEqual(self.accepted(issued).status,'cancelled'); self.assertEqual(self.row(issued)['code_hmac'],'')
        self.assertEqual(self.query('SELECT * FROM qq_official_conversation_links'),[])
        active = self.active()
        self.assertEqual(self.manage('/link cancel '+active.challenge_id).status,'use_unlink')
        self.assertTrue(self.current_edge(active))

    def test_observed_demotion_then_regrant_revokes_old_consent_instead_of_reviving_it(self):
        result = self.active(); self.now += 1; self.proof(self.message('member'))
        self.assertFalse(self.current_edge(result)); self.now += 1; self.proof()
        self.assertFalse(self.current_edge(result))
        self.assertEqual(self.manage('/link list').entries[0].state,'revoked')
        self.assertEqual(self.edge(result)['generation'],2)
        self.assertEqual(self.manage('/link status '+result.challenge_id).status,'revoked')

    def test_private_owner_grant_revision_cannot_be_replaced_by_peer_or_regrant(self):
        private = EndpointKey(self.account,'private','TEST_MANAGEMENT_OWNER'); self.provision(private)
        with self.tx() as cursor:self.auth.owners.apply_trusted_config(cursor,self.account,('TEST_MANAGEMENT_OWNER',),'TEST_OWNER_CONFIG1')
        result = self.active(source=private); self.assertTrue(self.current_edge(result))
        with self.tx() as cursor:self.auth.owners.apply_trusted_config(cursor,self.account,(),'TEST_OWNER_CONFIG2')
        self.assertFalse(self.current_edge(result))
        with self.tx() as cursor:self.auth.owners.apply_trusted_config(cursor,self.account,('TEST_MANAGEMENT_OWNER',),'TEST_OWNER_CONFIG3')
        self.assertFalse(self.current_edge(result))
        self.assertEqual(self.manage('/link list',endpoint=self.target).entries[0].state,'revoked')
        self.assertEqual(self.edge(result)['generation'],2)

    def test_new_bilateral_proof_after_source_or_target_regrant_replaces_old_id(self):
        for endpoint in (self.endpoint,self.target):
            first = self.active(); self.now += 1; self.proof(self.message('member',endpoint=endpoint))
            self.now += 1; self.proof(self.message(endpoint=endpoint))
            second = self.active()
            self.assertNotEqual(first.link_id,second.link_id)
            self.assertEqual((self.edge(first)['state'],self.edge(first)['generation']),('revoked',2))
            self.assertTrue(self.current_edge(second))
            self.manage('/unlink '+second.link_id); self.now += 601

    def test_expiry_during_existing_consent_lookup_never_consumes_new_bilateral_proof(self):
        original = LinkManagement.current
        for stale in (False,True):
            with self.subTest(stale=stale):
                old = self.active(); self.now += 1
                if stale:
                    self.proof(self.message('member')); self.now += 1; self.proof()
                candidate = self.issue(); self.confirm(candidate); deadline = self.row(candidate)['expires_at']
                def crosses(instance,*args):
                    allowed = original(instance,*args); self.now = deadline; return allowed
                with patch.object(LinkManagement,'current',crosses):
                    result = self.accepted(candidate,native='TEST_CONSENT_LOOKUP_'+str(stale))
                self.assertEqual(result.status,'expired')
                self.assertEqual(self.row(candidate)['delivery_state'],'committed')
                self.assertEqual((self.edge(old)['state'],self.edge(old)['generation']),('active',1))
                self.assertEqual(self.query('SELECT * FROM qq_official_conversation_links WHERE challenge_id=?',(candidate.challenge_id,)),[])
            self.manage('/unlink '+old.link_id); self.now += 601

    def test_route_membership_and_missing_receipt_are_not_durable_consents(self):
        result = self.active(); row = self.row(result)
        with patch.object(self.reducer,'get',return_value=dict(row)|dict(delivery_receipt_hash=None)):
            self.assertFalse(self.current_edge(result))
        self.query("UPDATE qq_official_endpoints SET membership_state='absent' WHERE endpoint_key=?",(self.target.key,))
        self.assertFalse(self.current_edge(result))
        self.query("UPDATE qq_official_endpoints SET membership_state='present',route_epoch=route_epoch+1 WHERE endpoint_key=?",(self.target.key,))
        self.assertEqual(self.manage('/link list').entries[0].state,'revoked')

    def test_expiry_after_caller_authority_lookup_denies_without_mutating_link(self):
        result = self.active(); message = self.message(); original = self.auth.current; checks = 0
        def current(*args):
            nonlocal checks
            allowed = original(*args); checks += 1
            if checks==2:self.now = message.platform_occurred_at+600
            return allowed
        with patch.object(self.auth,'current',side_effect=current):
            self.assertEqual(self.manage('/unlink '+result.link_id,message=message).status,'denied')
        self.assertEqual(self.edge(result)['state'],'active')

    def test_physical_scope_filter_precedes_limit_and_never_exposes_foreign_peers(self):
        own = self.active(); source = EndpointKey(self.account,'group','TEST_OTHER_SOURCE'); target = EndpointKey(self.account,'group','TEST_OTHER_TARGET')
        self.provision(source); self.provision(target); original = dict(self.row(own)); template = dict(self.edge(own))
        for index in range(40):
            challenge = original|dict(challenge_id='LC-'+f'{index+100:032x}',source_endpoint_key=source.key,target_endpoint_key=target.key,
                source_scope_key=source.scope.key,target_scope_key=target.scope.key,source_receipt_hash=f'{index+100:064x}',
                target_receipt_hash=f'{index+1000:064x}',delivery_receipt_hash=f'{index+10000:064x}',created_at=self.now+1,
                target_confirmed_at=self.now+1,activated_at=self.now+1,expires_at=self.now+601,delivery_received_at=self.now+1,
                source_actor_key=ActorKey(self.account,'group_member',source.native_conversation_id,self.actor.native_user_id).key,
                target_actor_key=ActorKey(self.account,'group_member',target.native_conversation_id,self.actor.native_user_id).key)
            self.query('INSERT INTO qq_official_link_challenges('+','.join(challenge)+') VALUES ('+','.join('?' for _ in challenge)+')',tuple(challenge.values()))
            edge = template|{key:challenge[key] for key in ('challenge_id','source_endpoint_key','target_endpoint_key','source_scope_key','target_scope_key','created_at')}
            edge['link_id'] = 'f'+f'{index:031x}'
            self.query('INSERT INTO qq_official_conversation_links('+','.join(edge)+') VALUES ('+','.join('?' for _ in edge)+')',tuple(edge.values()))
            self.query("UPDATE qq_official_conversation_links SET state='revoked',generation=generation+1,revoked_at=? WHERE link_id=?",(self.now+1,edge['link_id']))
        self.now += 1; listed = self.manage('/link list')
        self.assertEqual([item.link_id for item in listed.entries],[own.link_id]); self.assertFalse(listed.more)


for name in dir(fixture.LinkReducerTests):
    if name.startswith('test_') and name not in LinkManagementTests.__dict__:setattr(LinkManagementTests,name,None)
