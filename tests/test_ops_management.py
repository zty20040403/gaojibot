from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.cluster_control.adapters.ops import OpsClient, OpsError
from src.cluster_control.execution_contracts import canonical_json
from src.cluster_control.ops_management import OpsManagementService


class MemoryStore:
    def __init__(self):
        self.records = {}

    def find_operation(self, actor, origin, key):
        return next((r for r in self.records.values() if (r['actor_id'], r['origin_scope'], r['idempotency_key']) == (actor, origin, key)), None)

    def prepare_operation(self, record):
        self.records[record['operation_id']] = copy.deepcopy(record)
        return copy.deepcopy(record)

    def get_operation(self, key):
        return copy.deepcopy(self.records.get(key))

    def approve_operation(self, key, **kwargs):
        record = self.records[key]
        if record['contract_hash'] != kwargs['expected_hash'] or record['resource_version'] != kwargs['expected_version']:
            raise ValueError('Changed intent')
        if record['status'] != 'awaiting_approval':
            raise ValueError('Not awaiting approval')
        record['status'] = 'queued'
        return copy.deepcopy(record)

    def claim_managed_operation(self, owner):
        for record in self.records.values():
            if record['status'] in {'queued', 'running', 'cancelling', 'reconciling'}:
                record['status'] = 'cancelling' if record['status'] == 'cancelling' else 'running'
                record['fence'] = record.get('fence', 0) + 1
                return copy.deepcopy(record)

    def finish_managed_operation(self, key, **kwargs):
        self.records[key].update(status=kwargs['status'], result=kwargs['result'],
            backend_operation_id=kwargs['backend_id'], error_code=kwargs['error'])
        return True


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = Path(self.tmp.name) / 'token'
        self.token.write_text('test-credential-' + 'a' * 40)
        self.calls = []
        self.state = 'running'
        self.fail_submission = False
        self.fail_poll = False
        self.poll_errors = []
        self.response_job_id = 'job_test'
        self.now = time.time()
        self.finished_at = self.now
        self.clock = patch('src.cluster_control.service_verification.time', SimpleNamespace(time=lambda: self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.params = {'host': 'h610', 'unit': 'test.service'}
        self.before = {'active_state': 'active', 'sub_state': 'running', 'invocation_id': 'a' * 32}
        self.after = {'active_state': 'active', 'sub_state': 'running', 'invocation_id': 'b' * 32,
                      'reload_result': 'success', 'service_result': 'success'}
        self.live = {'unit': 'test.service', 'load_state': 'loaded', 'active_state': 'active', 'sub_state': 'running',
                     'details': {'invocation_id': 'b' * 32, 'main_pid': 234, 'restarts': 0}}
        self.live_age = 0
        self.live_host = None
        self.fail_unit_read = False
        self.definitions = [{
            'name': name, 'read_only': readonly, 'kind': kind,
            'idempotency': 'none' if readonly else 'required',
            'summary': 'Test operation', 'params_schema': {
                'type': 'object', 'properties': {'host': {'type': 'string'}, 'unit': {'type': 'string'}},
                'required': ['host'], 'additionalProperties': False,
            },
        } for name, readonly, kind in [('host.facts', True, 'observation'), ('units.restart', False, 'job_submission')]]
        def handle(request):
            self.calls.append(request)
            if request.method == 'GET':
                return httpx.Response(200, json={'version': 2, 'operations': self.definitions})
            op = json.loads(request.content)['op']
            if op == 'jobs.status':
                if self.poll_errors:
                    return httpx.Response(self.poll_errors.pop(0), json={'error': 'test observation error'})
                if self.fail_poll:
                    raise httpx.ReadTimeout('unavailable', request=request)
                return httpx.Response(200, json={'handle': {'job_id': self.response_job_id, 'revision': 2, 'state': self.state,
                    'host': self.params['host'], 'operation': self.unit_operation}, 'spec': self.params,
                    'updated_at': datetime.fromtimestamp(self.finished_at, timezone.utc).isoformat(),
                    'result': {'unit': self.params['unit'], 'action': self.unit_operation.removeprefix('units.'),
                        'success': True, 'attribution': 'systemd_job_accepted_and_target_observed',
                        'manager_job': '/org/freedesktop/systemd1/job/123', 'before': self.before, 'after': self.after}})
            if op == 'units.status':
                if self.fail_unit_read:
                    raise httpx.ReadTimeout('unit observation unavailable', request=request)
                return httpx.Response(200, json={'host': self.live_host or self.params['host'], 'unit': self.live,
                    'observed_at': datetime.fromtimestamp(self.now - self.live_age, timezone.utc).isoformat()})
            if op == 'jobs.cancel':
                return httpx.Response(200, json={'handle': {'job_id': 'job_test', 'revision': 3, 'state': 'running'}})
            if op in {'units.start', 'units.stop', 'units.restart', 'units.reload'}:
                self.params = json.loads(request.content)['params']
                self.unit_operation = op
                if self.fail_submission:
                    raise httpx.ReadTimeout('lost receipt', request=request)
                return httpx.Response(200, json={'job_id': 'job_test', 'revision': 1, 'state': 'queued'})
            return httpx.Response(200, json={'host': 'h610'})
        self.store = MemoryStore()
        client = OpsClient('http://ops.test', self.token, transport=httpx.MockTransport(handle))
        self.manager = OpsManagementService(client, self.store, hosts=('h610', 'h310', 'tank'), actors=('qq:3526452465', 'admin:kenneth'))

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    async def propose(self, key='test-intent-0001', host='h610'):
        return await self.manager.call('units.restart', {'host': host, 'unit': 'test.service'}, actor='qq:3526452465', origin='group:123', idempotency_key=key)

    async def finish_verification(self):
        self.now += 6
        await self.manager.run_once()

    async def approve(self, proposal):
        r = proposal['operation']
        return await self.manager.approve(r['operation_id'], actor='admin:kenneth', expected_hash=r['contract_hash'], expected_version=r['resource_version'])

    def posts(self, operation):
        return [r for r in self.calls if r.method == 'POST' and json.loads(r.content)['op'] == operation]

    async def test_catalog_and_actor_boundary(self):
        with self.assertRaises(PermissionError):
            await self.manager.catalog('qq:other')
        self.assertEqual(self.calls, [])
        catalog = await self.manager.catalog('admin:kenneth')
        self.assertNotIn('params_schema', catalog['operations'][0])
        detail = await self.manager.catalog('admin:kenneth', 'units.restart')
        self.assertIn('params_schema', detail['operations'][0])

    async def test_reads_direct_and_other_hosts_blocked(self):
        result = await self.manager.call('host.facts', {'host': 'h610'}, actor='admin:kenneth', origin='admin-console')
        self.assertEqual(result['result']['host'], 'h610')
        self.assertFalse(self.store.records)
        with self.assertRaises(PermissionError):
            await self.propose(host='b650')
        self.assertFalse(self.posts('units.restart'))

    async def test_intent_receipt_is_read_only_without_catalog_approval_or_replay(self):
        key = 'subagent:' + 'a' * 64
        proof = {'host_id': 'h610', 'historical': True, 'intent_key': key,
                 'origin_scope': 'group:123', 'authorized_before_dispatch': False,
                 'recorded_status': 'awaiting_approval'}
        self.store.operation_provenance = Mock(return_value=proof)
        with patch.object(self.manager, 'call', new_callable=AsyncMock) as submit, \
             patch.object(self.manager, 'approve', new_callable=AsyncMock) as approve, \
             patch.object(self.manager, 'definitions', new_callable=AsyncMock) as catalog, \
             patch.object(self.manager.client, '_credential', side_effect=AssertionError('No upstream credential read')):
            for _ in range(2):
                self.assertEqual(await self.manager.receipt(key, actor='admin:kenneth', origin='group:123'), proof)
            self.store.operation_provenance.assert_called_with('admin:kenneth', 'group:123', key)
            submit.assert_not_awaited()
            approve.assert_not_awaited()
            catalog.assert_not_awaited()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.records, {})

    async def test_intent_receipt_has_exact_actor_origin_key_and_host_boundaries(self):
        key = 'subagent:' + 'a' * 64
        proof = {'host_id': 'h610', 'intent_key': key}
        self.store.operation_provenance = Mock(side_effect=lambda actor, origin, intent:
            proof if (actor, origin, intent) == ('qq:3526452465', 'group:123', key) else None)
        for actor, origin, intent in (('admin:kenneth', 'group:123', key),
                ('qq:3526452465', 'group:other', key), ('qq:3526452465', 'group:123', 'subagent:' + 'b' * 64)):
            with self.subTest(actor=actor, origin=origin, key=intent):
                with self.assertRaises(LookupError):
                    await self.manager.receipt(intent, actor=actor, origin=origin)
        self.store.operation_provenance.reset_mock()
        with self.assertRaises(PermissionError):
            await self.manager.receipt(key, actor='qq:other', origin='group:123')
        self.store.operation_provenance.assert_not_called()
        proof['host_id'] = 'outside-grant'
        with self.assertRaises(LookupError):
            await self.manager.receipt(key, actor='qq:3526452465', origin='group:123')
        self.assertEqual(self.calls, [])

    async def test_invalid_intent_keys_are_rejected_before_storage(self):
        self.store.operation_provenance = Mock()
        for key in ('', 'a' * 64, 'subagent:' + 'a' * 63, 'subagent:' + 'A' * 64,
                    'subagent:' + 'a' * 64 + '\n', 'subagent:' + 'a' * 65, None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                await self.manager.receipt(key, actor='admin:kenneth', origin='group:123')
        self.store.operation_provenance.assert_not_called()

    async def test_schema_and_unknown_operation_rejected(self):
        with self.assertRaises(ValueError):
            await self.manager.call('units.restart', {'host': 'h610', 'arbitrary': True}, actor='admin:kenneth', origin='admin-console')
        with self.assertRaises(PermissionError):
            await self.manager.call('invented', {}, actor='admin:kenneth', origin='admin-console')
        self.assertFalse(self.posts('units.restart'))

    async def test_deployment_target_uses_the_same_host_grant(self):
        self.definitions.append({
            'name': 'deploy.prepare', 'read_only': False, 'kind': 'job_submission',
            'idempotency': 'required', 'params_schema': {
                'type': 'object', 'properties': {'target_host': {'type': 'string'}},
                'required': ['target_host'], 'additionalProperties': False,
            },
        })
        with self.assertRaises(PermissionError):
            await self.manager.call('deploy.prepare', {'target_host': 'b650'},
                actor='admin:kenneth', origin='admin-console', idempotency_key='deploy-wrong-host')
        self.assertFalse(self.store.records)
        self.assertFalse(self.posts('deploy.prepare'))

    async def test_write_requires_approval_and_runs_once(self):
        proposal = await self.propose()
        self.assertTrue(proposal['approval_required'])
        self.assertFalse(await self.manager.run_once())
        self.assertFalse(self.posts('units.restart'))
        await self.approve(proposal)
        await self.manager.run_once()
        self.assertEqual(self.posts('units.restart')[0].headers['Idempotency-Key'], proposal['operation']['operation_id'])
        await self.manager.run_once()
        self.state = 'succeeded'
        await self.manager.run_once()
        await self.finish_verification()
        again = await self.propose()
        self.assertTrue(again['executed'])
        self.assertFalse(again['approval_required'])
        self.assertEqual(len(self.posts('units.restart')), 1)

    async def test_same_key_is_bound_to_same_parameters(self):
        first = await self.propose()
        second = await self.propose()
        self.assertEqual(first['operation']['operation_id'], second['operation']['operation_id'])
        with self.assertRaises(ValueError):
            await self.propose(host='tank')

    async def test_rotation_and_schema_change_invalidate_approval(self):
        proposal = await self.propose()
        self.token.write_text('test-credential-' + 'b' * 40)
        with self.assertRaises(PermissionError):
            await self.approve(proposal)
        proposal = await self.propose('test-intent-0002')
        self.definitions[1]['params_schema']['properties']['host']['minLength'] = 1
        with self.assertRaises(PermissionError):
            await self.approve(proposal)
        self.assertFalse(self.posts('units.restart'))

    async def test_missing_receipt_never_replays(self):
        proposal = await self.propose()
        await self.approve(proposal)
        self.fail_submission = True
        await self.manager.run_once()
        self.assertFalse(await self.manager.run_once())
        record = self.store.get_operation(proposal['operation']['operation_id'])
        self.assertEqual(record['status'], 'needs_attention')
        self.assertEqual(len(self.posts('units.restart')), 1)
        self.assertTrue(record['result']['submission_started'])

    async def test_read_only_validation_retries_before_one_submission(self):
        proposal = await self.propose()
        await self.approve(proposal)
        self.store.records[proposal['operation']['operation_id']]['arguments']['deployment'] = {'host_id': 'h610'}
        self.manager.deployment_validator = AsyncMock(side_effect=[
            OpsError('upstream_error', 'HTTP 503 while reading workspace', retryable=True), None,
        ])
        with patch('src.cluster_control.ops_management.asyncio.sleep', new_callable=AsyncMock):
            await self.manager.run_once()
        self.assertEqual(self.manager.deployment_validator.await_count, 2)
        self.assertEqual(len(self.posts('units.restart')), 1)
        self.assertEqual(self.store.get_operation(proposal['operation']['operation_id'])['status'], 'running')

    async def test_validation_outage_is_known_not_submitted(self):
        proposal = await self.propose()
        await self.approve(proposal)
        self.store.records[proposal['operation']['operation_id']]['arguments']['deployment'] = {'host_id': 'h610'}
        self.manager.deployment_validator = AsyncMock(side_effect=OpsError('upstream_error', '503', retryable=True))
        with patch('src.cluster_control.ops_management.asyncio.sleep', new_callable=AsyncMock):
            await self.manager.run_once()
        record = self.store.get_operation(proposal['operation']['operation_id'])
        self.assertEqual(self.manager.deployment_validator.await_count, 3)
        self.assertFalse(self.posts('units.restart'))
        self.assertEqual(record['status'], 'failed')
        self.assertIs(record['result']['submission_started'], False)

    async def test_cancellation_during_validation_prevents_submission(self):
        proposal = await self.propose()
        await self.approve(proposal)
        key = proposal['operation']['operation_id']
        self.store.records[key]['arguments']['deployment'] = {'host_id': 'h610'}
        async def cancelled(_):
            self.store.records[key]['status'] = 'cancelling'
        self.manager.deployment_validator = cancelled
        await self.manager.run_once()
        self.assertFalse(self.posts('units.restart'))
        self.assertIs(self.store.get_operation(key)['result']['submission_started'], False)

    async def test_poll_outage_keeps_remote_job(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        self.fail_poll = True
        await self.manager.run_once()
        record = self.store.get_operation(proposal['operation']['operation_id'])
        self.assertEqual(record['status'], 'reconciling')
        self.assertEqual(record['backend_operation_id'], 'job_test')
        self.assertEqual(len(self.posts('units.restart')), 1)

    async def test_cancel_waits_for_target_confirmation(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        self.store.records[proposal['operation']['operation_id']]['status'] = 'cancelling'
        await self.manager.run_once()
        self.assertEqual(self.store.get_operation(proposal['operation']['operation_id'])['status'], 'cancelling')
        self.state = 'cancelled'
        await self.manager.run_once()
        self.assertEqual(self.store.get_operation(proposal['operation']['operation_id'])['status'], 'cancelled')

    async def test_transient_rejected_receipt_rechecks_same_job_without_resubmission(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        self.poll_errors = [400]
        self.state = 'succeeded'
        with patch('src.cluster_control.ops_management.asyncio.sleep', new_callable=AsyncMock):
            await self.manager.run_once()
        self.assertEqual(len(self.posts('jobs.status')), 2)
        await self.finish_verification()
        self.assertEqual(self.store.get_operation(proposal['operation']['operation_id'])['status'], 'succeeded')
        self.assertEqual(len(self.posts('units.restart')), 1)
        self.assertTrue(all(json.loads(r.content)['params'] == {'job_id': 'job_test'}
                            for r in self.posts('jobs.status')))

    async def test_rejected_receipt_retries_are_bounded_and_do_not_claim_failure(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        self.poll_errors = [400] * 4
        with patch('src.cluster_control.ops_management.asyncio.sleep', new_callable=AsyncMock):
            await self.manager.run_once()
        record = self.store.get_operation(proposal['operation']['operation_id'])
        self.assertEqual(record['status'], 'needs_attention')
        self.assertEqual(record['backend_operation_id'], 'job_test')
        self.assertEqual(len(self.posts('jobs.status')), 3)
        self.assertEqual(len(self.posts('units.restart')), 1)

    async def test_http_409_observation_recovers_on_each_host_without_reexecution(self):
        for host in ('h310', 'h610', 'tank'):
            with self.subTest(host=host):
                self.calls.clear()
                proposal = await self.propose(key='receipt-conflict-' + host, host=host)
                await self.approve(proposal)
                await self.manager.run_once()
                self.poll_errors = [409]
                self.state = 'succeeded'
                with patch('src.cluster_control.ops_management.asyncio.sleep', new_callable=AsyncMock):
                    await self.manager.run_once()
                self.assertEqual(len(self.posts('jobs.status')), 2)
                await self.finish_verification()
                record = self.store.get_operation(proposal['operation']['operation_id'])
                self.assertEqual(record['status'], 'succeeded')
                self.assertEqual(len(self.posts('units.restart')), 1)
                self.assertTrue(all(json.loads(request.content)['params'] == {'job_id': 'job_test'}
                                    for request in self.posts('jobs.status')))

    async def test_receipt_forbidden_is_not_retried(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        self.poll_errors = [403]
        await self.manager.run_once()
        self.assertEqual(len(self.posts('jobs.status')), 1)
        self.assertEqual(self.store.get_operation(proposal['operation']['operation_id'])['status'], 'needs_attention')

    async def test_receipt_cannot_confirm_another_job(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        self.response_job_id = 'unrelated_job'
        self.state = 'succeeded'
        await self.manager.run_once()
        self.assertEqual(self.store.get_operation(proposal['operation']['operation_id'])['status'], 'needs_attention')

    async def test_rechecked_receipt_preserves_actual_job_failure(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        self.poll_errors = [400]
        self.state = 'failed'
        with patch('src.cluster_control.ops_management.asyncio.sleep', new_callable=AsyncMock):
            await self.manager.run_once()
        self.assertEqual(self.store.get_operation(proposal['operation']['operation_id'])['status'], 'failed')
        self.assertEqual(len(self.posts('units.restart')), 1)

    async def test_expired_receipt_observation_does_not_poll_or_replay(self):
        proposal = await self.propose()
        await self.approve(proposal)
        await self.manager.run_once()
        key = proposal['operation']['operation_id']
        self.store.records[key]['deadline_at'] = int(time.time()) - 1
        await self.manager.run_once()
        self.assertFalse(self.posts('jobs.status'))
        self.assertEqual(len(self.posts('units.restart')), 1)
        self.assertEqual(self.store.get_operation(key)['status'], 'needs_attention')

    async def test_admin_api_fails_closed_without_accounts_even_with_old_token(self):
        import nonebot
        nonebot.init()
        from fastapi import FastAPI
        from src.plugins.ai_chat.admin import AdminServices, register_admin
        for token, header, expected in [('', '', 503), ('secret', '', 503), ('secret', 'wrong', 503)]:
            app = FastAPI()
            register_admin(app, AdminServices(version='test', started_at=1), token=token)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/bot-admin/api/v1/fleet/operations/op_test/approve',
                    headers={'Authorization': f'Bearer {header}'}, json={'contract_hash': 'a' * 64, 'resource_version': 1})
                self.assertEqual(response.status_code, expected)


class IntentReceiptApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import nonebot
        from prometheus_client import CollectorRegistry
        from src.cluster_control.api import create_app
        with patch('nonebot.config.DotEnvSettingsSource._read_env_files', return_value={}):
            nonebot.init()
        from src.plugins.ai_chat.fleet_client import FleetControlClient

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.credential = 'synthetic-control-token-' + 'c' * 40
        token = Path(self.tmp.name) / 'token'
        token.write_text(self.credential)
        self.key = 'subagent:' + 'a' * 64
        self.path = '/v1/ops/intent-receipts/' + 'a' * 64
        self.proof = {'source': 'gaoji-control', 'historical': True, 'host_id': 'h610',
            'actor_id': 'admin:kenneth', 'origin_scope': 'group:123', 'intent_key': self.key,
            'operation': 'exec.run', 'params_hash': 'b' * 64, 'operation_id': 'op_' + 'd' * 32,
            'authorized_before_dispatch': False, 'recorded_status': 'awaiting_approval'}
        self.lookup = Mock(side_effect=lambda actor, origin, key:
            copy.deepcopy(self.proof) if (actor, origin, key) == ('admin:kenneth', 'group:123', self.key) else None)
        self.upstream = AsyncMock(side_effect=AssertionError('Receipt lookup must not contact upstream'))
        manager = OpsManagementService(SimpleNamespace(_request=self.upstream),
            SimpleNamespace(operation_provenance=self.lookup), hosts=('h610',), actors=('admin:kenneth', 'admin:other'))
        app = create_app(SimpleNamespace(metrics_registry=CollectorRegistry()), api_token_file=token, management=manager)
        self.client = FleetControlClient('http://control.test', token, transport=httpx.ASGITransport(app=app))
        self.addAsyncCleanup(self.client.close)

    def headers(self, path, *, actor='admin:kenneth', origin='group:123'):
        timestamp = str(int(time.time()))
        message = '\n'.join(('GET', path, actor, origin, timestamp, hashlib.sha256(b'').hexdigest())).encode()
        return {'Authorization': 'Bearer ' + self.credential, 'X-KC-Actor': actor, 'X-KC-Origin': origin,
                'X-KC-Time': timestamp,
                'X-KC-Signature': hmac.new(self.credential.encode(), message, hashlib.sha256).hexdigest()}

    async def get(self, path, **kwargs):
        return await self.client._client.get('http://control.test' + path, **kwargs)

    async def test_client_uses_signed_digest_path_and_bypasses_external_replay(self):
        from src.plugins.ai_chat.agent.external import active_external
        tracker = SimpleNamespace(request=AsyncMock(side_effect=AssertionError('No external replay')))
        context_token = active_external.set(tracker)
        try:
            for _ in range(2):
                result = await self.client.operation_receipt(self.key, actor='admin:kenneth', origin='group:123')
                self.assertEqual(result, self.proof)
        finally:
            active_external.reset(context_token)
        tracker.request.assert_not_awaited()
        self.lookup.assert_called_with('admin:kenneth', 'group:123', self.key)
        self.assertEqual(self.lookup.call_count, 2)
        self.upstream.assert_not_awaited()

    async def test_client_signs_mapped_admin_principal_without_new_approval(self):
        from src.plugins.ai_chat.fleet_authorization import FleetAuthorization
        account = Mock(return_value={'username': 'kenneth'})
        mobile = SimpleNamespace(executors={}, pollers=[], store=SimpleNamespace(account_for_qq=account),
                                 propose=AsyncMock(side_effect=AssertionError('No new approval')))
        self.client.authorization = FleetAuthorization(self.client, mobile)
        result = await self.client.operation_receipt(self.key, actor='qq:3526452465', origin='group:123')
        self.assertEqual(result, self.proof)
        account.assert_called_once_with('3526452465')
        self.lookup.assert_called_once_with('admin:kenneth', 'group:123', self.key)
        mobile.propose.assert_not_awaited()
        self.upstream.assert_not_awaited()

    async def test_signature_covers_digest_actor_and_origin(self):
        headers = self.headers(self.path)
        cases = [(self.path[:-1] + 'b', headers),
                 (self.path, {**headers, 'X-KC-Origin': 'group:other'}),
                 (self.path, {**headers, 'X-KC-Actor': 'admin:other'}),
                 (self.path, {'Authorization': 'Bearer ' + self.credential})]
        for path, signed in cases:
            with self.subTest(path=path, headers=list(signed)):
                self.assertEqual((await self.get(path, headers=signed)).status_code, 401)
        self.lookup.assert_not_called()

    async def test_exact_principal_key_and_read_only_route(self):
        for actor, origin, path, expected in (
            ('admin:other', 'group:123', self.path, 404),
            ('admin:kenneth', 'group:other', self.path, 404),
            ('admin:kenneth', 'group:123', self.path[:-1] + 'b', 404),
            ('qq:unknown', 'group:123', self.path, 403),
        ):
            with self.subTest(actor=actor, origin=origin, path=path):
                response = await self.get(path, headers=self.headers(path, actor=actor, origin=origin))
                self.assertEqual(response.status_code, expected)
                self.assertNotIn('params_hash', response.json())
        self.assertEqual((await self.client._client.post('http://control.test' + self.path)).status_code, 405)
        legacy = await self.get('/v1/ops/receipt', params={'intent_key': self.key})
        self.assertEqual(legacy.status_code, 404)
        self.upstream.assert_not_awaited()

    async def test_api_rejects_malformed_digest_before_lookup(self):
        for digest in ('a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, self.key):
            path = '/v1/ops/intent-receipts/' + digest
            with self.subTest(digest=digest):
                self.assertEqual((await self.get(path, headers=self.headers(path))).status_code, 422)
        self.lookup.assert_not_called()

    async def test_client_rejects_malformed_key_locally(self):
        from src.plugins.ai_chat.fleet_client import FleetControlError
        with patch.object(self.client, '_authorized_request', new_callable=AsyncMock) as request:
            for key in ('', 'a' * 64, 'subagent:' + 'A' * 64, self.key + '\n', self.key + '/extra', None):
                with self.subTest(key=key), self.assertRaises(FleetControlError) as caught:
                    await self.client.operation_receipt(key, actor='admin:kenneth', origin='group:123')
                self.assertEqual(caught.exception.code, 'invalid_request')
            request.assert_not_awaited()

    async def test_client_checks_response_origin_and_intent_but_not_qq_admin_mapping(self):
        from src.plugins.ai_chat.fleet_client import FleetControlError
        with patch.object(self.client, '_authorized_request', new_callable=AsyncMock) as request:
            request.return_value = self.proof
            self.assertEqual(await self.client.operation_receipt(self.key, actor='qq:3526452465', origin='group:123'), self.proof)
            request.assert_awaited_with('GET', self.path, actor='qq:3526452465', origin='group:123')
            for bad in ({}, {**self.proof, 'origin_scope': 'group:other'},
                        {**self.proof, 'intent_key': 'subagent:' + 'b' * 64}):
                with self.subTest(response=bad):
                    request.return_value = bad
                    with self.assertRaises(FleetControlError) as caught:
                        await self.client.operation_receipt(self.key, actor='qq:3526452465', origin='group:123')
                    self.assertEqual(caught.exception.code, 'invalid_response')


@unittest.skipUnless(os.getenv('TEST_OPS_POSTGRES_DSN'), 'isolated PostgreSQL test not configured')
class ManagementPostgresTests(unittest.TestCase):
    def test_migration_approval_and_fenced_recovery(self):
        import uuid
        from alembic import command
        from alembic.config import Config
        import psycopg
        from psycopg import sql
        from src.bot_storage.database import PostgresDatabase
        from src.cluster_control.execution_storage import ClusterExecutionStore
        dsn = os.environ['TEST_OPS_POSTGRES_DSN']
        schema = 'gaoji_ops_test_' + uuid.uuid4().hex[:12]
        db = None
        with tempfile.TemporaryDirectory() as root:
            try:
                with patch.dict(os.environ, AI_POSTGRES_DSN=dsn, AI_POSTGRES_SCHEMA=schema):
                    command.upgrade(Config('alembic.ini'), 'head')
                db = PostgresDatabase(dsn, schema=schema, min_size=1, max_size=5)
                store = ClusterExecutionStore(db, Path(root))
                fake = SimpleNamespace(base_url='http://test', _credential=lambda: b'test')
                fake.backend_name = 'ops'
                fake.authorization_binding = lambda: {'url': fake.base_url, 'identity': fake._credential().hex()}
                manager = OpsManagementService(fake, store, hosts=('h610',), actors=('qq:3526452465', 'admin:kenneth'))
                async def definitions():
                    return [{'name': 'units.restart', 'read_only': False, 'kind': 'job_submission', 'idempotency': 'required', 'params_schema': {'type': 'object'}}]
                manager.definitions = definitions
                result = __import__('asyncio').run(manager.call('units.restart', {'host': 'h610', 'unit': 'test.service'}, actor='qq:3526452465', origin='test', idempotency_key='database-intent'))
                r = result['operation']
                self.assertIsNone(store.claim_managed_operation('worker'))
                store.approve_operation(r['operation_id'], actor_id='admin:kenneth', expected_hash=r['contract_hash'], expected_version=1, expires_at=int(time.time()) + 300)
                claim = store.claim_managed_operation('worker')
                self.assertEqual(claim['attempt'], 1)
                self.assertIsNone(store.claim_managed_operation('other'))
                self.assertFalse(store.finish_managed_operation(r['operation_id'], owner='other', fence=claim['fence'], status='succeeded', result={}, backend_id=None, error=''))
                with psycopg.connect(dsn) as conn:
                    conn.execute(sql.SQL('UPDATE {}.fleet_operations SET lease_expires_at=0 WHERE operation_id=%s').format(sql.Identifier(schema)), (r['operation_id'],))
                self.assertIsNone(store.claim_managed_operation('restarted'))
                self.assertEqual(store.get_operation(r['operation_id'])['status'], 'needs_attention')
                other_approval = store.get_operation(r['operation_id'])['approval_ref']
                intent_key = 'subagent:' + 'a' * 64
                params = {'host': 'h610', 'unit': 'test.service', 'env': {'TOKEN': 'synthetic-private-input'}}
                base = int(time.time())
                with patch('src.cluster_control.ops_management.time.time', return_value=base):
                    result = __import__('asyncio').run(manager.call('units.restart', params,
                        actor='qq:3526452465', origin='test', idempotency_key=intent_key))
                r = result['operation']

                def receipt():
                    return store.operation_provenance('qq:3526452465', 'test', intent_key)

                def snapshot():
                    with psycopg.connect(dsn) as conn:
                        return [conn.execute(sql.SQL('SELECT to_jsonb(t) FROM {}.{} t ORDER BY to_jsonb(t)::text')
                            .format(sql.Identifier(schema), sql.Identifier(table))).fetchall()
                            for table in ('fleet_operations', 'fleet_approvals', 'fleet_operation_events')]

                before = snapshot()
                proof = __import__('asyncio').run(manager.receipt(intent_key, actor='qq:3526452465', origin='test'))
                self.assertFalse(proof['authorized_before_dispatch'])
                self.assertEqual(proof['recorded_status'], 'awaiting_approval')
                self.assertIsNone(proof['backend_job_id'])
                self.assertEqual(proof['params_hash'], hashlib.sha256(canonical_json(params).encode()).hexdigest())
                self.assertEqual(proof['operation'], 'units.restart')
                self.assertEqual(proof['intent_key'], intent_key)
                self.assertNotIn('synthetic-private-input', json.dumps(proof))
                self.assertNotIn('params', proof)
                self.assertEqual(snapshot(), before)

                with patch('src.cluster_control.execution_storage.time.time', return_value=base + 10):
                    store.approve_operation(r['operation_id'], actor_id='admin:kenneth', expected_hash=r['contract_hash'],
                                            expected_version=1, expires_at=base + 300)
                self.assertFalse(receipt()['authorized_before_dispatch'])
                with patch('src.cluster_control.execution_storage.time.time', return_value=base + 11):
                    claim = store.claim_managed_operation('worker')
                with patch('src.cluster_control.execution_storage.time.time', return_value=base + 12):
                    self.assertTrue(store.finish_managed_operation(r['operation_id'], owner='worker', fence=claim['fence'],
                        status='failed', result={}, backend_id='job_provenance', error='timed_out'))
                before = snapshot()
                proof = receipt()
                self.assertTrue(proof['authorized_before_dispatch'])
                self.assertEqual(proof['recorded_status'], 'failed')
                self.assertTrue(proof['historical'])
                self.assertEqual(proof['created_at'], base)
                self.assertEqual(proof['dispatched_at'], base + 11)
                self.assertEqual(proof['status_recorded_at'], base + 12)
                self.assertEqual(proof['backend_job_id'], 'job_provenance')
                self.assertIsNone(store.operation_provenance('admin:kenneth', 'test', intent_key))
                self.assertIsNone(store.operation_provenance('qq:3526452465', 'other', intent_key))
                self.assertIsNone(store.operation_provenance('qq:3526452465', 'test', 'subagent:' + 'b' * 64))
                with patch('src.cluster_control.execution_storage.time.time', return_value=base + 10000):
                    self.assertEqual(receipt(), proof)
                self.assertEqual(snapshot(), before)

                def update(table, field, value):
                    with psycopg.connect(dsn) as conn:
                        conn.execute(sql.SQL('UPDATE {}.{} SET {}=%s WHERE operation_id=%s').format(
                            sql.Identifier(schema), sql.Identifier(table), sql.Identifier(field)), (value, r['operation_id']))

                for field, value in (('contract_hash', 'mismatch'), ('resource_version', 99),
                                     ('consumed_at', None), ('approved_at', base - 1),
                                     ('consumed_at', base + 12), ('expires_at', base + 11)):
                    with self.subTest(approval_field=field, value=value):
                        update('fleet_approvals', field, value)
                        self.assertFalse(receipt()['authorized_before_dispatch'])
                        update('fleet_approvals', field, proof['approval'][field])
                for reference in (None, other_approval):
                    update('fleet_operations', 'approval_ref', reference)
                    self.assertFalse(receipt()['authorized_before_dispatch'])
                    self.assertEqual(receipt()['approval'], {})
                update('fleet_operations', 'approval_ref', proof['approval']['approval_id'])
                tampered = {**r['arguments'], 'params': {**params, 'host': 'another-host'}}
                update('fleet_operations', 'arguments_json', canonical_json(tampered))
                self.assertFalse(receipt()['authorized_before_dispatch'])
                update('fleet_operations', 'arguments_json', canonical_json(r['arguments']))
                for field, value in (('backend_ref', 'other-backend'), ('operation', 'service.restart')):
                    update('fleet_operations', field, value)
                    self.assertIsNone(receipt())
                    update('fleet_operations', field, r[field])
                with psycopg.connect(dsn) as conn:
                    conn.execute(sql.SQL("UPDATE {}.fleet_operation_events SET event_type='not_dispatched' WHERE operation_id=%s AND event_type='dispatching'")
                                 .format(sql.Identifier(schema)), (r['operation_id'],))
                self.assertFalse(receipt()['authorized_before_dispatch'])
            finally:
                if db:
                    db.close()
                with psycopg.connect(dsn) as conn:
                    conn.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
