"""Service inventory through the real control API; SSH and systemctl are replaced."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock,patch
import unittest

import httpx
from src import ssh_operations as target
from src.cluster_control.adapters.ssh import SSHOperationsClient
from src.cluster_control.api import create_app
from src.cluster_control.service import FleetControlService
from src.plugins.ai_chat.ops_read import request_path


class OpsObservationTests(unittest.TestCase):
    def test_version_is_observed_metadata_and_missing_fields_stay_unknown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = target.deployment_version(root)
            self.assertIsNone(result['system_closure'])
            self.assertEqual(set(result['unknown_version_fields']),
                {'system_closure','configuration_revision','os_version','os_build'})
            (root/'etc').mkdir()
            (root/'etc/os-release').write_text('VERSION_ID="TEST_VERSION"\nBUILD_ID=TEST_BUILD\nSECRET=TEST_NOT_RETURNED\n')
            closure = root/'nix/store/TEST_SYSTEM'; closure.mkdir(parents=True)
            (root/'run').mkdir(); (root/'run/current-system').symlink_to(closure)
            (closure/'configuration-revision').write_text('TEST_ACTUAL_REVISION\n')
            result = target.deployment_version(root)
            self.assertEqual(result['system_closure'],str(closure.resolve()))
            self.assertEqual(result['configuration_revision'],'TEST_ACTUAL_REVISION')
            self.assertEqual(result['os_version'],'TEST_VERSION'); self.assertEqual(result['os_build'],'TEST_BUILD')
            self.assertEqual(result['unknown_version_fields'],[])
            self.assertNotIn('TEST_NOT_RETURNED',str(result))
            (closure/'configuration-revision').write_text('x'*4097)
            self.assertIn('configuration_revision',target.deployment_version(root)['unknown_version_fields'])

    def test_service_list_is_fixed_read_only_and_explicitly_truncated(self):
        raw = '\n'.join(f'TEST_{i}.service loaded inactive dead TEST_DESCRIPTION' for i in range(201))
        with patch.object(target,'command',return_value=raw) as command:
            result = target.observation({'host_id':'TEST_HOST'},'units.list',{})
        self.assertTrue(result['truncated']); self.assertEqual(len(result['units']),200)
        self.assertEqual(result['coverage'],'loaded_services')
        self.assertEqual(result['units'][0]['active_state'],'inactive')
        self.assertEqual(command.call_args.args[1:],(
            'systemctl',['list-units','--all','--type=service','--no-legend','--plain','--no-pager']))
        self.assertEqual(request_path({'action':'services','host':'TEST_HOST'}),('/v1/hosts/TEST_HOST/units',{}))
        self.assertEqual(request_path({'action':'version','host':'TEST_HOST'}),('/v1/hosts/TEST_HOST',{}))


class OpsObservationAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        token = Path(temporary.name)/'token'; token.write_text('TEST_READ_ONLY_TOKEN_123456789_0123456789'); token.chmod(0o600)
        self.backend = SSHOperationsClient({'TEST_HOST':{'destination':'test@host.example','helper':'/TEST_HELPER'}},
            known_hosts_file='/TEST_KEYS',writable=False)
        self.policy = {'host_id':'TEST_HOST','observe':True,'readable_units':['TEST_VISIBLE.service']}
        self.service = FleetControlService(self.backend,inventory=(self.policy,))
        self.addAsyncCleanup(self.service.close)
        self.backend._remote = AsyncMock(return_value={'host':'TEST_HOST','units':[
            {'unit':'TEST_VISIBLE.service','active_state':'active'},
            {'unit':'TEST_HIDDEN.service','active_state':'active'}],'truncated':False,'coverage':'loaded_services'})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.service,api_token_file=token)),
            base_url='https://control.example',headers={'Authorization':'Bearer TEST_READ_ONLY_TOKEN_123456789_0123456789'})
        self.addAsyncCleanup(self.client.aclose)

    async def test_authenticated_list_filters_inventory_and_rechecks_reduced_policy(self):
        result = await self.client.get('/v1/hosts/TEST_HOST/units')
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual([row['unit'] for row in result.json()['data']['units']],['TEST_VISIBLE.service'])
        self.assertNotIn('TEST_HIDDEN',result.text)
        self.assertEqual(self.backend._remote.await_args.args[1]['op'],'units.list')
        self.policy['readable_units'] = []
        result = await self.client.get('/v1/hosts/TEST_HOST/units')
        self.assertEqual(result.json()['data']['units'],[])
        self.assertEqual(self.backend._remote.await_count,2)
        result = await self.client.get('/v1/hosts/TEST_HOST/units',headers={'Authorization':'Bearer TEST_BAD'})
        self.assertEqual(result.status_code,401)
        result = await self.client.get('/v1/hosts/TEST_OTHER/units')
        self.assertEqual(result.json()['status'],'forbidden')
        self.assertEqual(self.backend._remote.await_count,2)

    async def test_missing_capability_and_wrong_host_are_not_empty_success(self):
        self.backend._remote.return_value = {'host':'TEST_OTHER','units':[],'truncated':False}
        result = await self.client.get('/v1/hosts/TEST_HOST/units')
        self.assertNotEqual(result.json()['status'],'fresh')
        self.backend.operations = AsyncMock(return_value=())
        result = await self.client.get('/v1/hosts/TEST_HOST/units')
        self.assertEqual(result.json()['status'],'unsupported')
