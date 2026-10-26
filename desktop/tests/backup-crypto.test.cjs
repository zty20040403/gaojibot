'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { createHash, randomBytes } = require('node:crypto');
const { Readable } = require('node:stream');
const backup = require('../lib/backup-crypto.cjs');
const PASSWORD = 'independent-test-password';

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'gaoji-crypto-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const bytes = Buffer.concat([Buffer.from('private-test-qq-session'), randomBytes(2 * 1024 * 1024)]);
  const info = { schema: 1, id: 'a'.repeat(32), source_instance_id: 'b'.repeat(32), runtime_checksum: 'c'.repeat(64),
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), database_bytes: 4096, created_at: 100, revision: 2, files: 3 };
  const filename = path.join(directory, '备份.gaojibak');
  return { directory, bytes, info, filename };
}
async function collect(stream) { const chunks = []; for await (const chunk of stream) chunks.push(chunk); return Buffer.concat(chunks); }

test('encrypted binary roundtrip, random nonces, private file and authenticated metadata', async t => {
  const { bytes, info, filename, directory } = await fixture(t);
  await backup.encrypt(Readable.from([bytes]), info, PASSWORD, filename);
  const encrypted = await fs.readFile(filename);
  assert.equal(encrypted.subarray(0, 8).toString(), 'GAOJIBK1');
  assert.equal(encrypted.includes(Buffer.from('private-test-qq-session')), false);
  if (process.platform !== 'win32') assert.equal((await fs.stat(filename)).mode & 0o777, 0o600);
  const archive = await backup.openEncrypted(filename, PASSWORD);
  try { assert.deepEqual(archive.metadata, info); assert.deepEqual(await collect(archive.stream()), bytes); }
  finally { await archive.close(); await archive.close(); }
  await backup.encrypt(Readable.from([bytes]), info, PASSWORD, filename);
  assert.notDeepEqual(await fs.readFile(filename), encrypted);
  assert.deepEqual(await fs.readdir(directory), ['备份.gaojibak']);
});

test('wrong password and header, ciphertext, tag corruption or truncation never open', async t => {
  const { bytes, info, filename } = await fixture(t);
  await backup.encrypt(Readable.from([bytes]), info, PASSWORD, filename);
  const good = await fs.readFile(filename);
  await assert.rejects(backup.openEncrypted(filename, 'wrong-long-password'), { code: 'backup_password_or_damage' });
  for (const index of [12, 28, good.indexOf(Buffer.from(info.source_instance_id)), 900, good.length - 1]) {
    const corrupt = Buffer.from(good); corrupt[index] ^= 1;
    await fs.writeFile(filename, corrupt);
    await assert.rejects(backup.openEncrypted(filename, PASSWORD), error => ['invalid_backup', 'backup_password_or_damage'].includes(error.code));
  }
  await fs.writeFile(filename, good.subarray(0, -1));
  await assert.rejects(backup.openEncrypted(filename, PASSWORD), { code: 'invalid_backup' });
});

test('failed export preserves previous file and removes only its partial', async t => {
  const { bytes, info, filename, directory } = await fixture(t);
  await fs.writeFile(filename, 'previous backup');
  for (const source of [Readable.from([bytes.subarray(0, 1000)]), Readable.from([Buffer.alloc(bytes.length)]),
    Readable.from((async function* () { yield bytes.subarray(0, 1000); throw new Error('source interrupted'); })())]) {
    await assert.rejects(backup.encrypt(source, info, PASSWORD, filename));
    assert.equal(await fs.readFile(filename, 'utf8'), 'previous backup');
    assert.deepEqual(await fs.readdir(directory), ['备份.gaojibak']);
  }
  const controller = new AbortController();
  await assert.rejects(backup.encrypt(Readable.from([bytes]), info, PASSWORD, filename,
    { signal: controller.signal, progress: () => controller.abort() }));
  assert.equal(await fs.readFile(filename, 'utf8'), 'previous backup');
  assert.deepEqual(await fs.readdir(directory), ['备份.gaojibak']);
});

test('mutation after validation is rejected before second-pass plaintext is read', async t => {
  const { bytes, info, filename } = await fixture(t);
  await backup.encrypt(Readable.from([bytes]), info, PASSWORD, filename);
  const archive = await backup.openEncrypted(filename, PASSWORD);
  try {
    await fs.appendFile(filename, 'changed');
    await assert.rejects(collect(archive.stream()), { code: 'invalid_backup' });
  } finally { await archive.close(); }
});

test('bounded metadata, short passwords and symlink destinations fail closed', async t => {
  const { bytes, info, filename, directory } = await fixture(t);
  for (const change of [{ bytes: backup.MAX_BYTES + 1 }, { revision: 0 }, { files: 200001 }, { schema: true }, { id: '../outside' }, { N: 2 ** 30 }]) {
    assert.throws(() => backup.metadata({ ...info, ...change }), { code: 'invalid_backup' });
  }
  await assert.rejects(backup.encrypt(Readable.from([bytes]), info, 'short', filename), { code: 'backup_password' });
  const outside = path.join(directory, 'untouched'); await fs.writeFile(outside, 'private');
  await fs.symlink(outside, filename);
  await assert.rejects(backup.encrypt(Readable.from([bytes]), info, PASSWORD, filename), { code: 'invalid_destination' });
  await assert.rejects(backup.openEncrypted(filename, PASSWORD));
  assert.equal(await fs.readFile(outside, 'utf8'), 'private');
});
