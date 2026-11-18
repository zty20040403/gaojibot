"""The host relay swaps in its own model and key, and only for a live job token."""
import asyncio
import json
import logging
import secrets
import socket
import unittest

import httpx

from src.plugins.ai_chat.job_gateway import MAX_JOB_TOOL_BODY, JobGateway
from src.plugins.ai_chat.tool_images import ToolImage
from tests.test_llm_gateway import profile


def free_port():
    # Avoid the OS's ordinary ephemeral client range: a just-probed adjacent
    # port there can be consumed by our own HTTP requests before relay startup.
    # Production keeps its configured fixed ports; only loopback fixtures probe.
    for _ in range(200):
        port = 20000+2*secrets.randbelow(5000)
        with socket.socket() as first, socket.socket() as second:
            try:
                first.bind(('127.0.0.1',port))
                second.bind(('127.0.0.1',port+1))
            except OSError:
                continue
            return port
    raise RuntimeError('TEST relay port pair unavailable')


class JobGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []

        def upstream(request):
            self.seen.append((str(request.url), request.headers['authorization'], json.loads(request.content)))
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={'content-type': 'text/event-stream'})

        self.gateway = JobGateway(port=free_port(), upstream=lambda: profile('relay',base_url='http://upstream.test/v1',api_key='TEST_REAL_KEY'),
            model=lambda: ('TEST_HOST_MODEL', 'high'), logger=logging.getLogger(__name__),
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
        await self.gateway.start()
        self.addAsyncCleanup(self.gateway.close)
        self.base = f'http://127.0.0.1:{self.gateway.port}/v1'

    async def test_relay_needs_a_live_token_and_uses_host_model_and_key(self):
        token, _ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            denied = await client.post(self.base + '/responses', json={'model': 'x'},
                                       headers={'Authorization': 'Bearer TEST_WRONG'})
            self.assertEqual(denied.status_code, 401)
            reply = await client.post(self.base + '/responses', json={'model': 'TEST_VM_CHOICE', 'input': [],
                                      'reasoning': {'summary': 'auto'}}, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(reply.status_code, 200)
            self.assertIn(b'response.completed', reply.content)
            url, auth, body = self.seen[0]
            self.assertEqual(url, 'http://upstream.test/v1/responses')
            self.assertEqual(auth, 'Bearer TEST_REAL_KEY')
            self.assertEqual(body['model'], 'TEST_HOST_MODEL')
            self.assertEqual(body['reasoning'], {'summary': 'auto', 'effort': 'high'})
            self.gateway.revoke(token)
            revoked = await client.post(self.base + '/responses', json={}, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(revoked.status_code, 401)

    async def test_revoke_cancels_request_waiting_for_upstream_headers(self):
        entered,cancelled,release = asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def upstream(request):
            entered.set()
            try:
                await release.wait()
                return httpx.Response(200,content=b'TEST')
            finally:
                cancelled.set()
        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        token,_ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            task = asyncio.create_task(client.post(self.base+'/responses',json={},headers={'Authorization':'Bearer '+token}))
            try:
                await asyncio.wait_for(entered.wait(),2)
                self.gateway.revoke(token)
                await asyncio.wait_for(cancelled.wait(),2)
                self.assertEqual((await task).status_code,401)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_revoke_closes_upstream_stream_without_completion(self):
        entered,closed = asyncio.Event(),asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: TEST_PARTIAL\n\n'
                entered.set()
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200,stream=Stream(),headers={'content-type':'text/event-stream'})))
        token,_ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            task = asyncio.create_task(client.post(self.base+'/responses',json={},headers={'Authorization':'Bearer '+token}))
            try:
                await asyncio.wait_for(entered.wait(),2)
                self.gateway.revoke(token)
                await asyncio.wait_for(closed.wait(),2)
                self.assertNotIn(b'response.completed',(await task).content)
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_missing_upstream_is_reported_for_the_job(self):
        token, _ = self.gateway.open()
        def broken():
            raise RuntimeError('TEST no cliproxy profile')
        self.gateway.upstream = broken
        async with httpx.AsyncClient(trust_env=False) as client:
            reply = await client.post(self.base + '/responses', json={}, headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(reply.status_code, 503)
        self.assertIn('TEST no cliproxy profile', self.gateway.failure(token))
        self.gateway.revoke(token)
        self.assertEqual(self.gateway.failure(token), '')

    async def test_revoke_closes_tunnel_even_before_engine_waits_for_it(self):
        token,channel = self.gateway.open()
        reader,writer = await asyncio.open_connection('127.0.0.1',self.gateway.port+1)
        try:
            writer.write(token.encode()+b'\n')
            await writer.drain()
            await asyncio.wait_for(asyncio.shield(channel.connected),2)
            self.assertIsNone(channel.writer)
            self.gateway.revoke(token)
            self.assertEqual(await asyncio.wait_for(reader.read(),2),b'')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_tunnel_accepts_only_its_token_once(self):
        token, channel = self.gateway.open()
        _, stranger = await asyncio.open_connection('127.0.0.1', self.gateway.port + 1)
        stranger.write(b'TEST_WRONG\n')
        await stranger.drain()
        reader, writer = await asyncio.open_connection('127.0.0.1', self.gateway.port + 1)
        writer.write(token.encode() + b'\n')
        await writer.drain()
        await channel.wait(5)
        await channel.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
        self.assertEqual(json.loads(await reader.readline())['method'], 'initialize')
        writer.write(b'{"id": 1, "result": {}}\n')
        await writer.drain()
        self.assertEqual(await channel.receive(), {'id': 1, 'result': {}})
        self.assertFalse(channel.connected.cancelled())
        writer.close()
        stranger.close()

    async def test_job_tool_rejects_oversized_stream_before_reading_tail(self):
        from types import SimpleNamespace
        from starlette.requests import Request

        _, channel = self.gateway.open()
        channel.session = SimpleNamespace()
        chunks = [b'x' * (MAX_JOB_TOOL_BODY // 2 + 1),
                  b'y' * (MAX_JOB_TOOL_BODY // 2 + 1), b'unread tail']
        seen = []

        async def receive():
            index = len(seen)
            seen.append(index)
            return {'type': 'http.request', 'body': chunks[index], 'more_body': index < len(chunks) - 1}

        request = Request({'type': 'http', 'method': 'POST', 'path': '/job/tool',
                           'query_string': b'', 'headers': []}, receive)
        request.state.channel = channel
        response = await self.gateway._job_tool(request)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(seen, [0, 1])

    async def test_job_tool_disconnect_drains_cancelled_tool_cleanup(self):
        from types import SimpleNamespace
        from starlette.requests import Request

        _, channel = self.gateway.open()
        self.gateway.tool_heartbeat_seconds = 0.01
        cleaning = asyncio.Event()
        released = asyncio.Event()
        interrupted = asyncio.Event()
        finished = asyncio.Event()

        async def execute_tool(_name, _arguments):
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
                finished.set()

        channel.session = SimpleNamespace(execute_tool=execute_tool)
        async def receive():
            return {'type': 'http.request', 'body': b'{"name":"slow"}', 'more_body': False}
        request = Request({'type': 'http', 'method': 'POST', 'path': '/job/tool',
                           'query_string': b'', 'headers': []}, receive)
        request.state.channel = channel
        response = await self.gateway._job_tool(request)
        iterator = response.body_iterator
        self.assertEqual(await asyncio.wait_for(anext(iterator), 1), b'{"type":"heartbeat"}\n')
        closing = asyncio.create_task(iterator.aclose())
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            closing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(interrupted.is_set())
            self.assertFalse(closing.done())
        finally:
            released.set()
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 1)
        self.assertTrue(finished.is_set())

    async def test_job_tool_catalog_requires_current_job(self):
        from types import SimpleNamespace

        async def current():
            raise PermissionError('TEST stale job')

        token, channel = self.gateway.open(current=current)
        async def execute_tool(_name, _arguments):
            self.fail('a stale job must not execute tools')

        channel.session = SimpleNamespace(tools=(
            {'function': {'name': 'history', 'description': 'TEST_PRIVATE_TOOL'}},
        ), execute_tool=execute_tool)
        root = f'http://127.0.0.1:{self.gateway.port}'
        async with httpx.AsyncClient(trust_env=False) as client:
            for method, path in (('GET', '/job/tools'), ('POST', '/job/tool')):
                response = await client.request(method, root + path, json={'name': 'history'},
                                                headers={'Authorization': 'Bearer ' + token})
                self.assertEqual(response.status_code, 403)
                self.assertNotIn('TEST_PRIVATE_TOOL', response.text)

    async def test_job_tool_catalog_denies_actual_cancelled_authority(self):
        from types import SimpleNamespace

        async def current():
            raise asyncio.CancelledError  # BackgroundAgents.current uses this for stale jobs.

        token, channel = self.gateway.open(current=current)
        channel.session = SimpleNamespace(tools=({'function': {'name': 'history'}},))
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f'http://127.0.0.1:{self.gateway.port}/job/tools',
                                        headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 403)

    async def test_job_tool_does_not_return_result_after_job_loses_authority(self):
        await self._assert_current_required_for_tool_result(fail=False, cancelled=False)
        await self._assert_current_required_for_tool_result(fail=True, cancelled=False)
        await self._assert_current_required_for_tool_result(fail=False, cancelled=True)
        await self._assert_current_required_for_tool_result(fail=True, cancelled=True)
        await self._assert_current_required_for_tool_result(fail=False, cancelled=True, during_tool=True)

    async def _assert_current_required_for_tool_result(self, *, fail, cancelled, during_tool=False):
        from types import SimpleNamespace

        valid = True
        entered, release = asyncio.Event(), asyncio.Event()

        async def current():
            if not valid:
                if cancelled:
                    raise asyncio.CancelledError
                raise PermissionError('TEST job no longer current')

        async def execute_tool(_name, _arguments):
            await current()  # The real BackgroundToolSession checks before executing.
            entered.set()
            await release.wait()
            if during_tool:
                await current()
            if fail:
                raise RuntimeError('TEST_PRIVATE_HISTORY')
            return 'TEST_PRIVATE_HISTORY'

        token, channel = self.gateway.open(current=current)
        channel.session = SimpleNamespace(execute_tool=execute_tool)
        url = f'http://127.0.0.1:{self.gateway.port}/job/tool'
        async with httpx.AsyncClient(trust_env=False) as client:
            task = asyncio.create_task(client.post(url, json={'name': 'history'},
                                                  headers={'Authorization': 'Bearer ' + token}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                valid = False
                release.set()
                try:
                    response = await asyncio.wait_for(task, 2)
                except httpx.RemoteProtocolError:
                    self.fail('a stale job must receive a denied result, not a broken response')
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn('TEST_PRIVATE_HISTORY', response.text)
        result = json.loads(response.text.strip().splitlines()[-1])
        self.assertFalse(result['ok'])

    async def test_job_current_preserves_real_task_cancellation(self):
        entered = asyncio.Event()
        async def current():
            entered.set()
            await asyncio.Event().wait()

        _, channel = self.gateway.open(current=current)
        task = asyncio.create_task(self.gateway._job_current(channel))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_job_tool_routes_require_job_token_stream_heartbeats_and_handle_errors(self):
        from types import SimpleNamespace

        class FakeVmAuth:
            closed = False

            async def authorize(self, token, *, write=False):
                raise AssertionError('VM token must not authorize /job/tools or /job/tool')

            async def close(self):
                self.closed = True

        self.gateway.vm_authority = FakeVmAuth()
        self.gateway.tool_heartbeat_seconds = 0.05
        token, channel = self.gateway.open()
        cancelled = asyncio.Event()

        async def execute_tool(name, arguments):
            if name == 'slow':
                try:
                    await asyncio.sleep(0.14)
                    return {'answer': arguments.get('q', '')}
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            if name == 'image':
                return ToolImage('preview', 'data:image/jpeg;base64,QUJD')
            if name == 'boom':
                raise ValueError('bad arg')
            return 'plain text'

        channel.session = SimpleNamespace(
            tools=(
                {'function': {'name': 'slow', 'description': 'Slow tool', 'parameters': {'type': 'object'}}},
                {'function': {'name': 'image', 'description': 'Image tool', 'parameters': {'type': 'object'}}},
            ),
            execute_tool=execute_tool,
        )
        root_url = f'http://127.0.0.1:{self.gateway.port}'
        async with httpx.AsyncClient(trust_env=False) as client:
            for method, path in (('GET', '/job/tools'), ('POST', '/job/tool')):
                for authorization in ('', 'Bearer TEST_WRONG', 'Bearer gvm_secret'):
                    response = await client.request(method, root_url + path, json={'name': 'slow'},
                                                    headers={'Authorization': authorization})
                    self.assertEqual(response.status_code, 401)
            tools_res = await client.get(root_url + '/job/tools', headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(tools_res.status_code, 200)
            self.assertEqual([item['name'] for item in tools_res.json()['tools']], ['slow', 'image'])

            slow_res = await client.post(root_url + '/job/tool', json={'name': 'slow', 'arguments': {'q': 'hi'}},
                                         headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(slow_res.status_code, 200)
            lines = [json.loads(line) for line in slow_res.text.strip().splitlines()]
            self.assertGreaterEqual(sum(1 for item in lines if item['type'] == 'heartbeat'), 1)
            self.assertEqual(lines[-1], {
                'type': 'result',
                'ok': True,
                'content': [{'type': 'text', 'text': '{"answer": "hi"}'}],
            })

            img_res = await client.post(root_url + '/job/tool', json={'name': 'image', 'arguments': {}},
                                        headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(json.loads(img_res.text.strip()), {
                'type': 'result',
                'ok': True,
                'content': [
                    {'type': 'text', 'text': 'preview'},
                    {'type': 'image', 'data': 'QUJD', 'mimeType': 'image/jpeg'},
                ],
            })

            err_res = await client.post(root_url + '/job/tool', json={'name': 'boom', 'arguments': {}},
                                        headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(json.loads(err_res.text.strip()), {
                'type': 'result',
                'ok': False,
                'error': 'ValueError: bad arg',
            })

            # Revoking token mid-execution cancels the running tool task.
            task = asyncio.create_task(client.post(root_url + '/job/tool', json={'name': 'slow', 'arguments': {}},
                                                    headers={'Authorization': 'Bearer ' + token}))
            await asyncio.sleep(0.06)
            self.gateway.revoke(token)
            await asyncio.wait_for(cancelled.wait(), 2)
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_receipt_start_is_finished_after_revocation(self):
        entered, finished = asyncio.Event(), asyncio.Event()

        class Receipt:
            async def start(self):
                # Represents a receipt whose DB insert has committed, but whose
                # start coroutine has not yet returned to the gateway.
                entered.set()
                await asyncio.Event().wait()
            async def finish(self):
                finished.set()
        class Billing:
            def request(self, _profile):
                return Receipt()
        self.gateway.billing = lambda _channel: Billing()
        self.gateway.model = lambda: (['m1', 'm2'], 'high')
        token, _ = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            request = asyncio.create_task(client.post(
                self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.gateway.revoke(token)
                try:
                    await asyncio.wait_for(finished.wait(), 1)
                except TimeoutError:
                    self.fail('cancelled model receipt was not finished')
                self.assertEqual((await request).status_code, 401)
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    async def test_error_body_read_timeout_closes_receipt_and_tries_next_model(self):
        attempts, receipts = [], []
        closed = asyncio.Event()

        class Receipt:
            def __init__(self, model):
                self.model, self.finished = model, False
            async def start(self):
                pass
            def observe(self, _usage):
                pass
            async def finish(self):
                self.finished = True

        class Billing:
            def request(self, profile):
                receipt = Receipt(profile.model)
                receipts.append(receipt)
                return receipt

        class ErrorStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.ReadTimeout('error response stalled')
                yield b''
            async def aclose(self):
                closed.set()

        def upstream(request):
            model = json.loads(request.content)['model']
            attempts.append(model)
            if model == 'm1':
                return httpx.Response(404, stream=ErrorStream())
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={'content-type': 'text/event-stream'})

        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.gateway.model = lambda: (['m1', 'm2'], 'high')
        self.gateway.billing = lambda _channel: Billing()
        token, channel = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(self.base + '/responses', json={'input': []},
                                         headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, ['m1', 'm2'])
        self.assertTrue(closed.is_set())
        self.assertEqual(channel.model_index, 1)
        self.assertEqual([receipt.finished for receipt in receipts], [True, True])

    async def test_upstream_error_does_not_echo_host_key_to_guest(self):
        def upstream(request):
            self.assertEqual(request.headers['authorization'], 'Bearer TEST_REAL_KEY')
            return httpx.Response(400, content=b'context error: TEST_REAL_KEY')

        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        token, _ = self.gateway.open()
        with self.assertLogs(self.gateway.logger, level='WARNING') as logged:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(self.base + '/responses', json={'input': []},
                                             headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 400)
        self.assertIn('context error', response.text)
        self.assertNotIn('TEST_REAL_KEY', response.text)
        self.assertNotIn('TEST_REAL_KEY', self.gateway.failure(token))
        self.assertNotIn('TEST_REAL_KEY', '\n'.join(logged.output))

    async def test_fallback_reason_does_not_echo_host_key(self):
        def upstream(request):
            if json.loads(request.content)['model'] == 'm1':
                return httpx.Response(503, content=b'busy: TEST_REAL_KEY')
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={'content-type': 'text/event-stream'})

        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.gateway.model = lambda: (['m1', 'm2'], 'high')
        token, channel = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(self.base + '/responses', json={'input': []},
                                         headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(channel.fallbacks[0][:2], ('m1', 'm2'))
        self.assertNotIn('TEST_REAL_KEY', channel.fallbacks[0][2])

    async def test_cliproxy_model_not_found_400_code_triggers_fallback(self):
        attempts = []
        def upstream(request):
            model = json.loads(request.content)['model']
            attempts.append(model)
            if model == 'm1':
                return httpx.Response(
                    400,
                    content=b'{"error":{"code":"model_not_found","message":"unknown provider for model m1","param":"model","type":"invalid_request_error"}}',
                )
            return httpx.Response(
                200,
                content=b'data: {"type":"response.completed"}\n\n',
                headers={'content-type': 'text/event-stream'},
            )

        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.gateway.model = lambda: (['m1', 'm2'], 'high')
        token, channel = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                self.base + '/responses',
                json={'input': []},
                headers={'Authorization': 'Bearer ' + token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, ['m1', 'm2'])
        self.assertEqual(channel.fallbacks[0][:2], ('m1', 'm2'))

    async def test_redacting_key_does_not_change_fallback_classification(self):
        self.gateway.upstream = lambda: profile('relay', base_url='http://upstream.test/v1', api_key='TEST_MODEL')
        def upstream(request):
            if json.loads(request.content)['model'] == 'm1':
                return httpx.Response(400, content=b'unsupported TEST_MODEL')
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={'content-type': 'text/event-stream'})

        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.gateway.model = lambda: (['m1', 'm2'], 'high')
        token, channel = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(self.base + '/responses', json={'input': []},
                                         headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(channel.fallbacks[0][:2], ('m1', 'm2'))
        self.assertNotIn('TEST_MODEL', channel.fallbacks[0][2])

    async def test_transport_failure_reason_does_not_echo_host_key(self):
        def upstream(request):
            if json.loads(request.content)['model'] == 'm1':
                raise httpx.ConnectError('TEST_REAL_KEY connection failed', request=request)
            return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                  headers={'content-type': 'text/event-stream'})

        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.gateway.model = lambda: (['m1', 'm2'], 'high')
        token, channel = self.gateway.open()
        with self.assertLogs(self.gateway.logger, level='WARNING') as logged:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(self.base + '/responses', json={'input': []},
                                             headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('TEST_REAL_KEY', channel.fallbacks[0][2])
        self.assertNotIn('TEST_REAL_KEY', '\n'.join(logged.output))

    async def test_model_chain_fallbacks_billing_and_stream_failures(self):
        attempts = []
        receipts = []

        class FakeReceipt:
            def __init__(self, model_name):
                self.model_name = model_name
                self.started = False
                self.finished = False
                self.usage = None

            async def start(self):
                self.started = True

            def observe(self, usage):
                self.usage = usage

            async def finish(self):
                self.finished = True

        class FakeBilling:
            def request(self, prof):
                r = FakeReceipt(prof.model)
                receipts.append(r)
                return r

        mode = {'kind': '404_then_ok'}

        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"type":"response.created"}\n\n'
                raise httpx.ReadTimeout('stream stalled')

        async def upstream(request):
            body = json.loads(request.content)
            model = body['model']
            attempts.append(model)
            if mode['kind'] == '404_then_ok':
                if model == 'm1':
                    return httpx.Response(404, content=b'model not found')
                return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                      headers={'content-type': 'text/event-stream'})
            if mode['kind'] == '400_context':
                return httpx.Response(400, content=b'maximum context length exceeded')
            if mode['kind'] == '400_model':
                if model == 'm1':
                    return httpx.Response(400, content=b'model does not exist')
                return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                      headers={'content-type': 'text/event-stream'})
            if mode['kind'] == '400_demand':
                if model == 'm1':
                    return httpx.Response(400, content=b"We're currently experiencing high demand, which may cause temporary errors.")
                return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                      headers={'content-type': 'text/event-stream'})
            if mode['kind'] == 'sse_demand':
                if model == 'm1':
                    sse = (
                        b'data: {"type":"response.created"}\n\n'
                        b'data: {"type":"response.failed","response":{"error":{"code":"server_error",'
                        b'"message":"We\'re currently experiencing high demand, which may cause temporary errors."}}}\n\n'
                    )
                    return httpx.Response(200, content=sse, headers={'content-type': 'text/event-stream'})
                return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                      headers={'content-type': 'text/event-stream'})
            if mode['kind'] == 'all_fail':
                return httpx.Response(503, content=b'busy')
            if mode['kind'] == 'stream_break':
                if model == 'm1':
                    return httpx.Response(200, stream=BrokenStream(), headers={'content-type': 'text/event-stream'})
                return httpx.Response(200, content=b'data: {"type":"response.completed"}\n\n',
                                      headers={'content-type': 'text/event-stream'})
            raise AssertionError(mode['kind'])

        await self.gateway.client.aclose()
        self.gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.gateway.model = lambda: (['m1', 'm2', 'm3'], 'high')
        self.gateway.billing = lambda _ch: FakeBilling()

        # 1) 404 -> fallback to m2, next request starts from m2, failed receipt finished
        token, channel = self.gateway.open()
        async with httpx.AsyncClient(trust_env=False) as client:
            r1 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(attempts, ['m1', 'm2'])
            self.assertEqual(len(channel.fallbacks), 1)
            self.assertEqual(channel.fallbacks[0][:2], ('m1', 'm2'))
            self.assertEqual(channel.last_model, 'm2')
            self.assertTrue(receipts[0].finished)
            self.assertTrue(receipts[1].finished)

            attempts.clear()
            r2 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(attempts, ['m2'])

            # 2) 400 context length does NOT fallback
            attempts.clear()
            mode['kind'] = '400_context'
            token2, _ = self.gateway.open()
            r3 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token2})
            self.assertEqual(r3.status_code, 400)
            self.assertEqual(attempts, ['m1'])

            # 3) 400 model does not exist DOES fallback
            attempts.clear()
            mode['kind'] = '400_model'
            token3, _ = self.gateway.open()
            r4 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token3})
            self.assertEqual(r4.status_code, 200)
            self.assertEqual(attempts, ['m1', 'm2'])

            # 4) all fail -> 503
            attempts.clear()
            mode['kind'] = 'all_fail'
            token4, _ = self.gateway.open()
            r5 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token4})
            self.assertEqual(r5.status_code, 503)
            self.assertEqual(attempts, ['m1', 'm2', 'm3'])

            # 5) two consecutive stream failures advance model_index to m2
            attempts.clear()
            mode['kind'] = 'stream_break'
            token5, ch5 = self.gateway.open()
            for _ in range(2):
                with self.assertRaises(httpx.HTTPError):
                    await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token5})
            self.assertEqual(ch5.model_index, 1)
            attempts.clear()
            r6 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token5})
            self.assertEqual(r6.status_code, 200)
            self.assertEqual(attempts, ['m2'])

            # 6) 400 high demand falls back immediately; 200 SSE high demand advances model_index for retry
            attempts.clear()
            mode['kind'] = '400_demand'
            token6, _ = self.gateway.open()
            r7 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token6})
            self.assertEqual(r7.status_code, 200)
            self.assertEqual(attempts, ['m1', 'm2'])

            attempts.clear()
            mode['kind'] = 'sse_demand'
            token7, ch7 = self.gateway.open()
            r8 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token7})
            self.assertEqual(r8.status_code, 200)
            self.assertEqual(ch7.model_index, 1)
            self.assertIn('high demand', ch7.fallbacks[0][2])
            attempts.clear()
            r9 = await client.post(self.base + '/responses', json={'input': []}, headers={'Authorization': 'Bearer ' + token7})
            self.assertEqual(r9.status_code, 200)
            self.assertEqual(attempts, ['m2'])
        self.assertTrue(all(r.finished for r in receipts))


if __name__ == '__main__':
    unittest.main()
