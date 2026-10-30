import asyncio
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.plugins.ai_chat.adapters.qq_official.native_schedules import NativeSchedules, NativeScheduleDenied, integer, text
from src.plugins.ai_chat.adapters.qq_official.optional_capabilities import optional_operation, reaction_reward
from src.plugins.ai_chat.adapters.qq_official.proactive_delivery import score_and_deliver, ProactiveLifetime
from src.plugins.ai_chat.adapters.qq_official.recent_speakers import recent_speakers
from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey, ActorKey, EffectiveCapabilities, CapabilityEvidence
from src.plugins.ai_chat.proactive import ProactiveDecision

ACCOUNT=AccountKey('qq-official','TEST_NATIVE','test')
EP=EndpointKey(ACCOUNT,'group','TEST_GROUP')
ACTOR=ActorKey(ACCOUNT,'group_member','TEST_GROUP','TEST_OPENID')


class PureTests(unittest.TestCase):
    def test_integer_rejects_boolean_negative_nonfinite_and_bignum(self):
        for value in (True,-1,float('nan'),float('inf'),2**63):
            with self.subTest(value=value),self.assertRaises(NativeScheduleDenied):integer(value)

    def test_bounded_text_rejects_nul_and_utf8_overflow_without_truncation(self):
        for value in ('abc\x00','界界',None,' '):
            with self.subTest(value=value),self.assertRaises(NativeScheduleDenied):text(value,4)
        self.assertEqual(text('界',4),'界')


    def test_missing_policy_is_explicit_unavailable(self):
        domain=NativeSchedules(database=None,token=SimpleNamespace(account_key=ACCOUNT.key),jobs=None,usage=None,
            outbox=None,admission=None,clock=lambda:100)
        with self.assertRaisesRegex(NativeScheduleDenied,'schedule_policy_unavailable'):
            domain._approve(None,None,{})

    def test_unknown_optional_capabilities_never_enable(self):
        evidence=EffectiveCapabilities(EP,1,{})
        for op in ('recall_own','recall_member','mute_member','remove_member','member_join_notice','reaction_feedback','voice_output'):
            with self.subTest(op=op):
                self.assertEqual(optional_operation(op,effective=evidence,now=100,developer_approved=True,
                    group_authorized=True,local_operator=True,tool_policy=True).reason,'platform_contract_unverified')

    def test_even_platform_evidence_cannot_invent_transport(self):
        evidence=EffectiveCapabilities(EP,1,{'voice_output':CapabilityEvidence(EP,'available','TEST',90,110)})
        self.assertEqual(optional_operation('voice_output',effective=evidence,now=100,developer_approved=True,
                    group_authorized=True,local_operator=True,tool_policy=True).reason,'verified_transport_unavailable')

    def test_all_local_intersections_required_and_expiry_rechecked(self):
        evidence=EffectiveCapabilities(EP,1,{'recall_own':CapabilityEvidence(EP,'available','TEST',90,110)})
        for key in ('developer_approved','group_authorized','local_operator','tool_policy'):
            flags=dict(developer_approved=True,group_authorized=True,local_operator=True,tool_policy=True);flags[key]=False
            self.assertEqual(optional_operation('recall_own',effective=evidence,now=100,**flags).status,'denied')
        self.assertEqual(optional_operation('recall_own',effective=evidence,now=110).reason,'platform_contract_unverified')

    def test_missing_or_untrusted_reaction_never_negative_sample(self):
        for event in (None,{}, {'reaction':'dislike'}, {'reaction':'like'}):self.assertIsNone(reaction_reward(event))

    def test_recent_speakers_exact_endpoint_visibility_and_no_bot_identity(self):
        other=ActorKey(ACCOUNT,'group_member','OTHER_GROUP','TEST_OPENID')
        bot=ActorKey(ACCOUNT,'bot','','TEST_BOT')
        rows=[dict(sender_actor_key=a.key,sender_display='private display',occurred_at=t)
              for a,t in ((ACTOR,100),(other,99),(ACTOR,98),(bot,97))]
        seen=[]
        def execute(sql,args):seen.append((sql,args));return SimpleNamespace(fetchall=lambda:rows)
        result=recent_speakers(SimpleNamespace(execute=execute),endpoint=EP,scope_key=EP.scope.key,now=100,current=lambda c,e:True)
        self.assertEqual(len(result.speakers),1);self.assertEqual(result.speakers[0].message_count,2)
        self.assertEqual(result.scanned_message_limit,500);self.assertFalse(result.is_membership_list)
        self.assertNotIn('private display',repr(result));self.assertNotIn('TEST_OPENID',repr(result))
        self.assertIn('m.source_endpoint_key=?',seen[0][0]);self.assertIn('min_canonical_message_id',seen[0][0])
        self.assertIn(EP.key,seen[0][1])

    def test_recent_speakers_denied_before_sql(self):
        with self.assertRaises(PermissionError):
            recent_speakers(None,endpoint=EP,scope_key=EP.scope.key,now=100,current=lambda c,e:False)

    def test_sdk_free_clean_import(self):
        code="import src.plugins.ai_chat.adapters.qq_official.native_schedules, src.plugins.ai_chat.adapters.qq_official.recent_speakers, src.plugins.ai_chat.adapters.qq_official.optional_capabilities, src.plugins.ai_chat.adapters.qq_official.proactive_delivery;import sys;assert not any(n=='nonebot' or n.startswith('nonebot.') for n in sys.modules)"
        subprocess.run([sys.executable,'-c',code],check=True)


class ProactiveTests(unittest.IsolatedAsyncioTestCase):
    def ports(self,choice):
        return dict(source=object(),scorer=AsyncMock(return_value=ProactiveDecision(90,'reply',False)),
            current=AsyncMock(return_value=ProactiveLifetime(90,120)),send_policy=AsyncMock(return_value=choice),enqueue=AsyncMock(),now=lambda:100,threshold=60)

    async def test_scores_normally_without_window_and_discards_immediately(self):
        ports=self.ports(False)
        result=await score_and_deliver(**ports)
        self.assertEqual(result.status,'discarded');ports['scorer'].assert_awaited_once();ports['enqueue'].assert_not_awaited()


    def choice(self):
        return True

    async def test_existing_unsolicited_purpose_and_original_deadline(self):
        ports=self.ports(self.choice())
        self.assertEqual((await score_and_deliver(**ports)).status,'queued')
        self.assertEqual(ports['enqueue'].await_args.kwargs,dict(purpose='unsolicited',expires_at=120))



    async def test_source_loss_after_scoring_does_not_enqueue(self):
        ports=self.ports(self.choice());ports['current'].side_effect=[ProactiveLifetime(90,120),PermissionError('revoked')]
        with self.assertRaises(PermissionError):await score_and_deliver(**ports)
        ports['enqueue'].assert_not_awaited()

    async def test_cancelled_scorer_propagates(self):
        ports=self.ports(self.choice());ports['scorer'].side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):await score_and_deliver(**ports)
        ports['enqueue'].assert_not_awaited()

    async def test_slow_scorer_cannot_refresh_original_deadline(self):
        ports=self.ports(self.choice())
        clock=[100]
        ports['now']=lambda:clock[0]
        async def slow(source):clock[0]=121;return ProactiveDecision(99,'late',False)
        ports['scorer']=slow
        result=await score_and_deliver(**ports)
        self.assertEqual(result.reason,'source_expired');ports['enqueue'].assert_not_awaited();ports['send_policy'].assert_not_awaited()

    async def test_restart_old_source_is_not_scored_or_queued(self):
        ports=self.ports(self.choice());ports['now']=lambda:121
        result=await score_and_deliver(**ports)
        self.assertEqual(result.reason,'source_expired');ports['scorer'].assert_not_awaited();ports['enqueue'].assert_not_awaited()


class HostPolicyTests(unittest.TestCase):
    def test_normal_admitted_request_uses_deployment_policy_without_otp(self):
        from src.plugins.ai_chat.adapters.qq_official.native_schedule_policy import DeploymentSchedulePolicy,NativeScheduleConfiguration
        policy=DeploymentSchedulePolicy(NativeScheduleConfiguration(True,'TEST_POLICY',7,86400000))
        result=policy.approve(None,object(),{'kind':'routine','max_tokens':4096,'daily_tokens':16384,'daily_runs':4},100)
        self.assertEqual(result.authorization_until,86400000)
        self.assertEqual(result.revision,7)

    def test_user_end_contract_is_preserved_or_explicitly_rejected(self):
        from src.plugins.ai_chat.adapters.qq_official.native_schedule_policy import DeploymentSchedulePolicy,NativeScheduleConfiguration
        policy=DeploymentSchedulePolicy(NativeScheduleConfiguration(True,'TEST_POLICY',7,86400000))
        spec={'kind':'routine','max_tokens':4096,'daily_tokens':16384,'daily_runs':4,'authorization_until':80000000}
        self.assertEqual(policy.approve(None,None,spec,100).authorization_until,80000000)
        with self.assertRaises(NativeScheduleDenied):policy.approve(None,None,{**spec,'authorization_until':90000000},100)

    def test_disabled_factory_has_no_database_or_model_initialization(self):
        from src.plugins.ai_chat.adapters.qq_official.native_schedule_policy import build_native_schedules
        self.assertIsNone(build_native_schedules(configuration=None,database=None,token=None,jobs=None,
            usage=None,outbox=None,admission=None,clock=None,profile_for=None))
