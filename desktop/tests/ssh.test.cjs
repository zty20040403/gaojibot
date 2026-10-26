'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const net = require('node:net');
const { fingerprint, discoverHost, connect } = require('../lib/ssh.cjs');
const hostKey = Buffer.from('public-host-key-fixture');
const target = { host: 'example.com', username: 'tester' };

function clientFactory(capture, key = hostKey) {
  return () => {
    const client = new EventEmitter();
    client.destroy = client.end = () => {};
    client.connect = options => {
      capture.options = options;
      queueMicrotask(() => client.emit(options.hostVerifier(key) ? 'ready' : 'error', new Error('rejected')));
    };
    return client;
  };
}

test('fingerprint discovery never sends a password or private key', async () => {
  const capture = {};
  const result = await discoverHost({ ...target, password: 'do-not-send' }, clientFactory(capture));
  assert.equal(result.fingerprint, fingerprint(hostKey));
  assert.equal(capture.options.password, undefined);
  assert.equal(capture.options.privateKey, undefined);
});

test('verified server receives credentials, changed identity is rejected', async () => {
  const capture = {};
  await connect(target, { type: 'password', password: 'fixture-password' }, fingerprint(hostKey), clientFactory(capture));
  assert.equal(capture.options.password, 'fixture-password');
  await assert.rejects(connect(target, { type: 'password', password: 'fixture-password' },
    fingerprint(hostKey), clientFactory({}, Buffer.from('different-server'))), error => error.code === 'host_changed');
});

test('missing credentials and missing trusted fingerprint fail before connecting', () => {
  assert.throws(() => connect(target, { type: 'password', password: 'abc' }, ''));
  assert.throws(() => connect(target, { type: 'unknown' }, fingerprint(hostKey)));
});

function pendingClient() {
  const client = new EventEmitter();
  client.connect = options => { client.options = options; };
  client.destroyed = false;
  client.destroy = client.end = () => {
    client.destroyed = true;
    client.emit('close');
  };
  return client;
}

test('handshake disconnect reports network failure and tolerates subsequent errors', async () => {
  const client = pendingClient();
  const pending = connect(target, { type: 'password', password: 'fixture' }, fingerprint(hostKey), () => client);
  client.emit('error', Object.assign(new Error('Connection lost before handshake'), { level: 'protocol' }));
  assert.doesNotThrow(() => client.emit('error', new Error('Socket closed')));
  await assert.rejects(pending, { code: 'connection_failed' });
  assert.equal(client.destroyed, true);
});

test('only authentication rejection is labeled as credential failure', async () => {
  for (const [level, code] of [['client-authentication', 'authentication_failed'], ['client-timeout', 'connection_timeout'], ['client-dns', 'connection_failed']]) {
    const client = pendingClient();
    const pending = connect(target, { type: 'password', password: 'fixture' }, fingerprint(hostKey), () => client);
    client.emit('error', Object.assign(new Error('private diagnostic'), { level }));
    await assert.rejects(pending, error => error.code === code && !error.message.includes('private diagnostic'));
  }
});

test('connection closed without ready or error rejects instead of hanging', async () => {
  const client = pendingClient();
  const pending = connect(target, { type: 'password', password: 'fixture' }, fingerprint(hostKey), () => client);
  client.emit('close');
  await assert.rejects(pending, { code: 'connection_failed' });
});

test('errors after login close the client without crashing or returning a new connection', async () => {
  const client = pendingClient();
  const pending = connect(target, { type: 'password', password: 'fixture' }, fingerprint(hostKey), () => client);
  client.emit('ready');
  assert.equal(await pending, client);
  assert.doesNotThrow(() => client.emit('error', new Error('reset')));
  assert.doesNotThrow(() => client.emit('error', new Error('closed')));
  assert.equal(client.destroyed, true);
});

test('host discovery closes its probe and handles repeated handshake errors', async () => {
  const client = pendingClient();
  const pending = discoverHost(target, () => client);
  assert.equal(client.options.hostVerifier(hostKey), false);
  client.emit('error', new Error('Host denied'));
  assert.doesNotThrow(() => client.emit('error', new Error('Connection lost')));
  assert.equal((await pending).fingerprint, fingerprint(hostKey));
  assert.equal(client.destroyed, true);
});

test('real localhost peer closing before SSH handshake does not crash the client', { timeout: 10000 }, async () => {
  const server = net.createServer(socket => { socket.on('error', () => socket.destroy()); socket.destroy(); });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  try {
    const local = { host: '127.0.0.1', port: server.address().port, username: 'fixture' };
    await assert.rejects(connect(local, { type: 'password', password: 'local-test-only' }, fingerprint(hostKey)), { code: 'connection_failed' });
    await assert.rejects(discoverHost(local), { code: 'connection_failed' });
  } finally { await new Promise(resolve => server.close(resolve)); }
});
