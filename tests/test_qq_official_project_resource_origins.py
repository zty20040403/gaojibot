from dataclasses import asdict, replace
import unittest

from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey, ActorKey
from src.plugins.ai_chat.conversation_scope import ConversationScope
from src.plugins.ai_chat.adapters.qq_official.project_authority import Denied, Operation, ProjectRequest
from src.plugins.ai_chat.adapters.qq_official.project_resource_origins import origin_from_evidence
from src.plugins.ai_chat.adapters.qq_official.migration_types import encoded,digest


def evidence(kind='project_task'):
    source=EndpointKey(AccountKey('onebot-v11','100001','production'),'group','12345')
    target=EndpointKey(AccountKey('qq-official','TEST_M7_ORIGIN','test'),'group','TEST_GROUP')
    scope=ConversationScope('onebot-v11','group','12345')
    oldactor=ActorKey(target.account,'group_member','TEST_GROUP','TEST_OLD')
    actor=ActorKey(target.account,'group_member','TEST_GROUP','TEST_NEW')
    sourceactor=ActorKey(source.account,'user','','200001')
    request=ProjectRequest(target,actor,scope,2,1,7,Operation.READ)
    route=dict(route_id='1'*32,source_scope_key=scope.key,source_endpoint_key=source.key,
        source_account_key=source.account.key,target_scope_key=target.scope.key,target_endpoint_key=target.key,
        target_account_key=target.account.key,target_generation=1,target_endpoint_epoch=1,epoch=2,
        phase='completed',active_endpoint_key=target.key)
    project=dict(project_id=7,scope_key=scope.key,bot_native_user_id='100001',status='stopped')
    endpoint=dict(endpoint_key=target.key,account_key=target.account.key,generation=1,route_epoch=1,
        membership_state='present',binding_state='active')
    mapping=dict(mapping_id='2'*32,revision=1,source=sourceactor.key,target=oldactor.key,
        target_endpoint=target.key,expires_at=101)
    data=dict(type='project_task',project_id=7,mapping=dict(project_grant=dict(grant_id='3'*32,revision=1)))
    if kind=='file':
        data=dict(type='artifact_reference',origin='project',immutable_ref=dict(project_id=7,sha256='f'*64),mapping=data['mapping'])
    item=dict(item_id='4'*32,migration_id='5'*32,parent_item_id=None,kind=kind,source_ref='7',
        source_revision=1,classification='unsent',export_json=encoded(data),export_digest='a'*64,
        state='active',source_actor_key=sourceactor.key,target_actor_key=oldactor.key,
        mapping_id=mapping['mapping_id'],mapping_revision=1,mapping_expires_at=101)
    record={key:item[key] for key in ('kind','source_ref','source_revision','classification','export_json','export_digest')}
    record.update(source_actor=sourceactor.key,mapping=mapping)
    preview=dict(source_endpoint=source.key,source_scope=scope.key,target_endpoint=target.key,
        target_scope=target.scope.key,target_generation=1,target_endpoint_epoch=1,items=[record])
    journal=dict(migration_id='5'*32,route_id=route['route_id'],preview_json=encoded(preview),
        preview_digest=digest(preview),approved_by='TEST_ADMIN',approved_version=1)
    stage=dict(grant_id='3'*32,grant_revision=1,project_id=7,source_endpoint_key=source.key,
        source_scope_key=scope.key,original_bot_id='100001',target_account_key=target.account.key,
        target_endpoint_key=target.key,expected_route_epoch=2,migration_id=journal['migration_id'],
        source_actor_key=sourceactor.key,target_actor_key=oldactor.key,mapping_id='2'*32,mapping_revision=1)
    stage['grant_snapshot_json']=encoded(dict(grant_id='3'*32,revision=1,project_id=7,
        account_key=target.account.key,endpoint_key=target.key,actor_key=oldactor.key,
        project_scope_key=scope.key,route_epoch=2,endpoint_generation=1,endpoint_route_epoch=1,
        issued_at=1,expires_at=101))
    return dict(request=request,project=project,endpoint=endpoint,route=route,journal=journal,
        item=item,stage=stage,aliases={scope.key:route['route_id'],target.scope.key:route['route_id']})


class ResourceOriginTests(unittest.TestCase):
    def test_new_actor_and_expired_historical_grant_are_resource_facts_only(self):
        data=evidence();origin=origin_from_evidence(**data)
        self.assertEqual(origin.project_id,7)
        self.assertNotEqual(data['request'].actor.key,data['stage']['target_actor_key'])
        self.assertNotIn('actor_key',asdict(origin));self.assertNotIn('capabilities',asdict(origin))
        self.assertIn('not permission',repr(origin))
        data['item']['state']='expired'
        self.assertEqual(origin_from_evidence(**data),origin)

    def test_same_actor_can_use_history_but_no_new_expiry_or_mapping_is_minted(self):
        data=evidence();first=origin_from_evidence(**data)
        old=ActorKey.parse(data['stage']['target_actor_key'])
        data['request']=replace(data['request'],actor=old)
        self.assertEqual(origin_from_evidence(**data),first)
        self.assertEqual(data['item']['mapping_expires_at'],101)

    def test_project_file_is_valid_but_arbitrary_subagent_file_is_not(self):
        data=evidence('file');self.assertEqual(origin_from_evidence(**data).project_id,7)
        self.change_reviewed_export(data,lambda export:export.update(origin='subagent'))
        with self.assertRaises(Denied):origin_from_evidence(**data)

    def test_other_project_or_nonincluded_project_rejected(self):
        data=evidence();data['request']=replace(data['request'],project_id=8)
        with self.assertRaises(Denied):origin_from_evidence(**data)
        data=evidence();self.change_reviewed_export(data,lambda export:export.update(project_id=8))
        with self.assertRaises(Denied):origin_from_evidence(**data)
        data=evidence();p=__import__('json').loads(data['journal']['preview_json']);p['items']=[]
        data['journal'].update(preview_json=encoded(p),preview_digest=digest(p))
        with self.assertRaises(Denied):origin_from_evidence(**data)

    def test_other_group_same_literal_user_or_other_account_denied(self):
        for account,group in ((AccountKey('qq-official','TEST_OTHER','test'),'TEST_GROUP'),
                              (evidence()['request'].endpoint.account,'TEST_OTHER')):
            data=evidence();target=EndpointKey(account,'group',group)
            actor=ActorKey(account,'group_member',group,'TEST_NEW')
            data['request']=replace(data['request'],endpoint=target,actor=actor)
            with self.assertRaises(Denied):origin_from_evidence(**data)

    def test_route_epoch_rollback_alias_or_physical_change_rejected(self):
        for table,key,value in (('route','phase','rolled_back'),('route','epoch',3),
                ('route','active_endpoint_key','other'),('endpoint','generation',2),
                ('endpoint','route_epoch',2),('endpoint','binding_state','revoked'),
                ('endpoint','membership_state','absent'),('route','target_endpoint_epoch',2)):
            data=evidence();data[table][key]=value
            with self.subTest(table=table,key=key),self.assertRaises(Denied):origin_from_evidence(**data)
        data=evidence();data['aliases'].pop(data['request'].canonical_scope.key)
        with self.assertRaises(Denied):origin_from_evidence(**data)

    def test_deleted_or_different_original_bot_denied(self):
        for key,value in (('status','purged'),('status','trash'),('bot_native_user_id','100002')):
            data=evidence();data['project'][key]=value
            with self.subTest(key=key),self.assertRaises(Denied):origin_from_evidence(**data)

    def test_reviewed_digest_and_exact_stage_ref_required(self):
        for table,key,value in (('journal','preview_digest','b'*64),('item','export_digest','b'*64),
                ('stage','grant_revision',2),('stage','mapping_revision',2),('stage','migration_id','6'*32),
                ('item','target_actor_key','swapped'),('item','state','quarantined'),
                ('item','classification','unknown'),('journal','approved_by',None)):
            data=evidence();data[table][key]=value
            with self.subTest(table=table,key=key),self.assertRaises(Denied):origin_from_evidence(**data)

    def test_snapshot_is_immutable_historical_evidence_not_live_grant_lookup(self):
        data=evidence();before=origin_from_evidence(**data)
        snapshot=__import__('json').loads(data['stage']['grant_snapshot_json']);snapshot['revision']=2
        data['stage']['grant_snapshot_json']=encoded(snapshot)
        with self.assertRaises(Denied):origin_from_evidence(**data)
        self.assertEqual(before.staged_grant_revision,1)

    def test_bounded_parse_and_bool_not_numeric_identity(self):
        for table,key,value in (('journal','preview_json',' '*262145),('item','export_json','NaN'),
                ('endpoint','generation',True),('stage','grant_revision',True)):
            data=evidence();data[table][key]=value
            with self.subTest(table=table,key=key),self.assertRaises(Denied):origin_from_evidence(**data)

    @staticmethod
    def change_reviewed_export(data,change):
        import json
        export=json.loads(data['item']['export_json']);change(export)
        data['item']['export_json']=encoded(export)
        preview=json.loads(data['journal']['preview_json']);preview['items'][0]['export_json']=encoded(export)
        data['journal'].update(preview_json=encoded(preview),preview_digest=digest(preview))
