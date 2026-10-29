from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from src.local_runtime.official_configuration import (platforms,official_settings,stage_credentials,
    read_credentials,runtime_environment)
from src.local_runtime.validation import InstanceError


class OfficialConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o700);(self.root/'config').mkdir(mode=0o700)
        self.pair={'app_id':'TEST_APP_A','app_secret':'TEST_SECRET_A'}

    def stage(self,**kw):return stage_credentials(self.root,environment='production',previous=None,supplied=self.pair,**kw)

    def test_default_legacy_and_strict_flags(self):
        self.assertEqual(platforms(),dict(onebot=True,qq_official=False))
        for invalid in ({},{'onebot':True,'qq_official':1},{'onebot':False,'qq_official':False},
                        {'onebot':True,'qq_official':False,'auto':True}):
            with self.subTest(value=invalid),self.assertRaises(InstanceError):platforms(invalid)

    def test_private_bundle_roundtrip_and_no_values_in_reference_or_repr(self):
        ref=self.stage();bundle=read_credentials(self.root,ref)
        self.assertEqual(bundle.credentials.app_id,self.pair['app_id'])
        self.assertEqual(bundle.credentials.app_secret,self.pair['app_secret'])
        self.assertEqual(len(ref),32);self.assertNotIn('TEST_',repr(bundle))
        for file in (self.root/'config'/'official-credentials'/ref).iterdir():
            self.assertEqual(file.stat().st_mode&0o777,0o600)
        self.assertFalse((self.root/'instance.json').exists())

    def test_retry_reuses_bundle_same_account_rotation_keeps_binding_pepper(self):
        ref=self.stage();first=read_credentials(self.root,ref)
        self.assertEqual(stage_credentials(self.root,environment='production',previous=ref,supplied=self.pair),ref)
        second_ref=stage_credentials(self.root,environment='production',previous=ref,supplied=self.pair|{'app_secret':'TEST_ROTATED'})
        self.assertNotEqual(second_ref,ref)
        self.assertEqual(read_credentials(self.root,second_ref).pepper,first.pepper)
        self.assertEqual(read_credentials(self.root,ref).credentials.app_secret,'TEST_SECRET_A')

    def test_new_account_or_environment_never_reuses_old_pepper(self):
        ref=self.stage();first=read_credentials(self.root,ref)
        changed=stage_credentials(self.root,environment='production',previous=ref,supplied=self.pair|{'app_id':'TEST_APP_B'})
        sandbox=stage_credentials(self.root,environment='sandbox',previous=ref,supplied=self.pair)
        self.assertNotEqual(read_credentials(self.root,changed).pepper,first.pepper)
        self.assertNotEqual(read_credentials(self.root,sandbox).pepper,first.pepper)
        with self.assertRaises(InstanceError):stage_credentials(self.root,environment='sandbox',previous=ref)

    def test_half_pair_invalid_identifier_and_world_readable_files_rejected(self):
        for value in ({'app_id':'TEST_A'},{'app_id':'TEST_A','app_secret':''},self.pair|{'extra':'x'},
                      self.pair|{'app_id':'bad\nvalue'}):
            with self.subTest(keys=tuple(value)),self.assertRaises(InstanceError):
                stage_credentials(self.root,environment='production',previous=None,supplied=value)
        ref=self.stage();path=self.root/'config'/'official-credentials'/ref/'app-secret';path.chmod(0o644)
        with self.assertRaises(InstanceError) as failure:read_credentials(self.root,ref)
        self.assertNotIn(str(self.root),str(failure.exception));self.assertNotIn('TEST_SECRET',str(failure.exception))

    def test_symlink_or_renderer_reference_cannot_select_another_path(self):
        ref=self.stage();path=self.root/'config'/'official-credentials'/ref/'app-secret'
        path.unlink();path.symlink_to(self.root/'not-present')
        with self.assertRaises(InstanceError):read_credentials(self.root,ref)
        for value in ('../outside','/tmp/file','a'*32+'/',True):
            with self.subTest(value=value),self.assertRaises(InstanceError):read_credentials(self.root,value)

    def test_failed_stage_does_not_replace_previous_or_leave_partial_bundle(self):
        old=self.stage();parent=self.root/'config'/'official-credentials'
        import src.local_runtime.official_configuration as candidate
        original=candidate._write;count=0
        def fail(path,data):
            nonlocal count
            count+=1
            if count==2:raise OSError('TEST_PRIVATE_DIAGNOSTIC')
            return original(path,data)
        with patch.object(candidate,'_write',fail),self.assertRaises(InstanceError) as failure:
            stage_credentials(self.root,environment='production',previous=old,supplied=self.pair|{'app_secret':'TEST_CHANGED'})
        self.assertEqual([x.name for x in parent.iterdir()],[old])
        self.assertEqual(read_credentials(self.root,old).credentials.app_secret,'TEST_SECRET_A')
        self.assertNotIn('TEST_PRIVATE',str(failure.exception))

    def test_environment_uses_file_paths_and_explicit_separate_admission_cost_limits(self):
        settings=dict(platforms={'onebot':False,'qq_official':True},official=official_settings())
        ref=self.stage()
        with self.assertRaises(InstanceError):runtime_environment(settings,ref,configured=True)
        settings['official']['exclusive_deployment_confirmed']=True
        result=runtime_environment(settings,ref,configured=True)
        self.assertEqual(result['AI_ONEBOT_ENABLED'],'false');self.assertEqual(result['AI_QQ_OFFICIAL_ENABLED'],'true')
        self.assertEqual(result['AI_QQ_OFFICIAL_PRIVATE_POLICY'],'owner_only')
        self.assertEqual(result['AI_QUOTA_DAILY_CALLS'],'100')
        self.assertNotIn('TEST_',json.dumps(result));self.assertNotIn('AI_QQ_OFFICIAL_PUBLIC',result)
        self.assertEqual(runtime_environment({},None,configured=False),{'AI_ONEBOT_ENABLED':'true','AI_QQ_OFFICIAL_ENABLED':'false'})

    def test_public_admission_does_not_disable_finite_budget_or_mutate_platform_service(self):
        config=official_settings();config['private_policy']='public'
        self.assertEqual(official_settings(config)['daily_calls'],100)
        for change in ({'daily_calls':0},{'daily_calls':True},{'daily_output_tokens':10**12},
                       {'private_allowed_openids':['x\x00y']},{'private_policy':'allowlist'}):
            with self.subTest(change=change),self.assertRaises(InstanceError):official_settings(config|change)

    def test_runtime_copy_is_revision_bound_read_only_input_and_idempotent(self):
        from src.local_runtime.official_configuration import materialize_runtime,runtime_name
        ref=self.stage();target=self.root/'runtime-config';target.mkdir(mode=0o700)
        name=materialize_runtime(self.root,reference=ref,revision=3,environment='production',target_root=target)
        self.assertEqual(name,runtime_name(ref,3))
        self.assertEqual(materialize_runtime(self.root,reference=ref,revision=3,environment='production',target_root=target),name)
        copied=target/name;host=json.loads((copied/'host.json').read_text())
        self.assertEqual(host['onebot_accounts'],[])
        self.assertEqual(set(x.name for x in copied.iterdir()),{'app-id','app-secret','bind-pepper','host.json'})
        self.assertEqual(copied.stat().st_mode&0o777,0o700)
        for child in copied.iterdir():self.assertEqual(child.stat().st_mode&0o777,0o600)
        (copied/'app-secret').write_text('TEST_TAMPERED')
        with self.assertRaises(InstanceError):materialize_runtime(self.root,reference=ref,revision=3,environment='production',target_root=target)
        self.assertEqual((copied/'app-secret').read_text(),'TEST_TAMPERED')
        self.assertEqual(read_credentials(self.root,ref).credentials.app_secret,'TEST_SECRET_A')
        fresh=materialize_runtime(self.root,reference=ref,revision=4,environment='production',target_root=target)
        self.assertNotEqual(name,fresh)

    def test_runtime_copy_refuses_environment_change_unsafe_root_and_symlink(self):
        from src.local_runtime.official_configuration import materialize_runtime,runtime_name
        ref=self.stage();target=self.root/'runtime-config';target.mkdir(mode=0o755)
        with self.assertRaises(InstanceError):materialize_runtime(self.root,reference=ref,revision=1,environment='production',target_root=target)
        target.chmod(0o700)
        with self.assertRaises(InstanceError):materialize_runtime(self.root,reference=ref,revision=1,environment='sandbox',target_root=target)
        (target/runtime_name(ref,1)).symlink_to(self.root)
        with self.assertRaises(InstanceError):materialize_runtime(self.root,reference=ref,revision=1,environment='production',target_root=target)
