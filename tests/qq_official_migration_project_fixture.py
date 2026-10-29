"""Real owner/group/admin binding for console project migration tests."""
import secrets

from src.plugins.ai_chat.adapters.qq_official.admin_bindings import AdminBindingStore
from src.plugins.ai_chat.adapters.qq_official.authority import OwnerAuthority
from src.plugins.ai_chat.adapters.qq_official.binding import BindingContext, BindingReducer, parse_binding_command
from src.plugins.ai_chat.adapters.qq_official.lease import assert_epoch
from src.plugins.ai_chat.platform_types import ActorKey, EndpointKey
from tests.qq_official_migration_mapping_fixture import record_evidence, rotate_mapping


def install_project_owner(test):
    private=EndpointKey(test.account,'private','TEST_MIGRATION_PROJECT_OWNER')
    owner=ActorKey(test.account,'c2c_user','','TEST_MIGRATION_PROJECT_OWNER')
    test.provision(private)
    authority=OwnerAuthority(clock=lambda:test.now)
    reducer=BindingReducer(test.pepper,authority=authority,epoch_guard=assert_epoch,clock=lambda:test.now)
    def context(endpoint,actor):return BindingContext(endpoint,actor,secrets.token_hex(16),test.now)
    with test.tx() as c:
        configured=authority.apply_trusted_config(c,test.account,(owner.native_user_id,),'TEST_PROJECT_MIGRATION_CONFIG')
        c.execute("UPDATE qq_official_endpoints SET binding_state='unbound' WHERE endpoint_key=?",(test.endpoint.key,))
        issued=reducer.apply(c,context=context(private,owner),command=parse_binding_command('/bind create'),lease_token=test.token)
        reducer.mark_issued(c,account=test.account,challenge_id=issued.challenge_id,outcome='success',lease_token=test.token)
        pending=reducer.apply(c,context=context(test.endpoint,test.actor),
            command=parse_binding_command(f'/bind submit {issued.challenge_id} {issued.effect.code}'),lease_token=test.token)
        confirmed=reducer.apply(c,context=context(private,owner),
            command=parse_binding_command(f'/bind confirm {pending.candidate_id} 1 --grant-owner'),lease_token=test.token)
        test.assertEqual(confirmed.status,'confirmed')
        admins=AdminBindingStore(clock=lambda:test.now,control_guard=lambda c,**kw:kw['authorization'] is test)
        admins.bind_existing(c,admin_account_id='admin',admin_version=1,endpoint=private,actor=owner,
            owner_revision=configured[0].grant.revision,authorization=test)
    test.mapping_inbox=record_evidence(test)
    test.query('''UPDATE qq_official_inbound_events i SET endpoint_generation=e.generation,route_epoch=e.route_epoch
        FROM qq_official_endpoints e WHERE i.inbox_id=? AND e.endpoint_key=i.endpoint_key''',(test.mapping_inbox,))
    rotate_mapping(test)
    return private,owner
