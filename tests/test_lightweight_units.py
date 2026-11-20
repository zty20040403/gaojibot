"""Tests for §4.6 lightweight persistent units (`gaoji units` + host resident cap & wake)."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.plugins.ai_chat import sandbox_gaoji as cli
from src.plugins.ai_chat import sandbox_gaoji_units as units_mod
from src.plugins.ai_chat.adapters.qq_official.background_delivery import present_wake
from src.plugins.ai_chat.adapters.qq_official.vm_lifecycle import MAX_RESIDENT_CONTAINERS
from tests import test_vm_lifecycle as vm_fixture
from tests.qq_official_outbox_fixture import OutboxFixture


class FakeUnitsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict, **_kwargs):
        self.calls.append((name, arguments))
        if name == 'units_keepalive':
            return 200, {'ok': True, 'said': f"kept {arguments['units']}"}
        return 200, {'ok': True, 'accepted': True}


class GuestUnitsTests(unittest.TestCase):
    def test_apply_installs_services_skips_links_and_collisions_and_removes_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdir = root / 'systemd'
            rdir = root / 'run_systemd'
            idir = root / 'installed'
            edir = root / 'etc_systemd'
            sdir.mkdir()
            edir.mkdir()

            (sdir / 'demo.service').write_text('[Unit]\nDescription=Demo\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n', 'utf-8')
            (sdir / 'link.service').symlink_to('/etc/passwd')
            (edir / 'sshd.service').write_text('', 'utf-8')
            (sdir / 'sshd.service').write_text('[Service]\nExecStart=/bin/true\n', 'utf-8')

            cmds: list[list[str]] = []

            def fake_run(argv: list[str], **_kw):
                cmds.append(argv)
                out = 'active\n' if argv[:2] == ['systemctl', 'is-active'] else ''
                return subprocess.CompletedProcess(argv, 0, out, '')

            client = FakeUnitsClient()
            status, payload = units_mod.apply_units(
                client,
                systemd_dir=sdir,
                runtime_dir=rdir,
                installed_dir=idir,
                etc_systemd=edir,
                lock_path=root / 'units.lock',
                run_cmd=fake_run,
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload['units'], [{'name': 'demo.service', 'state': 'active'}])
            self.assertEqual({item['name'] for item in payload['skipped']}, {'link.service', 'sshd.service'})
            self.assertEqual((rdir / 'demo.service.d' / 'gaoji.conf').read_text('utf-8'), '[Service]\nSlice=gaoji-units.slice\n')
            self.assertEqual(client.calls[-1], ('units_keepalive', {'units': 1}))

            # Delete demo.service and re-apply -> removed cleanly.
            (sdir / 'demo.service').unlink()
            _, payload2 = units_mod.apply_units(
                client,
                systemd_dir=sdir,
                runtime_dir=rdir,
                installed_dir=idir,
                etc_systemd=edir,
                lock_path=root / 'units.lock',
                run_cmd=fake_run,
            )
            self.assertEqual(payload2['removed'], ['demo.service'])
            self.assertFalse((rdir / 'demo.service').exists())
            self.assertEqual(client.calls[-1], ('units_keepalive', {'units': 0}))

    def test_start_ended_notify_and_explicit_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_root = root / 'units_run'
            out_dir = root / 'out'
            cmds: list[list[str]] = []

            def fake_run(argv: list[str], **_kw):
                cmds.append(argv)
                return subprocess.CompletedProcess(argv, 0, '', '')

            st, started = units_mod.start_command(
                command='echo hello && sleep 1',
                title='Build task',
                workdir=str(root),
                limit=300,
                unit='gaoji-cmd-test1',
                run_root=run_root,
                out_dir=out_dir,
                run_cmd=fake_run,
            )
            self.assertEqual(st, 200)
            self.assertEqual(started['unit'], 'gaoji-cmd-test1')
            self.assertTrue(any('systemd-run' in c[0] for c in cmds))

            (out_dir / 'gaoji-cmd-test1.txt').write_text('build ok\n', 'utf-8')
            with patch.dict('os.environ', {'SERVICE_RESULT': 'success', 'EXIT_CODE': 'exited', 'EXIT_STATUS': '0'}):
                units_mod.ended_hook('gaoji-cmd-test1', run_root=run_root, run_cmd=fake_run)
            self.assertTrue(any('gaoji-notify@gaoji-cmd-test1.service' in c for c in cmds))

            client = FakeUnitsClient()
            nst, _ = units_mod.notify_hook(client, 'gaoji-cmd-test1', run_root=run_root, out_dir=out_dir)
            self.assertEqual(nst, 200)
            self.assertFalse((run_root / 'gaoji-cmd-test1').exists())
            wake_name, wake_args = client.calls[-1]
            self.assertEqual(wake_name, 'units_wake')
            self.assertIn('[gaoji-cmd-test1 完成] Build task', wake_args['text'])
            self.assertIn('build ok', wake_args['text'])

            # Explicit stop suppresses notify wake.
            units_mod.start_command(
                command='sleep 100',
                workdir=str(root),
                unit='gaoji-cmd-test2',
                run_root=run_root,
                out_dir=out_dir,
                run_cmd=fake_run,
            )
            units_mod.stop_units('gaoji-cmd-test2', run_root=run_root, run_cmd=fake_run)
            with patch.dict('os.environ', {'SERVICE_RESULT': 'success', 'EXIT_CODE': 'killed', 'EXIT_STATUS': 'TERM'}):
                _, end_res = units_mod.ended_hook('gaoji-cmd-test2', run_root=run_root, run_cmd=fake_run)
            self.assertTrue(end_res.get('stopped'))
            self.assertFalse((run_root / 'gaoji-cmd-test2').exists())

    def test_cli_parses_units_and_wake_subcommands(self) -> None:
        parsed = cli.parser().parse_args(['units', 'apply', '--boot'])
        self.assertEqual((parsed.command, parsed.units_command, parsed.boot), ('units', 'apply', True))
        parsed_wake = cli.parser().parse_args(['wake', 'hello'])
        self.assertEqual((parsed_wake.command, parsed_wake.text), ('wake', 'hello'))


class HostResidentAndWakeTests(OutboxFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.plugins.ai_chat.adapters.qq_official.send_store import OfficialSendStore
        from src.plugins.ai_chat.delivery import DeliveryStore
        await vm_fixture.VMLifecycleTests.asyncSetUp(self)
        self.manager._conversation_locks = {}
        deliveries = DeliveryStore(self.database, recover_interrupted=False)
        self.addCleanup(deliveries.close)
        self.service.runtime.outbox = OfficialSendStore(
            deliveries, admission=lambda *_: True, clock=lambda: self.now
        )
        self.service.app.message_ledger = None
        self.service.app.conversations = None
        self.service.app.settings = SimpleNamespace(system_prompt='', style_examples='')

    async def test_resident_cap_12_and_idle_protection(self):
        scope = SimpleNamespace(
            identity=SimpleNamespace(endpoint=self.endpoint, sandbox_id='s123456'),
            current=AsyncMock(),
        )
        res = await self.lifecycle.keepalive(scope, 2)
        self.assertTrue(res['resident'])
        self.assertIn('s123456', self.lifecycle.resident_sandboxes)
        # Idle cleaner must NOT stop a resident container.
        self.assertFalse(await self.lifecycle.stopping(self.owner, 's123456', automatic=True))

        # Fill up to MAX_RESIDENT_CONTAINERS (12).
        for idx in range(1, MAX_RESIDENT_CONTAINERS):
            self.lifecycle.resident_sandboxes.add(f'sfill{idx:02d}')
        self.assertEqual(len(self.lifecycle.resident_sandboxes), MAX_RESIDENT_CONTAINERS)

        over_scope = SimpleNamespace(
            identity=SimpleNamespace(endpoint=self.endpoint, sandbox_id='sover99'),
            current=AsyncMock(),
        )
        over = await self.lifecycle.keepalive(over_scope, 1)
        self.assertFalse(over['resident'])
        self.assertIn(str(MAX_RESIDENT_CONTAINERS), over['said'])

        # Clearing units releases resident hold.
        cleared = await self.lifecycle.keepalive(scope, 0)
        self.assertFalse(cleared['resident'])
        self.assertNotIn('s123456', self.lifecycle.resident_sandboxes)

    async def test_present_wake_delivers_foreground_summary_to_outbox(self):
        with patch(
            'src.plugins.ai_chat.adapters.qq_official.background_delivery.ask_deepseek_with_tools',
            new=AsyncMock(return_value='后台命令跑完啦，结果一切正常。'),
        ), patch(
            'src.plugins.ai_chat.adapters.qq_official.background_delivery.turn_window',
            return_value=([], '', None),
        ):
            await present_wake(self.service, self.endpoint, '[gaoji-cmd-1 完成] $ make test\n退出码 0', unit='gaoji-cmd-1')
        plans = self.query("SELECT purpose, manifest_json FROM delivery_plans WHERE account_key=?", (self.account.key,))
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['purpose'], 'background_result')


if __name__ == '__main__':
    unittest.main()
