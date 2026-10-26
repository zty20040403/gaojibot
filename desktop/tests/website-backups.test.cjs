'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { Readable, PassThrough, Duplex } = require('node:stream');
const { createHash } = require('node:crypto');
const crypto = require('../lib/backup-crypto.cjs');
const backups = require('../lib/website-backups.cjs');
const { siteRequest } = require('../lib/applications.cjs');
const { Websites } = require('../lib/websites.cjs');

const PASSWORD = 'independent-test-password';
async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'gaoji-site-backup-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const bytes = Buffer.from('fixture archive bytes');
  const info = { schema: 1, app_id: 'static-site', id: 'a'.repeat(32), source_instance_id: 'b'.repeat(32),
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), created_at: 100, files: 1 };
  const filename = path.join(directory, 'backup.gaojibak');
  return { directory, bytes, info, filename };
}

test('website envelope authenticates before staging, preserves identity and clears partial files', async t => {
  const { directory, bytes, info, filename } = await fixture(t);
  await crypto.encrypt(Readable.from([bytes]), info, PASSWORD, filename);
  await assert.rejects(backups.importBackup(filename, 'incorrect-password', directory));
  assert.deepEqual(await fs.readdir(directory), ['backup.gaojibak']);
  const first = await backups.importBackup(filename, PASSWORD, directory);
  const again = await backups.importBackup(filename, PASSWORD, directory);
  assert.equal(first.checksum, info.sha256);
  assert.equal(again.checksum, first.checksum);
  assert.notEqual(first.selection, again.selection);
  assert.deepEqual(await fs.readFile(path.join(directory, first.checksum + '.tar.gz')), bytes);
  assert.equal((await fs.readdir(directory)).length, 2);
  const corrupted = await fs.readFile(filename); corrupted[corrupted.length - 1] ^= 1;
  await fs.writeFile(filename, corrupted);
  await assert.rejects(backups.importBackup(filename, PASSWORD, directory));
  assert.deepEqual(await fs.readFile(path.join(directory, first.checksum + '.tar.gz')), bytes);
});

test('legacy robot backups cannot be staged as a website', async t => {
  const { directory, filename } = await fixture(t);
  const bytes = Buffer.alloc(2048);
  const info = { schema: 1, id: 'a'.repeat(32), source_instance_id: 'b'.repeat(32), runtime_checksum: 'c'.repeat(64),
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), database_bytes: 1024, created_at: 100, revision: 1, files: 2 };
  await crypto.encrypt(Readable.from([bytes]), info, PASSWORD, filename);
  await assert.rejects(backups.importBackup(filename, PASSWORD, directory), { code: 'wrong_backup' });
  assert.deepEqual(await fs.readdir(directory), ['backup.gaojibak']);
});

test('website requests keep old schema and strictly bind backup identifiers', () => {
  const value = { schema: 1, app_id: 'static-site', instance_id: 'a'.repeat(32), name: 'Site', expected_revision: 1, content_sha256: '', operation: 'backup' };
  assert.deepEqual(siteRequest(value), value);
  assert.throws(() => siteRequest({ ...value, backup_id: 'b'.repeat(32) }));
  assert.throws(() => siteRequest({ ...value, operation: 'restore' }));
  assert.throws(() => siteRequest({ ...value, operation: 'restore', backup_id: '../etc/passwd' }));
  assert.equal(siteRequest({ ...value, operation: 'restore', backup_id: 'b'.repeat(32) }).backup_id, 'b'.repeat(32));
});

test('SSH export is byte-exact, waits for command receipt, and never embeds sudo password in the command', async t => {
  const { bytes, info, filename } = await fixture(t);
  let command, input;
  const client = { exec(value, callback) {
    command = value;
    const channel = new Duplex({ read() {}, write(data, _encoding, done) { input = String(data); done(); },
      final(done) { done(); setImmediate(() => { this.push(bytes); this.push(null); }); }, autoDestroy: false });
    channel.once('end', () => channel.emit('close', 0));
    channel.stderr = new PassThrough(); channel.close = () => channel.destroy();
    callback(null, channel);
  } };
  await backups.exportBackup(client, info.source_instance_id, info, 'password-required', 'test-sudo', PASSWORD, filename);
  assert.equal(input, 'test-sudo\n'); assert.ok(!command.includes('test-sudo'));
  assert.match(command, /sudo -S/);
  const archive = await crypto.openEncrypted(filename, PASSWORD);
  try { const chunks = []; for await (const part of archive.stream()) chunks.push(part); assert.deepEqual(Buffer.concat(chunks), bytes); }
  finally { await archive.close(); }
});

test('status cleanup cannot collect a staged import before its selection is registered', async t => {
  const { directory, bytes, info } = await fixture(t);
  const store = { list: () => [] };
  const websites = new Websites(directory, 'unused', store, () => { throw Error('No remote requests'); });
  let unblock, ready;
  const waiting = new Promise(resolve => { unblock = resolve; });
  const staged = new Promise(resolve => { ready = resolve; });
  const original = backups.importBackup;
  backups.importBackup = async () => {
    await fs.writeFile(path.join(directory, info.sha256 + '.tar.gz'), bytes);
    ready(); await waiting;
    return { selection: 'fixture-selection', checksum: info.sha256, files: 1, bytes: bytes.length };
  };
  try {
    const pending = websites.importBackup('fixture', PASSWORD);
    await staged;
    websites.cleanup();
    assert.deepEqual(await fs.readFile(path.join(directory, info.sha256 + '.tar.gz')), bytes);
    unblock(); await pending;
    websites.cleanup();
    assert.deepEqual(await fs.readFile(path.join(directory, info.sha256 + '.tar.gz')), bytes);
  } finally { unblock(); backups.importBackup = original; }
});
