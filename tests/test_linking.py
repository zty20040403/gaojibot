from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.linking import LinkReducer
from src.plugins.ai_chat.adapters.qq_official.link_commands import parse_link_command
from src.plugins.ai_chat.platform_types import AccountKey,EndpointKey
from tests import test_link_authority as fixture


class LinkReducerTests(fixture.LinkAuthorityTests):
    def setUp(self):
        super().setUp(); self.reducer = LinkReducer(self.auth)
        self.target = EndpointKey(self.account,'group','TEST_LINK_TARGET'); self.provision(self.target)

    def command(self,text,*,endpoint=None,message=None):
        message = message or self.message(endpoint=endpoint,content=text)
        with self.tx() as cursor:
            self.auth.observe(cursor,self.token,message)
            return self.reducer.apply(cursor,self.token,message,parse_link_command(text))

    def issue(self):
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):return self.command('/link')

    def row(self,issued):return self.query('SELECT * FROM qq_official_link_challenges WHERE challenge_id=?',(issued.challenge_id,))[0]

    def accepted(self,issued,*,outcome='committed',native='TEST_LINK_ACCEPTED',received=None):
        with self.tx() as cursor:return self.reducer.record_issue(cursor,self.token,issued.challenge_id,outcome=outcome,
            native_id=native,received_at=self.now if received is None else received)

    def confirm(self,issued):return self.command('/link '+issued.code,endpoint=self.target)

    def test_original_source_duplicate_never_reissues_code_and_both_delivery_orders_activate(self):
        original = self.message(content='/link')
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):
            issued = self.command('/link',message=original)
        self.assertEqual(issued.status,'sending'); self.assertNotIn('723891',repr(issued)+str(self.row(issued)))
        self.assertIsNone(self.command('/link',message=original).code)
        self.assertEqual(self.accepted(issued).status,'issued')
        self.assertEqual(self.confirm(issued).status,'active')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],1)
        self.assertEqual(self.accepted(issued).link_id,self.query('SELECT link_id FROM qq_official_conversation_links')[0]['link_id'])
        self.now += 1; another = self.issue()
        self.assertEqual(self.confirm(another).status,'confirming')
        self.assertEqual(self.accepted(another,native='TEST_LINK_ACCEPTED2').status,'already_linked')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],1)
        self.query("UPDATE qq_official_conversation_links SET state='revoked',generation=generation+1,revoked_at=?",(self.now,))
        self.assertEqual(self.accepted(issued).status,'revoked')

    def test_fast_confirm_unknown_and_late_known_acceptance_never_restore_cancelled_permission(self):
        issued = self.issue(); self.assertEqual(self.confirm(issued).status,'confirming')
        self.assertEqual(self.accepted(issued,outcome='unknown').status,'cancelled')
        self.now += 700
        self.assertEqual(self.accepted(issued).status,'cancelled')
        row = self.row(issued)
        self.assertEqual((row['state'],row['delivery_state'],row['code_hmac']),('cancelled','committed',''))
        self.assertTrue(row['delivery_receipt_hash']); self.assertEqual(row['delivery_received_at'],self.now)
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)

    def test_original_source_or_confirming_target_demotion_cancels_even_if_regranted(self):
        issued = self.issue(); self.now += 1
        self.command('/link',message=self.message('member'))
        self.assertEqual(self.confirm(issued).status,'cancelled')
        self.now += 1; self.command('/link',message=self.message('admin'))
        self.assertEqual(self.confirm(issued).status,'invalid')
        self.now += 1; another = self.issue(); self.confirm(another); self.now += 1
        self.command('/link',endpoint=self.target,message=self.message('member',endpoint=self.target))
        self.assertEqual(self.accepted(another).status,'cancelled')
        self.assertEqual(self.row(another)['delivery_state'],'committed')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)

    def test_same_endpoint_wrong_codes_and_collision_limits_do_not_grant(self):
        issued = self.issue()
        self.assertEqual(self.command('/link '+issued.code).status,'invalid')
        for _ in range(5):self.assertEqual(self.command('/link 000001',endpoint=self.target).status,'invalid')
        self.assertEqual(self.command('/link 000001',endpoint=self.target).status,'limited')
        with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=723891):
            self.assertEqual(self.command('/link',endpoint=self.target).status,'limited')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_link_challenges')[0]['n'],1)
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)

    def test_restart_closes_pending_code_without_replay_or_old_epoch_activation(self):
        issued = self.issue(); self.confirm(issued)
        self.account_lease.close(); self.token = self.lease().acquire()
        with self.tx() as cursor:self.assertEqual(self.reducer.recover(cursor,self.token),1)
        self.assertEqual(self.confirm(issued).status,'invalid')
        self.assertEqual(self.accepted(issued).status,'cancelled')
        self.assertIsNone(self.accepted(issued).code)
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)

    def test_changed_route_requires_fresh_bilateral_grant_not_old_edge_reactivation(self):
        issued = self.issue(); self.accepted(issued); first = self.confirm(issued)
        self.query('UPDATE qq_official_endpoints SET route_epoch=route_epoch+1 WHERE endpoint_key=?',(self.endpoint.key,))
        self.now += 1; another = self.issue(); self.accepted(another,native='TEST_CHANGED_ROUTE_ACCEPTED'); second = self.confirm(another)
        self.assertNotEqual(first.link_id,second.link_id)
        old = self.query('SELECT state,generation FROM qq_official_conversation_links WHERE link_id=?',(first.link_id,))[0]
        self.assertEqual(old,dict(state='revoked',generation=2)); self.assertEqual(second.status,'active')

    def test_failed_or_unknown_before_confirmation_close_atomically_and_late_facts_stay_terminal(self):
        for outcome in ('failed','unknown'):
            issued = self.issue()
            self.assertEqual(self.accepted(issued,outcome=outcome).status,'cancelled')
            self.assertEqual(self.row(issued)['code_hmac'],'')
            self.assertEqual(self.accepted(issued,native='TEST_LATE_'+outcome).status,'cancelled')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)

    def test_fixed_window_reset_does_not_truncate_collision_space_or_allow_more_live_codes(self):
        issued = self.issue(); row = self.row(issued)
        for index in range(19):
            values = dict(row)|dict(challenge_id='LC-'+f'{index:032x}',source_receipt_hash=f'{index:064x}',code_hmac=f'{index+1:064x}')
            self.query('INSERT INTO qq_official_link_challenges('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
        self.query('UPDATE qq_official_binding_rate_limits SET window_start=?,hits=20',(self.now-600,))
        self.assertEqual(self.command('/link',endpoint=self.target).status,'limited')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_link_challenges')[0]['n'],20)

    def test_private_peer_cross_account_and_old_epoch_do_not_get_bilateral_authority(self):
        private = EndpointKey(self.account,'private','TEST_NOT_OWNER'); self.provision(private)
        self.assertEqual(self.command('/link',endpoint=private).status,'denied')
        other = EndpointKey(AccountKey('qq-official','TEST_OTHER_LINK_APP','test'),'group','TEST_LINK_TARGET'); self.provision(other)
        with self.assertRaisesRegex(PermissionError,'account_mismatch'):self.command('/link',endpoint=other)
        issued = self.issue(); self.account_lease.close(); self.token = self.lease().acquire()
        self.assertEqual(self.confirm(issued).status,'cancelled')
        self.assertEqual(self.accepted(issued).status,'cancelled')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)

    def test_activation_uses_the_checked_timestamp_not_a_second_clock_read(self):
        issued = self.issue(); self.confirm(issued); deadline = self.row(issued)['expires_at']; ticks = iter((deadline-1,deadline))
        def now():return next(ticks,deadline)
        with patch.object(self.reducer,'current',return_value=True),patch.object(self.reducer,'now',now):
            self.assertEqual(self.accepted(issued).status,'active')
        self.assertEqual(self.row(issued)['delivery_state'],'committed')
        self.assertEqual(self.row(issued)['activated_at'],deadline-1)

    def test_expiry_after_last_authority_lookup_keeps_receipt_without_activation(self):
        issued = self.issue(); self.confirm(issued); deadline = self.row(issued)['expires_at']; checks = 0
        def current(*args):
            nonlocal checks
            checks += 1
            if checks==2:self.now = deadline
            return True
        with patch.object(self.reducer,'current',current):self.assertEqual(self.accepted(issued).status,'expired')
        self.assertEqual(self.row(issued)['delivery_state'],'committed')
        self.assertEqual(self.query('SELECT count(*) AS n FROM qq_official_conversation_links')[0]['n'],0)


for name in dir(fixture.LinkAuthorityTests):
    if name.startswith('test_') and name not in LinkReducerTests.__dict__:setattr(LinkReducerTests,name,None)
