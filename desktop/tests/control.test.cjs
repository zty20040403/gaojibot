'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { EventEmitter } = require('node:events');
const { request, consoleTunnel } = require('../lib/control.cjs');

async function fixture(t, handler) {
  const server = http.createServer(handler);
  server.on('clientError', (_error, socket) => socket.destroy());
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const streams = [];
  const client = new EventEmitter();
  client.forwardOut = (source, _sourcePort, host, port, callback) => {
    assert.equal(source, '127.0.0.1'); assert.equal(host, '127.0.0.1'); assert.equal(port, server.address().port);
    const stream = net.createConnection({ host, port });
    streams.push(stream);
    stream.once('error', () => {});
    stream.once('connect', () => callback(null, stream));
  };
  t.after(() => { for (const stream of streams) stream.destroy(); });
  return { client, access: { port: server.address().port, token: 't'.repeat(43) }, streams };
}

test('management requests use the verified SSH channel and bounded routes', async t => {
  let captured;
  const { client, access } = await fixture(t, (req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      captured = { method: req.method, path: req.url, authorization: req.headers.authorization, payload: JSON.parse(Buffer.concat(chunks)) };
      res.end(JSON.stringify({ data: { revision: 2 } }));
    });
  });
  assert.deepEqual(await request(client, access, 'save', { revision: 1 }), { revision: 2 });
  assert.deepEqual(captured, { method: 'POST', path: '/settings', authorization: 'Bearer ' + access.token, payload: { revision: 1 } });
});

test('unsupported operations and oversized payloads fail before opening a tunnel', async () => {
  const client = { forwardOut() { assert.fail('must not contact server'); } };
  const access = { port: 5000, token: 't'.repeat(43) };
  await assert.rejects(request(client, access, 'exec'), { code: 'invalid_operation' });
  await assert.rejects(request(client, access, 'status', { data: 'x'.repeat(256 * 1024) }), { code: 'invalid_operation' });
  await assert.rejects(request(client, { ...access, port: 22 }, 'status'), { code: 'setup_unavailable' });
});

test('component connection tests use fixed routes and keep credentials in the request body', async t => {
  let captured;
  const { client, access } = await fixture(t, (req, res) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => {
      captured = { path: req.url, body: JSON.parse(Buffer.concat(chunks)) };
      res.end(JSON.stringify({ data: { responded: true, available: 2 } }));
    });
  });
  const payload = { options: { base_url: 'https://fixture.invalid' }, key: 'synthetic-test-key-not-real' };
  assert.deepEqual(await request(client, access, 'clusterTest', payload), { responded: true, available: 2 });
  assert.deepEqual(captured, { path: '/cluster/test', body: payload });
  assert.deepEqual(await request(client, access, 'matrixTest', payload), { responded: true, available: 2 });
  assert.deepEqual(captured, { path: '/matrix/test', body: payload });
  assert.deepEqual(await request(client, access, 'imessageTest', payload), { responded: true, available: 2 });
  assert.deepEqual(captured, { path: '/imessage/test', body: payload });
});

test('connection errors never expose the transport error or authentication token', async () => {
  const client = { forwardOut(_a, _b, _c, _d, callback) { callback(new Error('secret-transport-data')); } };
  await assert.rejects(request(client, { port: 5000, token: 'private-token' }, 'status'), error => {
    assert.equal(error.code, 'tunnel_failed');
    assert.doesNotMatch(error.message, /secret-transport-data|private-token/);
    return true;
  });
});

test('invalid, oversized, aborted and redirected responses reject without hanging', async t => {
  for (const mode of ['invalid', 'oversized', 'aborted', 'redirect']) {
    await t.test(mode, async child => {
      const { client, access } = await fixture(child, (req, res) => {
        req.resume();
        if (mode === 'invalid') res.end('<html>not json</html>');
        if (mode === 'oversized') res.end(JSON.stringify({ data: 'x'.repeat(2 * 1024 * 1024) }));
        if (mode === 'aborted') {
          res.writeHead(200, { 'content-length': 10000 }); res.write('{"data":');
          setImmediate(() => res.socket?.destroy());
        }
        if (mode === 'redirect') {
          res.writeHead(302, { location: 'https://must-not-contact.example' }); res.end('{}');
        }
      });
      await assert.rejects(request(client, access, 'status'), error => ['invalid_response', 'connection_lost', 'setup_failed'].includes(error.code));
    });
  }
});

test('console listens on loopback, forwards the existing session and closes with it', async t => {
  const { client, access } = await fixture(t, (_req, res) => res.end('console fixture'));
  const tunnel = await consoleTunnel(client, access.port);
  t.after(tunnel.close);
  assert.equal(new URL(tunnel.url).hostname, '127.0.0.1');
  const response = await fetch(tunnel.url);
  assert.equal(await response.text(), 'console fixture');
  client.emit('close');
  await assert.rejects(fetch(tunnel.url));
});
