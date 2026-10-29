import json
import unittest

from src.plugins.ai_chat.adapters.qq_official.host_config import HostConfiguration, HostConfigurationError
from src.plugins.ai_chat.adapters.qq_official.native_media import CONTRACT
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey


class ImageConfigTests(unittest.TestCase):
    def raw(self, kinds=(1,4), contract=CONTRACT):
        account = AccountKey('qq-official', 'TEST_IMAGE_CONFIG', 'test')
        endpoint = EndpointKey(account, 'group', 'TEST_GROUP')
        return dict(version=1, account=account.key,
            onebot_accounts=[{'account':AccountKey('onebot-v11','12345','production').key,'deployment_digest':'a'*64}],
            files={'private_directory':'/TEST/unused/image', 'native_routes':[
                dict(endpoint=endpoint.key, endpoint_generation=1, route_epoch=1, capability_revision=1,
                    max_bytes=3500000, verified_at=10, expires_at=20, contract=contract, file_type=kind)
                for kind in kinds]})

    def test_same_endpoint_has_independent_file_and_image_evidence(self):
        config = HostConfiguration.parse(json.dumps(self.raw()))
        self.assertEqual(tuple(e.file_type for e in config.native_routes), (1,4))

    def test_duplicate_type_and_unknown_audio_video_are_rejected(self):
        for kinds in ((1,1), (2,), (3,), (True,)):
            with self.subTest(kinds=kinds), self.assertRaises(HostConfigurationError):
                HostConfiguration.parse(json.dumps(self.raw(kinds)))

    def test_old_contract_preserved_never_reinterpreted(self):
        config = HostConfiguration.parse(json.dumps(self.raw(contract='old-test-contract')))
        self.assertEqual({e.contract for e in config.native_routes}, {'old-test-contract'})
