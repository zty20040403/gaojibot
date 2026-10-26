'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createCipheriv, createDecipheriv, randomBytes } = require('node:crypto');
const { Connections } = require('../lib/connections.cjs');

function fixture(t, available = true) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-connections-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const key = randomBytes(32);
  const encryption = {
    isEncryptionAvailable: () => available,
    encryptString(value) {
      const iv = randomBytes(12); const cipher = createCipheriv('aes-256-gcm', key, iv);
      const encrypted = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]);
      return Buffer.concat([iv, cipher.getAuthTag(), encrypted]);
    },
    decryptString(value) {
      const decipher = createDecipheriv('aes-256-gcm', key, value.subarray(0, 12));
      decipher.setAuthTag(value.subarray(12, 28));
      return Buffer.concat([decipher.update(value.subarray(28)), decipher.final()]).toString('utf8');
    },
  };
  return { directory, encryption, store: new Connections(directory, encryption) };
}
const target = { name: 'test', host: '192.0.2.10', port: 22, username: 'test', fingerprint: 'SHA256:' + 'a'.repeat(43) };
const credentials = { type: 'password', password: 'test-only-secret-never-log' };
test('remembered credentials are encrypted on disk and omitted from public listing', t => {
  const { store, directory, encryption } = fixture(t);
  const item = store.save({ ...target, remember: true }, credentials);
  assert.equal(JSON.stringify(item).includes(credentials.password), false);
  assert.equal(fs.readFileSync(store.filename, 'utf8').includes(credentials.password), false);
  assert.deepEqual(new Connections(directory, encryption).credentials(item.id), credentials);
  if (process.platform !== 'win32') assert.equal(fs.statSync(store.filename).mode & 0o777, 0o600);
});
test('session-only login is not silently persisted', t => {
  const { store, directory, encryption } = fixture(t);
  const item = store.save(target, credentials);
  assert.deepEqual(store.credentials(item.id), credentials);
  assert.equal(new Connections(directory, encryption).list()[0].authenticated, false);
  assert.throws(() => new Connections(directory, encryption).credentials(item.id), /重新填写/);
});
test('unavailable secure storage never falls back to a plaintext password', t => {
  const { store } = fixture(t, false);
  assert.throws(() => store.save({ ...target, remember: true }, credentials), /安全存储/);
  assert.equal(store.list().length, 0);
  assert.equal(fs.existsSync(store.filename), false);
});
test('forgetting removes credentials without touching unrelated data', t => {
  const { store, directory } = fixture(t);
  fs.writeFileSync(path.join(directory, 'sentinel'), 'retain');
  const item = store.save({ ...target, remember: true }, credentials);
  store.forget(item.id);
  assert.equal(store.list().length, 0);
  assert.throws(() => store.credentials(item.id), /添加服务器/);
  assert.equal(fs.readFileSync(path.join(directory, 'sentinel'), 'utf8'), 'retain');
});
test('credential file symlinks are refused', t => {
  if (process.platform === 'win32') return t.skip('symlink permission depends on Windows developer mode');
  const { directory, encryption } = fixture(t);
  const other = path.join(directory, 'other');
  fs.writeFileSync(other, '{}'); fs.symlinkSync(other, path.join(directory, 'servers.json'));
  assert.throws(() => new Connections(directory, encryption), /符号链接/);
});
test('installation reference survives login refresh but cannot move to another host', t => {
  const { store } = fixture(t);
  const item = store.save(target, credentials);
  const deployment = { job: 'a'.repeat(32), checksum: 'b'.repeat(64), version: 'test', startedAt: Date.now() };
  store.setDeployment(item.id, deployment);
  const refreshed = store.save({ ...target, id: item.id }, credentials);
  assert.deepEqual(refreshed.deployment, deployment);
  assert.throws(() => store.save({ ...target, id: item.id, host: '192.0.2.20' }, credentials), /新增连接/);
  assert.equal(store.get(item.id).host, target.host);
});
