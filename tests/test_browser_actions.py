"""Real temporary guest journals/directories; never signal the host browser."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

from src.plugins.ai_chat import sandbox_browser_actions as actions

BOOT = '11111111-1111-4111-8111-111111111111'


class BrowserActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.journal,self.profile = self.root/'journal',self.root/'chromium'
        self.profile.mkdir(); (self.profile/'TEST_COOKIE').write_text('TEST_OLD')
        self.request = dict(id='a'*64,boot=BOOT,expires=150,action='clear',domain='')
        self.stop = AsyncMock()
        patcher = patch.object(actions,'stop_browser',self.stop)
        patcher.start(); self.addCleanup(patcher.stop)

    async def execute(self, request=None, perform=None, boot=BOOT):
        return await actions.execute(request or self.request,
            perform or (lambda value:actions.clear_profile(value,profile=self.profile)),
            journal=self.journal,boot=boot,clock=lambda:100)

    async def test_receipt_replay_never_deletes_a_new_login_and_boot_change_is_refused(self):
        self.assertEqual(await self.execute(),{'cleared':True})
        self.assertFalse(self.profile.exists())
        self.profile.mkdir(); (self.profile/'TEST_COOKIE').write_text('TEST_NEW')
        self.assertEqual(await self.execute(),{'cleared':True})
        self.assertEqual((self.profile/'TEST_COOKIE').read_text(),'TEST_NEW')
        self.stop.assert_awaited_once()
        self.assertEqual(await self.execute(dict(self.request,expires=151)),{'error':'browser_request_conflict'})
        self.assertEqual(await self.execute(boot='22222222-2222-4222-8222-222222222222'),{'error':'browser_boot_changed'})
        self.assertEqual((self.profile/'TEST_COOKIE').read_text(),'TEST_NEW')

    async def test_cancelled_started_action_stays_unknown_and_is_never_retried(self):
        started = asyncio.Event()
        async def pending(value):
            started.set(); await asyncio.Future()
        perform = AsyncMock(side_effect=pending)
        task = asyncio.create_task(self.execute(perform=perform))
        await asyncio.wait_for(started.wait(),1)
        self.assertEqual(await self.execute(),{'error':'browser_busy'})
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await self.execute(perform=perform),{'error':'browser_action_unknown'})
        self.assertEqual(perform.await_count,1)
        self.assertEqual((self.profile/'TEST_COOKIE').read_text(),'TEST_OLD')

    async def test_errors_never_expose_secret_text_or_replay_a_partial_change(self):
        perform = AsyncMock(side_effect=RuntimeError('TEST_SECRET'))
        self.assertEqual(await self.execute(perform=perform),{'error':'browser_action_unknown'})
        self.assertNotIn('TEST_SECRET',next(self.journal.glob('*.json')).read_text())
        self.assertEqual(await self.execute(perform=perform),{'error':'browser_action_unknown'})
        self.assertEqual(perform.await_count,1)

    async def test_new_profile_after_rename_is_not_removed_by_old_cleanup(self):
        remove = actions.shutil.rmtree
        def recreate(trash):
            self.profile.mkdir(); (self.profile/'TEST_NEW').write_text('TEST_LATER_LOGIN')
            remove(trash)
        with patch.object(actions.shutil,'rmtree',side_effect=recreate):
            self.assertEqual(await self.execute(),{'cleared':True})
        self.assertEqual((self.profile/'TEST_NEW').read_text(),'TEST_LATER_LOGIN')
        self.assertFalse((self.profile/'TEST_COOKIE').exists())

    async def test_symlink_and_stuck_browser_never_remove_profile(self):
        target = self.root/'TEST_TARGET'; self.profile.rename(target); self.profile.symlink_to(target,target_is_directory=True)
        self.assertEqual(await self.execute(),{'error':'browser_action_unknown'})
        self.stop.assert_not_awaited(); self.assertTrue((target/'TEST_COOKIE').is_file())
        self.profile.unlink(); target.rename(self.profile)
        self.stop.side_effect = TimeoutError('TEST_STUCK_BROWSER')
        self.assertEqual(await self.execute(dict(self.request,id='b'*64)),{'error':'browser_action_unknown'})
        self.assertTrue((self.profile/'TEST_COOKIE').is_file())

    async def test_invalid_or_expired_request_has_no_effect(self):
        for request in (dict(self.request,expires=99),dict(self.request,expires=221),dict(self.request,expires=True),
                dict(self.request,id='../TEST'),dict(self.request,action='TEST_SHELL'),dict(self.request,boot='TEST_BOOT')):
            self.assertIn('error',await self.execute(request))
        self.stop.assert_not_awaited(); self.assertTrue((self.profile/'TEST_COOKIE').is_file())


class BrowserProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_pidfd_and_second_profile_check_prevent_signalling_reused_pid(self):
        for still_owned in (False,True):
            with patch.object(actions,'processes',side_effect=[[12345],[]]), \
                    patch.object(actions,'uses_profile',return_value=still_owned), \
                    patch.object(actions.os,'pidfd_open',create=True,return_value=42) as opened, \
                    patch.object(actions.signal,'pidfd_send_signal',create=True) as sent, \
                    patch.object(actions.os,'close') as closed:
                await actions.stop_browser(Path('/TEST_PROFILE'))
            opened.assert_called_once_with(12345); closed.assert_called_once_with(42)
            if still_owned:
                sent.assert_called_once_with(42,actions.signal.SIGTERM)
            else:
                sent.assert_not_called()
