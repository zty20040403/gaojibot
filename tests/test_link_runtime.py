"""Actual account factory/gateway/TxA/RestClient against loopback, not real QQ."""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone
from unittest.mock import patch

from src.plugins.ai_chat.adapters.qq_official.sender import OfficialSender
from src.qq_official.testing.fake_platform import FakePlatform
from tests import test_qq_official_runtime as fixture


class LinkRuntimeTests(fixture.OfficialRuntimeTests):
    async def start_account(self,fake):
        loaded = replace(fixture.loaded_config(fake.app_id,base=fake.base_url),owner_openids=('TEST_RUNTIME_OWNER','TEST_TARGET_OWNER'))
        runtime = self.runtime(loaded=loaded); await runtime.start(); await asyncio.wait_for(fake.wait_ready(),5)
        await self.wait_state(runtime,'running'); self.assertIsNotNone(runtime.link_service)
        return runtime

    async def emit(self,fake,text='/link',actor='TEST_RUNTIME_OWNER'):
        counter = getattr(self,'counter',0)+1; self.counter = counter; native = 'TEST_LINK_RUNTIME_'+str(counter)
        await fake.emit('C2C_MESSAGE_CREATE',dict(id=native,author=dict(user_openid=actor),content=text,timestamp=datetime.now(timezone.utc).isoformat()))
        await self.wait_db("SELECT * FROM qq_official_inbound_events WHERE native_message_id=? AND status='processed'",(native,))
        return native

    async def delivered(self,native,state='committed'):
        return await self.wait_db('SELECT * FROM deliveries WHERE reply_to_native_message_id=? AND status=?',(native,state),timeout=8)

    async def test_default_factory_real_private_pair_management_is_body_free_and_directional(self):
        async with FakePlatform() as fake:
            runtime = await self.start_account(fake)
            with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=321987):
                source = await self.emit(fake); await self.delivered(source)
            self.assertIn('/link 321987',fake.received[0]['payload']['content'])
            target = await self.emit(fake,'/link 321987','TEST_TARGET_OWNER'); await self.delivered(target)
            edge = (await self.wait_db("SELECT * FROM qq_official_conversation_links WHERE state='active'"))[0]
            listed = await self.emit(fake,'/link list'); await self.delivered(listed)
            self.assertIn('→',fake.received[-1]['payload']['content']); self.assertNotIn('TEST_TARGET_OWNER',fake.received[-1]['payload']['content'])
            revoked = await self.emit(fake,'/unlink '+edge['link_id'],'TEST_TARGET_OWNER'); await self.delivered(revoked)
            self.assertEqual(self.query('SELECT state,generation FROM qq_official_conversation_links'),[dict(state='revoked',generation=2)])
            self.assertEqual({row['binding_state'] for row in self.query('SELECT binding_state FROM qq_official_endpoints')},{'unbound'})
            for table in ('qq_official_inbound_events','delivery_plans','deliveries','delivery_attempts','messages'):
                data = repr(self.query('SELECT * FROM '+table)); self.assertNotIn('321987',data); self.assertNotIn('/link list',data)
            self.assertEqual(self.query("SELECT count(*) AS n FROM messages WHERE direction='inbound'"),[dict(n=0)])
            links = runtime.link_service; await runtime.stop()
            self.assertTrue(links.closing); self.assertEqual(links.tasks,set()); self.assertIsNone(runtime.link_service)

    async def test_claim_then_owner_revocation_prevents_http_in_real_runtime(self):
        async with FakePlatform() as fake:
            runtime = await self.start_account(fake); original = runtime.outbox.prepare
            def prepare(*args,**kwargs):
                reservation = original(*args,**kwargs); runtime.store.apply_configuration(runtime._token,()); return reservation
            with patch.object(runtime.outbox,'prepare',side_effect=prepare):
                native = await self.emit(fake); await self.delivered(native,'failed')
            self.assertEqual(fake.received,[]); self.assertEqual(self.query('SELECT state,code_hmac FROM qq_official_link_challenges'),[dict(state='cancelled',code_hmac='')])
            await runtime.stop()

    async def test_busy_closes_unissued_code_and_monitor_expires_without_replay(self):
        async with FakePlatform() as fake:
            runtime = await self.start_account(fake); links = runtime.link_service; release = asyncio.Event()
            blockers = {asyncio.create_task(release.wait()) for _ in range(32)}; links.tasks.update(blockers)
            try:
                await self.emit(fake); await self.wait_db("SELECT * FROM qq_official_link_challenges WHERE state='cancelled'")
                self.assertEqual(fake.received,[]); self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])
            finally:
                release.set(); await asyncio.gather(*blockers); links.tasks.difference_update(blockers)
            entered,unblock = asyncio.Event(),asyncio.Event()
            async def paused(*args):entered.set(); await unblock.wait(); return 'failed'
            try:
                with patch.object(runtime.sender,'emit_ephemeral',side_effect=paused):
                    await self.emit(fake); await asyncio.wait_for(entered.wait(),5)
                    row = self.query("SELECT * FROM qq_official_link_challenges WHERE state='sending'")[0]
                    links.ingress.authority.clock = lambda:row['expires_at']; links.next_sweep = 0
                    await links.maintain(); unblock.set()
                    await self.wait_db("SELECT * FROM qq_official_link_challenges WHERE state='expired' AND code_hmac=''")
            finally:unblock.set()
            self.assertEqual(fake.received,[]); await runtime.stop()

    async def test_shutdown_closes_unused_issued_code_and_restart_never_redeems_it(self):
        async with FakePlatform() as fake:
            first = await self.start_account(fake)
            with patch('src.plugins.ai_chat.adapters.qq_official.linking.secrets.randbelow',return_value=321987):
                native = await self.emit(fake); await self.delivered(native)
            self.assertEqual(self.query('SELECT state FROM qq_official_link_challenges'),[dict(state='issued')])
            await first.stop()
            self.assertEqual(self.query('SELECT state,code_hmac,delivery_state FROM qq_official_link_challenges'),
                [dict(state='cancelled',code_hmac='',delivery_state='committed')])
            second = await self.start_account(fake); confirmation = await self.emit(fake,'/link 321987','TEST_TARGET_OWNER')
            await self.delivered(confirmation); self.assertEqual(self.query('SELECT * FROM qq_official_conversation_links'),[])
            self.assertEqual(len(fake.received),2); await second.stop()

    async def test_shutdown_before_ephemeral_prepare_cancels_and_drains_account_work(self):
        async with FakePlatform() as fake:
            runtime = await self.start_account(fake); links = runtime.link_service; entered = asyncio.Event()
            async def pause(*args):entered.set(); await asyncio.Event().wait()
            with patch.object(runtime.sender,'emit_ephemeral',side_effect=pause):
                await self.emit(fake); await asyncio.wait_for(entered.wait(),5); await runtime.stop()
            self.assertEqual(links.tasks,set()); self.assertTrue(links.closing); self.assertIsNone(runtime.link_service)
            self.assertEqual(self.query('SELECT state,code_hmac FROM qq_official_link_challenges'),[dict(state='cancelled',code_hmac='')])
            self.assertEqual(fake.received,[]); self.assertEqual(self.query('SELECT * FROM delivery_plans'),[])

    async def test_restart_applies_actual_old_receipt_as_fact_without_restoring_code(self):
        async with FakePlatform() as fake:
            captured = asyncio.Event(); never = asyncio.Event(); first = None
            async def dormant(sender):await never.wait()
            async def capture_only(sender,prepare,*args):
                ref = await first._calls(prepare,*args); delivery = ref.delivery_ids[0]
                row = await first._calls(sender.outbox.get,sender.token,delivery)
                reservation = await first._calls(sender.outbox.prepare,sender.token,delivery,send_mode='passive',msg_id=row['reply_to_native_message_id'],msg_seq=1)
                response = await sender.rest.send_message(reservation.endpoint.kind,reservation.endpoint.native_conversation_id,reservation.payload())
                await first._calls(sender.outbox.capture_receipt,reservation,response); captured.set(); return 'unknown'
            with patch.object(OfficialSender,'run',dormant),patch.object(OfficialSender,'emit_ephemeral',capture_only):
                first = await self.start_account(fake); await self.emit(fake); await asyncio.wait_for(captured.wait(),5)
                await self.wait_db("SELECT * FROM qq_official_link_challenges WHERE state='cancelled'")
                self.assertIsNone(self.query('SELECT applied_at FROM delivery_receipts')[0]['applied_at'])
                await first.stop()
            second = await self.start_account(fake)
            await self.wait_db('SELECT * FROM delivery_receipts WHERE applied_at IS NOT NULL')
            row = self.query('SELECT state,delivery_state,delivery_receipt_hash,code_hmac FROM qq_official_link_challenges')[0]
            self.assertEqual((row['state'],row['delivery_state'],row['code_hmac']),('cancelled','committed','')); self.assertTrue(row['delivery_receipt_hash'])
            self.assertEqual(len(fake.received),1); self.assertEqual(self.query('SELECT * FROM qq_official_conversation_links'),[])
            await second.stop()


for name in dir(fixture.OfficialRuntimeTests):
    if name.startswith('test_') and name not in LinkRuntimeTests.__dict__:setattr(LinkRuntimeTests,name,None)
