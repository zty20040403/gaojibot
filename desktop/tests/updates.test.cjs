'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { PassThrough } = require('node:stream');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const updates = require('../lib/updates.cjs');
const control = require('../lib/control.cjs');
const { Connections } = require('../lib/connections.cjs');
const { FILES } = require('../lib/deploy.cjs');

const record = { id: '1'.repeat(32), job: '2'.repeat(32), kind: 'update', revision: 2,
  expected_checksum: 'a'.repeat(64), target_checksum: 'b'.repeat(64), runnerChecksum: 'b'.repeat(64) };
function responding(values) {
  const calls = [];
  return { calls, exec(command, callback) {
    calls.push(command);
    const value = values.shift();
    assert.ok(value, 'unexpected SSH command: ' + command);
    const stream = new PassThrough(); stream.stderr = new PassThrough(); stream.close = () => {};
    stream.end = () => queueMicrotask(() => { stream.emit('data', Buffer.from(value.stdout || '')); stream.emit('close', value.code || 0); });
    callback(null, stream);
  } };
}
function remote(status = 'running', extra = {}) {
  return { stdout: JSON.stringify({ schema: 1, id: record.id, job: record.job, target_checksum: record.target_checksum,
    status, stage: 'verify', message: 'fixture', ...extra }) };
}
function bundleFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-update-bundle-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const manifest = { schema: 1, version: 'test', files: {} };
  for (const name of FILES) {
    const data = Buffer.from('update fixture ' + name);
    fs.writeFileSync(path.join(directory, name), data);
    manifest.files[name] = { bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') };
  }
  fs.writeFileSync(path.join(directory, 'deployment.json'), JSON.stringify(manifest));
  return { directory, checksum: manifest.files['gaoji-runtime.tar.gz'].sha256 };
}
const probe = '\n__GAOJI_OS__\nID=debian\nVERSION_ID="12"\nPRETTY_NAME="Test"\n__GAOJI_ARCH__\nx86_64\n__GAOJI_UID__\n0\n__GAOJI_DISK__\n/dev/test 99999999 0 99999999 0% /\n__GAOJI_SYSTEMD__\nyes\n__GAOJI_DOCKER__\n\n__GAOJI_NIX__\n\n__GAOJI_SUDO__\nroot\n__GAOJI_DONE__\n';
test('update script validates bundle, operator, request ID and has no reinstall or arbitrary command', () => {
  const manifest = { files: Object.fromEntries(FILES.map(name => [name, { sha256: 'b'.repeat(64) }])) };
  const script = updates.launchScript(record, '/tmp/gaoji-upload.1234567890', manifest, 1000);
  if (process.platform !== 'win32') execFileSync('/bin/sh', ['-n'], { input: script });
  assert.match(script, /owner.json/);
  assert.match(script, /--prepare-update --operation/);
  assert.match(script, /sha256sum --check --status/);
  assert.doesNotMatch(script, /nixos-rebuild|docker .*prune|gaoji-app-install-/);
  assert.throws(() => updates.launchScript({ ...record, id: '$(reboot)' }, '/tmp/gaoji-upload.1234567890', manifest, 1000));
  assert.throws(() => updates.launchScript(record, '/home/user', manifest, 1000));
  assert.throws(() => updates.launchScript(record, '/tmp/gaoji-upload.1234567890', manifest, -1));
});
test('missing status remains uncertain; live and stopped workers are distinguished', async () => {
  const missing = responding([{ code: 1 }]);
  assert.equal((await updates.status(missing, record)).status, 'unconfirmed');
  assert.equal(missing.calls.length, 1);
  const active = responding([remote(), { stdout: 'active\n' }]);
  assert.equal((await updates.status(active, record)).status, 'running');
  const dead = responding([remote(), { stdout: 'failed\n' }]);
  const result = await updates.status(dead, record);
  assert.equal(result.status, 'interrupted');
  assert.equal(result.id, record.id);
  assert.equal(result.resumable, true);
});
test('mismatched remote receipts are rejected and finished tasks are never relaunched', async () => {
  for (const fields of [{ id: '3'.repeat(32) }, { job: '4'.repeat(32) }, { target_checksum: 'c'.repeat(64) }]) {
    await assert.rejects(updates.status(responding([remote('running', fields)]), record), { code: 'invalid_update_status' });
  }
  const client = responding([remote('completed', { finished: true })]);
  const result = await updates.resume(client, '/not-read', record, 'not-used', () => assert.fail('must not submit'));
  assert.equal(result.status, 'completed');
  assert.equal(client.calls.length, 1);
});
test('rollback records require explicit data replacement confirmation', () => {
  assert.throws(() => updates.validateRecord({ ...record, kind: 'rollback', rollback_id: '3'.repeat(32) }), { code: 'confirmation_required' });
  assert.throws(() => updates.validateRecord({ ...record, revision: true }));
  const parsed = updates.validateRecord({ ...record, password: 'must-not-persist' });
  assert.equal(parsed.password, undefined);
});
test('update identity survives app restart and reconnect; passwords never enter the record', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-update-connections-'));
  t.after(() => fs.rmSync(dir, { force: true, recursive: true }));
  const storage = { isEncryptionAvailable: () => false };
  const store = new Connections(dir, storage);
  const input = { name: 'test', host: 'fixture.invalid', port: 22, username: 'test', fingerprint: 'SHA256:' + 'A'.repeat(43), remember: false };
  const server = store.save(input, { type: 'password', password: 'session-only' });
  store.setDeployment(server.id, { job: record.job, checksum: record.expected_checksum, version: 'test', startedAt: 1 });
  store.setUpdate(server.id, { ...record, password: 'must-not-persist' });
  store.save({ ...input, id: server.id }, { type: 'password', password: 'replacement' });
  const reopened = new Connections(dir, storage);
  assert.deepEqual(reopened.get(server.id).update, record);
  assert.doesNotMatch(fs.readFileSync(path.join(dir, 'servers.json'), 'utf8'), /must-not-persist|session-only|replacement/);
  assert.throws(() => store.setUpdate(server.id, { ...record, job: '5'.repeat(32) }));
});

test('legacy server fallback applies only to a missing versions endpoint', async t => {
  const { directory } = bundleFixture(t);
  t.mock.method(control, 'accessFor', async () => ({}));
  const actions = [];
  t.mock.method(control, 'request', async (_client, _access, action) => {
    actions.push(action);
    if (action === 'versions') throw Object.assign(new Error('older controller'), { code: 'setup_unsupported' });
    return { instance: { revision: 2 }, operation: null };
  });
  const client = responding([{ stdout: JSON.stringify({ schema: 1, job: record.job, checksum: record.expected_checksum,
    status: 'awaiting_setup', completed: [] }) }]);
  const details = await updates.plan(client, directory, { job: record.job, version: 'old' });
  assert.equal(details.current.legacy, true);
  assert.equal(details.current.checksum, record.expected_checksum);
  assert.deepEqual(actions, ['versions', 'status']);
  t.mock.method(control, 'request', async () => { throw Object.assign(new Error('offline'), { code: 'connection_lost' }); });
  await assert.rejects(updates.plan(client, directory, { job: record.job }), { code: 'connection_lost' });
  assert.equal(client.calls.length, 1);
});

test('lost acknowledgement retains the operation and resumes the same uploaded request', async t => {
  const { directory, checksum } = bundleFixture(t);
  t.mock.method(control, 'accessFor', async () => ({}));
  t.mock.method(control, 'request', async () => ({ checksum: record.expected_checksum, version: 'old', revision: 2, rollbacks: [] }));
  let persisted;
  const client = responding([{ stdout: probe }, { stdout: '/tmp/gaoji-upload.1234567890' }, { code: 1 }]);
  let uploads = 0;
  client.sftp = callback => callback(null, { end() {}, fastPut(_source, target, _options, done) {
    assert.ok(FILES.some(name => target.endsWith('/' + name))); uploads++; done();
  } });
  const execute = client.exec;
  client.exec = (command, callback) => {
    if (command.includes('--prepare-update')) assert.ok(persisted, 'identity must be durable before dispatch');
    execute(command, callback);
  };
  await assert.rejects(updates.start(client, directory, { deployment: { job: record.job } }, {
    kind: 'update', revision: 2, expectedChecksum: record.expected_checksum, confirm: true,
  }, value => { persisted = value; }), { code: 'update_unconfirmed' });
  assert.equal(uploads, 4);
  assert.equal(persisted.target_checksum, checksum);
  assert.equal(persisted.password, undefined);
  const blocked = responding([{ code: 1 }]);
  await assert.rejects(updates.start(blocked, directory, { deployment: { job: record.job }, update: persisted }, {
    kind: 'update', revision: 2, expectedChecksum: record.expected_checksum, confirm: true,
  }, () => assert.fail('must not create a second operation')), { code: 'update_busy' });
  const again = responding([{ code: 1 }, { stdout: probe }, { stdout: '/tmp/gaoji-upload.0987654321' }, {},
    { stdout: JSON.stringify({ schema: 1, id: persisted.id, job: record.job, target_checksum: checksum,
      status: 'running', stage: 'prepare', message: 'accepted' }) }, { stdout: 'active' }, {}]);
  again.sftp = client.sftp;
  const resumed = await updates.resume(again, directory, persisted, undefined, value => assert.deepEqual(value, persisted));
  assert.equal(resumed.id, persisted.id);
  assert.equal(resumed.status, 'running');
  assert.equal(uploads, 8);
  assert.ok(again.calls.some(command => command.includes('--prepare-update --operation ' + persisted.id)));
});
