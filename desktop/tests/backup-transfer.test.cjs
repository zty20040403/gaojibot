'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { createHash, randomBytes, generateKeyPairSync } = require('node:crypto');
const { Readable } = require('node:stream');
const crypto = require('../lib/backup-crypto.cjs');
const transfer = require('../lib/backup-transfer.cjs');
const PASSWORD = 'transfer-test-password';

async function fixture(t, handler) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'gaoji-transfer-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const server = http.createServer(handler);
  server.on('clientError', (_error, socket) => socket.destroy());
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const streams = [];
  const client = { forwardOut(source, _sourcePort, host, port, callback) {
    assert.equal(source, '127.0.0.1'); assert.equal(host, '127.0.0.1'); assert.equal(port, server.address().port);
    const stream = net.createConnection({ host, port }); streams.push(stream);
    stream.once('error', () => {}); stream.once('connect', () => callback(null, stream));
  } };
  t.after(() => { for (const stream of streams) stream.destroy(); });
  const bytes = randomBytes(3 * 1024 * 1024);
  const info = { schema: 1, id: 'a'.repeat(32), source_instance_id: 'b'.repeat(32), runtime_checksum: 'c'.repeat(64),
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), database_bytes: 4096, created_at: 100, revision: 2, files: 3 };
  const access = { port: server.address().port, token: 't'.repeat(43), instance_id: 'd'.repeat(32) };
  return { client, access, bytes, info, filename: path.join(directory, 'backup.gaojibak'),
    receipt: { id: createHash('sha256').update(access.instance_id + ':' + info.sha256).digest('hex').slice(0, 32), imported: true } };
}
function json(res, data, status = 200) { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ data })); }
async function collect(source) { const chunks = []; for await (const chunk of source) chunks.push(chunk); return Buffer.concat(chunks); }

async function throughSSH(t, f) {
  const { Server } = require('ssh2');
  const ssh = require('../lib/ssh.cjs');
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048,
    privateKeyEncoding: { type: 'pkcs1', format: 'pem' }, publicKeyEncoding: { type: 'pkcs1', format: 'pem' } });
  const peers = new Set(), sockets = new Set();
  const server = new Server({ hostKeys: [privateKey] }, peer => {
    peers.add(peer);
    peer.on('error', () => {});
    peer.once('close', () => peers.delete(peer));
    peer.on('authentication', context => {
      if (context.method === 'password' && context.username === 'fixture' && context.password === PASSWORD) context.accept();
      else context.reject();
    });
    peer.on('ready', () => peer.on('tcpip', (accept, reject, info) => {
      if (info.destIP !== '127.0.0.1' || info.destPort !== f.access.port) { reject(); return; }
      const target = net.createConnection({ host: info.destIP, port: info.destPort });
      sockets.add(target);
      target.on('error', () => target.destroy());
      target.once('close', () => sockets.delete(target));
      target.once('connect', () => {
        const channel = accept();
        channel.on('error', () => target.destroy());
        channel.once('close', () => target.destroy());
        target.once('close', () => channel.destroy());
        target.pipe(channel).pipe(target);
      });
    }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let client;
  t.after(async () => {
    client?.end();
    for (const peer of peers) peer.end();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  });
  const endpoint = { host: '127.0.0.1', username: 'fixture', port: server.address().port };
  const host = await ssh.discoverHost(endpoint);
  client = await ssh.connect(endpoint, { type: 'password', password: PASSWORD }, host.fingerprint);
  return client;
}

test('real SSH channels roundtrip backup bytes without net.Socket timeout methods', { timeout: 20000 }, async t => {
  let imported = false;
  const f = await fixture(t, async (req, res) => {
    assert.equal(req.headers.authorization, 'Bearer ' + f.access.token);
    if (req.url === '/versions') { await collect(req); return json(res, { checksum: f.info.runtime_checksum, revision: 4 }); }
    if (req.url === '/backup/import/status') { await collect(req); return json(res, { ...f.receipt, imported }); }
    if (req.url === '/backup/export') {
      await collect(req);
      res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': f.bytes.length,
        'X-Gaoji-Backup': Buffer.from(JSON.stringify(f.info)).toString('base64') });
      return res.end(f.bytes);
    }
    assert.equal(req.url, '/backup/import');
    assert.deepEqual(await collect(req), f.bytes);
    imported = true;
    json(res, f.receipt);
  });
  const client = await throughSSH(t, f);
  const channel = await require('../lib/control.cjs').forward(client, f.access.port);
  assert.equal(typeof channel.setTimeout, 'undefined');
  channel.destroy();
  await transfer.exportBackup(client, f.access, f.info.id, PASSWORD, f.filename);
  const archive = await crypto.openEncrypted(f.filename, PASSWORD);
  try { assert.deepEqual(await collect(archive.stream()), f.bytes); } finally { await archive.close(); }
  assert.deepEqual(await transfer.importBackup(client, f.access, f.filename, PASSWORD), f.receipt);
  assert.equal(imported, true);
});

test('an idle response aborts and removes incomplete encrypted output', { timeout: 10000 }, async t => {
  const original = global.setTimeout;
  global.setTimeout = (callback, ms, ...args) => original(callback, ms === 60000 ? 500 : ms, ...args);
  t.after(() => { global.setTimeout = original; });
  const f = await fixture(t, async (req, res) => {
    await collect(req);
    res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': f.bytes.length,
      'X-Gaoji-Backup': Buffer.from(JSON.stringify(f.info)).toString('base64') });
    res.flushHeaders();
  });
  await assert.rejects(transfer.exportBackup(f.client, f.access, f.info.id, PASSWORD, f.filename), { code: 'backup_connection_lost' });
  assert.deepEqual(await fs.readdir(path.dirname(f.filename)), []);
});

test('authenticated export encrypts streamed bytes and validates headers', { timeout: 10000 }, async t => {
  const f = await fixture(t, async (req, res) => {
    assert.equal(req.url, '/backup/export'); assert.equal(req.headers.authorization, 'Bearer ' + f.access.token);
    assert.deepEqual(JSON.parse(await collect(req)), { id: f.info.id });
    res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': f.bytes.length,
      'X-Gaoji-Backup': Buffer.from(JSON.stringify(f.info)).toString('base64') }); res.end(f.bytes);
  });
  assert.deepEqual(await transfer.exportBackup(f.client, f.access, f.info.id, PASSWORD, f.filename), { exported: true, bytes: f.bytes.length });
  const archive = await crypto.openEncrypted(f.filename, PASSWORD);
  try { assert.deepEqual(await collect(archive.stream()), f.bytes); } finally { await archive.close(); }
});

test('import verifies locally, sends no password, and accepts only matching receipt', { timeout: 10000 }, async t => {
  let uploaded = false;
  const f = await fixture(t, async (req, res) => {
    assert.equal(req.headers.authorization, 'Bearer ' + f.access.token);
    if (req.url === '/versions') { await collect(req); return json(res, { checksum: f.info.runtime_checksum, revision: 4 }); }
    if (req.url === '/backup/import/status') { await collect(req); return json(res, { ...f.receipt, imported: uploaded }); }
    assert.equal(req.url, '/backup/import'); assert.equal(req.headers['x-gaoji-revision'], '4');
    assert.equal(JSON.stringify(req.headers).includes(PASSWORD), false);
    assert.deepEqual(JSON.parse(Buffer.from(req.headers['x-gaoji-backup'], 'base64')), f.info);
    assert.deepEqual(await collect(req), f.bytes); uploaded = true; json(res, f.receipt);
  });
  await crypto.encrypt(Readable.from([f.bytes]), f.info, PASSWORD, f.filename);
  const phases = new Set();
  assert.deepEqual(await transfer.importBackup(f.client, f.access, f.filename, PASSWORD, { progress: phase => phases.add(phase) }), f.receipt);
  assert.equal(uploaded, true); assert.deepEqual([...phases], ['verifying', 'uploading']);
});

test('same-archive retry handles an early duplicate receipt without hanging', { timeout: 10000 }, async t => {
  let published = false;
  const f = await fixture(t, (req, res) => {
    if (req.url === '/versions') { req.resume(); return json(res, { checksum: f.info.runtime_checksum, revision: 4 }); }
    if (req.url === '/backup/import/status') { req.resume(); return json(res, { ...f.receipt, imported: published, already_imported: published }); }
    published = true;
    json(res, { ...f.receipt, already_imported: true });
  });
  await crypto.encrypt(Readable.from([f.bytes]), f.info, PASSWORD, f.filename);
  assert.equal((await transfer.importBackup(f.client, f.access, f.filename, PASSWORD)).already_imported, true);
});

test('local import obtains target identity from the authenticated controller', { timeout: 10000 }, async t => {
  let uploaded = false;
  const f = await fixture(t, async (req, res) => {
    if (req.url === '/versions') { req.resume(); return json(res, {
      checksum: f.info.runtime_checksum, revision: 4, instance_id: f.access.instance_id,
    }); }
    if (req.url === '/backup/import/status') { req.resume(); return json(res, { ...f.receipt, imported: false }); }
    assert.equal(req.url, '/backup/import');
    assert.deepEqual(await collect(req), f.bytes);
    uploaded = true;
    json(res, f.receipt);
  });
  await crypto.encrypt(Readable.from([f.bytes]), f.info, PASSWORD, f.filename);
  const { instance_id, ...localAccess } = f.access;
  assert.deepEqual(await transfer.importBackup(f.client, localAccess, f.filename, PASSWORD), f.receipt);
  assert.equal(uploaded, true);
});

test('missing or conflicting target identity prevents any upload', { timeout: 10000 }, async t => {
  for (const mode of ['missing', 'conflicting']) await t.test(mode, async child => {
    let requests = 0;
    const f = await fixture(child, (req, res) => {
      requests++;
      assert.equal(req.url, '/versions');
      req.resume();
      json(res, { checksum: f.info.runtime_checksum, revision: 4,
        ...(mode === 'conflicting' ? { instance_id: 'e'.repeat(32) } : {}),
      });
    });
    await crypto.encrypt(Readable.from([f.bytes]), f.info, PASSWORD, f.filename);
    const access = { ...f.access };
    if (mode === 'missing') delete access.instance_id;
    await assert.rejects(transfer.importBackup(f.client, access, f.filename, PASSWORD), { code: 'invalid_backup_response' });
    assert.equal(requests, 1);
  });
});

test('wrong password makes no request; mismatching runtime makes no upload', { timeout: 10000 }, async t => {
  let requests = 0;
  const f = await fixture(t, (req, res) => { requests++; assert.equal(req.url, '/versions'); req.resume(); json(res, { checksum: 'f'.repeat(64), revision: 4 }); });
  await crypto.encrypt(Readable.from([f.bytes]), f.info, PASSWORD, f.filename);
  await assert.rejects(transfer.importBackup(f.client, f.access, f.filename, 'wrong-test-password'), { code: 'backup_password_or_damage' });
  assert.equal(requests, 0);
  await assert.rejects(transfer.importBackup(f.client, f.access, f.filename, PASSWORD), { code: 'backup_version_mismatch' });
  assert.equal(requests, 1);
});

test('cancel or invalid import receipt cannot report successful import', { timeout: 10000 }, async t => {
  for (const mode of ['cancel', 'disconnect', 'wrong-receipt']) await t.test(mode, async child => {
    const f = await fixture(child, async (req, res) => {
      if (req.url === '/versions') { req.resume(); return json(res, { checksum: f.info.runtime_checksum, revision: 4 }); }
      if (req.url === '/backup/import/status') { req.resume(); return json(res, { ...f.receipt, imported: false }); }
      if (mode === 'disconnect') return req.socket.destroy();
      if (mode === 'cancel') return req.resume();
      await collect(req); json(res, { ...f.receipt, id: 'e'.repeat(32) });
    });
    await crypto.encrypt(Readable.from([f.bytes]), f.info, PASSWORD, f.filename);
    const controller = new AbortController();
    await assert.rejects(transfer.importBackup(f.client, f.access, f.filename, PASSWORD, { signal: controller.signal,
      progress: phase => { if (mode === 'cancel' && phase === 'uploading') controller.abort(); } }),
    { code: mode === 'cancel' ? 'backup_cancelled' : mode === 'disconnect' ? 'backup_connection_lost' : 'invalid_backup_response' });
  });
});
