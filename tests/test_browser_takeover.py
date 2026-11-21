import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from src.plugins.ai_chat import browser_takeover as takeover
from src.plugins.ai_chat.adapters.qq_official.progress import ProgressEmitter
from src.plugins.ai_chat.application.owned_sandbox import OwnedSandboxExecutor
from src.plugins.ai_chat.message_ir import MessageBody
from src.plugins.ai_chat.platform_types import AccountKey, ActorKey, EndpointKey, InboundMessage, PlatformFeatures
from src.plugins.ai_chat.tools import neutral_registry_for
from src.plugins.ai_chat.tools.state import ToolContext, TurnState
from tests.test_owned_sandbox import Binding, FakeManager


ORIGIN = 'https://TEST.invalid'


class TakeoverToolTests(unittest.IsolatedAsyncioTestCase):
    def turn(self, kind, peer, sid):
        endpoint = EndpointKey(AccountKey('qq-official', 'TEST_APP', 'test'), kind, peer)
        actor = (ActorKey(endpoint.account, 'c2c_user', '', peer) if kind == 'private' else
                 ActorKey(endpoint.account, 'group_member', peer, 'TEST_MEMBER'))
        binding = Binding(endpoint=endpoint, actor=actor)
        manager = FakeManager()
        executor = OwnedSandboxExecutor(manager=manager, binding=binding, delivery=None, clock=lambda: 100)
        executor.generation_enabled = True
        manager.list = AsyncMock(return_value=[{'sandbox_id': sid, 'status': 'Up (container)'}])
        progress = Mock(spec=ProgressEmitter)
        progress.say = AsyncMock(return_value={'ok': True, 'status': 'enqueued'})
        inbound = InboundMessage(endpoint=endpoint, actor=actor, native_message_id='TEST_NATIVE', event_id=None,
            sender_display='', platform_role='', body=MessageBody(()), mentions_bot=False,
            reply_to_native_message_id=None, occurred_at=100, reply_credential=None)
        platform = SimpleNamespace(account=endpoint.account, features=PlatformFeatures(), progress=progress,
                                   progress_enabled=True, execution=SimpleNamespace(handle=SimpleNamespace(canonical_message_id=7)))
        app = SimpleNamespace(browser_takeovers=self.links, db_call=AsyncMock(side_effect=lambda fn,*args: fn(*args)),
                              settings=SimpleNamespace(admin_origin=ORIGIN, sandbox_operations_owners=set()))
        if kind == 'group':
            app.official_runtime = SimpleNamespace(store=MagicMock())
            app.official_runtime.store.authority.owner_for_actor.return_value = None
        return TurnState(ToolContext(SimpleNamespace(context=app), inbound=inbound, platform=platform,
                                     agent_executor=executor, capabilities=frozenset({'sandbox'})))

    def setUp(self):
        self.links = takeover.TakeoverLinks()

    async def test_member_cannot_claim_group_login_authority(self):
        turn = self.turn('group', 'TEST_GROUP', 's000003')
        with patch('src.plugins.ai_chat.tools.browser_takeover.source_role', return_value='member') as role:
            result = await neutral_registry_for(turn.context).execute(turn, 'browser_takeover', {'role': 'admin'})
        self.assertFalse(json.loads(result)['ok'])
        self.assertEqual(role.call_args.args[1:], (turn.context.inbound.endpoint, turn.context.inbound.actor, 7))
        turn.context.platform.progress.say.assert_not_awaited()
        self.assertFalse(self.links.pending)

    async def test_private_and_authorized_group_links_only_reach_the_bound_sandbox(self):
        for kind, peer, sid, role in [('private', 'TEST_A', 's000001', ''),
                ('group', 'TEST_A', 's000002', 'owner'), ('group', 'TEST_B', 's000003', 'admin'),
                ('group', 'TEST_C', 's000004', 'member')]:
            turn = self.turn(kind, peer, sid)
            if role == 'member':
                turn.context.app.official_runtime.store.authority.owner_for_actor.return_value = 'TEST_HOST_OWNER'
            registry = neutral_registry_for(turn.context)
            self.assertIn('browser_takeover', [d['function']['name'] for d in registry.definitions(turn.context)])
            with patch('src.plugins.ai_chat.tools.browser_takeover.source_role', return_value=role):
                result = await registry.execute(turn, 'browser_takeover', {'sid': 's000009'})
            self.assertEqual(json.loads(result), {'ok': True, 'status': 'enqueued'})
            turn.agent_executor.sandbox_manager.list.assert_awaited_once_with(turn.agent_executor.owner)
            message, = turn.context.platform.progress.say.await_args.args
            if kind == 'group':
                self.assertIn('这个登录全群可用', message.splitlines()[0])
            prefix, token = message.splitlines()[-1].split('#')
            self.assertEqual(prefix, ORIGIN + '/browser-takeover')
            self.assertEqual(self.links.claim(token)[:2], (turn.agent_executor.owner, sid))
            self.assertIsNone(self.links.claim(token))

    async def test_current_tab_rechecks_owner_before_connecting_to_guest_loopback(self):
        manager = Mock(broker='/TEST_RUNTIME/broker.sock', io_lock=Mock(return_value=AsyncMock()),
                       _owned_running=AsyncMock(side_effect=PermissionError))
        client = MagicMock()
        client.__aenter__.return_value = client
        client.ws_connect = AsyncMock(return_value=AsyncMock())
        response = client.get.return_value.__aenter__.return_value
        response.raise_for_status = Mock()
        response.json.return_value = [{'type': 'worker'}, {'type': 'page', 'id': 'TEST_CURRENT',
            'webSocketDebuggerUrl': 'ws://TEST_OTHER.invalid/TEST'}, {'type': 'page', 'id': 'TEST_OLDER'}]
        with patch.object(takeover.aiohttp, 'ClientSession', return_value=client), \
                patch.object(takeover.aiohttp, 'UnixConnector') as connector:
            with self.assertRaises(PermissionError):
                async with takeover.current_tab(manager, 'TEST_WRONG', 's000001'):
                    self.fail('unowned browser')
            client.get.assert_not_called()
            manager._owned_running.side_effect = None
            async with takeover.current_tab(manager, 'TEST_OWNER', 's000001'):
                connector.assert_called_with(path='/TEST_RUNTIME/cdp-s000001.sock')
                manager._owned_running.assert_awaited_with('TEST_OWNER', 's000001')
                client.ws_connect.assert_awaited_once_with('ws://localhost/devtools/page/TEST_CURRENT')

    async def test_ops_namespace_refused_and_unclaimed_link_expires(self):
        for kind in ('group', 'private'):
            turn = self.turn(kind, 'TEST_OPS', 's000001')
            turn.context.app.settings.sandbox_operations_owners.add(turn.agent_executor.owner)
            self.assertFalse(json.loads(await neutral_registry_for(turn.context).execute(turn, 'browser_takeover', {}))['ok'])
            turn.context.platform.progress.say.assert_not_awaited()
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'time', return_value=100):
            token = self.links.issue('TEST_OWNER', 's000001')
        with patch.object(loop, 'time', return_value=700):
            self.assertIsNone(self.links.claim(token))


class ViewerTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        self.links = takeover.TakeoverLinks()
        takeover.register_takeover(app, SimpleNamespace(settings=SimpleNamespace(admin_origin=ORIGIN),
            browser_takeovers=self.links, sandbox_manager='TEST_MANAGER'))
        self.client = self.enterContext(TestClient(app))
        self.token = self.client.portal.call(self.links.issue, 'TEST_OWNER', 's000001')
        self.commands = []

        @asynccontextmanager
        async def tab(manager, owner, sid):
            self.assertEqual((manager, owner, sid), ('TEST_MANAGER', 'TEST_OWNER', 's000001'))
            async def frames():
                frame = {'data': 'TEST_IMAGE', 'metadata': {}, 'sessionId': 71}
                yield SimpleNamespace(json=lambda: {'method': 'Page.screencastFrame', 'params': frame})
                await asyncio.Future()
            cdp = Mock()
            cdp.__aiter__ = lambda _: frames()
            cdp.send_json = AsyncMock(side_effect=self.commands.append)
            yield cdp
        self.enterContext(patch.object(takeover, 'current_tab', tab))

    def connect(self, origin=ORIGIN):
        return self.client.websocket_connect('/browser-takeover/live?sid=s000002', headers={'Origin': origin})

    def test_preview_does_not_claim_link_and_completed_session_cannot_be_reopened(self):
        self.assertEqual(self.client.get('/browser-takeover').headers['cache-control'], 'no-store')
        with self.assertRaises(WebSocketDisconnect), self.connect('https://TEST_OTHER.invalid'):
            pass
        with self.connect() as ws:
            ws.send_text(self.token)
            self.assertEqual(ws.receive_json()['image'], 'TEST_IMAGE')
            for kind in ('mousePressed', 'mouseMoved', 'mouseReleased'):
                ws.send_json({'type': kind, 'x': 15, 'y': 42})
            ws.send_json({'type': 'done'})
            self.assertEqual(ws.receive()['code'], 1000)
        self.assertEqual([c['method'] for c in self.commands], ['Page.startScreencast', 'Page.screencastFrameAck',
                         *['Input.dispatchMouseEvent'] * 3])
        self.assertEqual(self.commands[1]['params'], {'sessionId': 71})
        drag = [cmd['params'] for cmd in self.commands[2:]]
        self.assertEqual([(e['type'], e['button'], e['buttons']) for e in drag],
                         [('mousePressed', 'left', 1), ('mouseMoved', 'left', 1), ('mouseReleased', 'left', 0)])
        with self.connect() as ws:
            ws.send_text(self.token)
            self.assertEqual(ws.receive()['code'], 4403)

    def test_deadline_also_ends_an_open_stream(self):
        owner, sid, deadline = self.links.pending[self.token]
        self.links.pending[self.token] = owner, sid, deadline - 599.5
        with self.connect() as ws:
            ws.send_text(self.token)
            self.assertEqual(ws.receive_json()['image'], 'TEST_IMAGE')
            self.assertEqual(ws.receive()['code'], 4408)

    def test_viewer_only_forwards_input_not_arbitrary_cdp(self):
        inputs = [({'type': 'text', 'text': 'TEST_文本'}, 'Input.insertText'),
                  ({'type': 'keyDown', 'key': 'Enter', 'keyCode': 13}, 'Input.dispatchKeyEvent'),
                  ({'type': 'wheel', 'x': 4, 'y': 3, 'dx': 0, 'dy': 1}, 'Input.dispatchMouseEvent')]
        for event, method in inputs:
            self.assertEqual(takeover.input_command(event)[0], method)
        with self.connect() as ws:
            ws.send_text(self.token)
            ws.receive_json()
            ws.send_json({'type': 'Runtime.evaluate'})
            self.assertEqual(ws.receive()['code'], 1011)
        self.assertNotIn('Runtime.evaluate', [c['method'] for c in self.commands])

    def test_viewer_scales_and_sends_mouse_drags_for_all_pointer_types(self):
        script = Path(takeover.__file__).with_suffix('.html').read_text().split('<script>')[1].split('</script>')[0]
        dom = """
            const handlers = {}, events = [];
            const document = {getElementById: () => ({
                addEventListener: (name, fn) => handlers[name] = fn, setPointerCapture() {}, focus() {},
                getBoundingClientRect: () => ({left:10, top:20, width:320, height:220})
            })}, location = {hash:'', pathname:''}, history = {replaceState() {}};
        """
        drag = """
            viewport = {deviceWidth:1280, deviceHeight:880, pageScaleFactor:2, offsetTop:40};
            ws = {send: data => events.push(JSON.parse(data))};
            for (const pointerType of ['touch', 'pen', 'mouse']) {
                const e = {pointerType, pointerId:1, button:0, clientX:170, clientY:140, preventDefault() {}};
                handlers.pointerdown(e); handlers.pointermove(e);
                handlers[pointerType === 'pen' ? 'pointercancel' : 'pointerup'](e);
                handlers.pointermove(e);
            }
            console.log(JSON.stringify(events));
        """
        result = subprocess.run(['node', '-e', dom + script + drag], capture_output=True, check=True, text=True)
        events = json.loads(result.stdout)
        self.assertEqual([e['type'] for e in events], ['mousePressed', 'mouseMoved', 'mouseReleased'] * 3)
        self.assertAlmostEqual(events[1]['x'], 320)
        self.assertAlmostEqual(events[1]['y'], 220)
