"""Real PostgreSQL leases and boot-token rotation; no VM or production access."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from unittest.mock import Mock

from tests.qq_official_outbox_fixture import OutboxFixture
from src.plugins.ai_chat.adapters.qq_official.lease import LeaseLost
from src.plugins.ai_chat.adapters.qq_official.vm_sessions import VMSessions, fingerprint
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey

BOOT = '00000000-0000-4000-8000-000000000001'
NEXT_BOOT = '00000000-0000-4000-8000-000000000002'


class VMSessionTests(OutboxFixture):
    def setUp(self):
        super().setUp()
        self.private_admission = Mock(return_value=True)
        self.store = self.reopen()

    def reopen(self, lease=None):
        return VMSessions(self.tx,lease or self.token,private_admission=self.private_admission,clock=lambda:self.now)

    def issue(self, endpoint=None, sandbox_id='s123456', boot_id=BOOT, expected_route=(1,1)):
        return self.store.issue(endpoint or self.endpoint,sandbox_id,boot_id,expected_route=expected_route)

    def test_hash_only_and_reopened_store_keeps_scope_without_human_privileges(self):
        credential = self.issue()
        self.assertNotIn(credential.token,repr(credential))
        row = self.query('SELECT * FROM qq_official_vm_sessions')[0]
        self.assertEqual(row['token_hash'],fingerprint(credential.token))
        self.assertNotIn(credential.token,json.dumps(dict(row)))
        current = self.reopen().authenticate(credential.token)
        self.assertEqual(current,credential.source)
        self.assertEqual(current.scope_key,self.endpoint.scope.key)
        self.assertFalse(hasattr(current,'actor'))
        self.assertEqual(current.caller_ref,'vm:s123456:1')
        with self.tx() as cursor:
            self.store.check(cursor,current)
        self.private_admission.assert_not_called()

    def test_rotation_rejects_old_token_and_already_authenticated_source(self):
        first = self.issue()
        second = self.issue(boot_id=NEXT_BOOT)
        self.assertEqual(second.source.token_generation,2)
        self.assertNotEqual(first.token,second.token)
        self.assertEqual(self.store.authenticate(second.token),second.source)
        with self.assertRaises(PermissionError):
            self.store.authenticate(first.token)
        with self.tx() as cursor, self.assertRaises(PermissionError):
            self.store.check(cursor,first.source)
        self.store.revoke(self.endpoint)
        with self.assertRaises(PermissionError):
            self.store.authenticate(second.token)
        # An explicit lifecycle repair rotates again; old tokens never revive.
        third = self.issue(boot_id=NEXT_BOOT)
        self.assertEqual(third.source.token_generation,3)
        with self.assertRaises(PermissionError):
            self.store.authenticate(second.token)

    def test_shared_history_alias_does_not_share_a_vm_identity(self):
        other = EndpointKey(self.account,'group','TEST_OTHER_GROUP')
        self.provision(other)
        first = self.issue()
        # A history alias has one active route; transferring it cannot transfer
        # the old VM's authority to the newly bound physical endpoint.
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked' WHERE endpoint_key=?",(self.endpoint.key,))
        self.query('UPDATE qq_official_endpoints SET canonical_scope_key=? WHERE endpoint_key=?',
            (self.endpoint.scope.key,other.key))
        second = self.issue(other,'s123457')
        self.assertNotEqual(first.source.scope_key,second.source.scope_key)
        self.assertEqual(self.store.authenticate(second.token).scope_key,other.scope.key)
        with self.assertRaises(PermissionError):
            self.store.authenticate(first.token)
        with self.tx() as cursor, self.assertRaises(PermissionError):
            self.store.check(cursor,replace(first.source,endpoint=other))

    def test_stop_keeps_vm_authentication_but_does_not_grant_action_permission(self):
        credential = self.issue()
        self.query('INSERT INTO conversation_settings(scope_key,stopped,stop_generation) VALUES (?,TRUE,1)',
            (self.endpoint.scope.key,))
        self.assertEqual(self.store.authenticate(credential.token),credential.source)
        from src.plugins.ai_chat.adapters.qq_official.conversation_control import permitted
        with self.tx() as cursor:
            self.assertFalse(permitted(cursor,self.endpoint,generation=0))

    def test_route_or_membership_revocation_fails_closed(self):
        credential = self.issue()
        self.query("UPDATE qq_official_endpoints SET membership_state='absent'")
        with self.assertRaises(PermissionError):
            self.store.authenticate(credential.token)
        self.query("UPDATE qq_official_endpoints SET membership_state='present',route_epoch=2")
        with self.assertRaisesRegex(PermissionError,'vm_route_replaced'):
            self.store.authenticate(credential.token)
        with self.assertRaisesRegex(PermissionError,'vm_route_replaced'):
            self.issue()
        new = self.issue(expected_route=(1,2))
        self.assertEqual(self.store.authenticate(new.token),new.source)
        self.query("UPDATE qq_official_endpoints SET binding_state='revoked'")
        with self.assertRaises(PermissionError):
            self.store.authenticate(new.token)

    def test_private_endpoint_policy_is_rechecked_without_a_fake_group_member(self):
        private = EndpointKey(self.account,'private','TEST_PRIVATE_PEER')
        self.provision(private)
        credential = self.issue(private)
        peer = self.private_admission.call_args.args[2]
        self.assertEqual(peer.native_user_id,'TEST_PRIVATE_PEER')
        self.assertEqual(peer.identity_kind,'c2c_user')
        self.private_admission.return_value = False
        with self.assertRaises(PermissionError):
            self.store.authenticate(credential.token)

    def test_account_lease_loss_and_reacquisition_require_lifecycle_revalidation(self):
        credential = self.issue()
        self.account_lease.close()
        with self.assertRaises(LeaseLost):
            self.store.authenticate(credential.token)
        lease = self.lease().acquire()
        with self.assertRaises(PermissionError):
            self.reopen(lease).authenticate(credential.token)

    def test_another_account_cannot_authenticate_or_revoke_a_vm(self):
        credential = self.issue()
        other = EndpointKey(AccountKey('qq-official','TEST_OTHER_ACCOUNT','test'),'group','TEST_GROUP')
        self.provision(other)
        lease = self.lease(other.account).acquire()
        store = self.reopen(lease)
        with self.assertRaises(PermissionError):
            store.authenticate(credential.token)
        with self.assertRaises(PermissionError):
            store.revoke(self.endpoint)
        self.assertEqual(self.store.authenticate(credential.token),credential.source)

    def test_concurrent_rotation_leaves_one_live_credential(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            credentials = list(pool.map(lambda _:self.issue(),range(4)))
        current = []
        for credential in credentials:
            try:
                current.append(self.store.authenticate(credential.token))
            except PermissionError:
                pass
        self.assertEqual(len(current),1)
        self.assertEqual(current[0].token_generation,4)

    def test_invalid_credentials_and_boot_identity_never_echo_supplied_material(self):
        for token in ('','TEST_JOB_TOKEN','Bearer TEST_SECRET',None,{},'gvm_TEST_SECRET'):
            with self.assertRaisesRegex(PermissionError,'^vm_token_invalid$'):
                self.store.authenticate(token)
        for args in ({'sandbox_id':'../../TEST'},{'boot_id':'TEST_SECRET'}, {'boot_id':None}):
            with self.assertRaisesRegex(ValueError,'^invalid_vm_boot$'):
                self.issue(**args)
