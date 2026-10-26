'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const lifecycle = require('../lib/lifecycle.cjs');
const ssh = require('../lib/ssh.cjs');
const deploy = require('../lib/deploy.cjs');
const { Connections } = require('../lib/connections.cjs');

const record = { id: 'a'.repeat(32), job: 'b'.repeat(32), instance_id: 'c'.repeat(32), kind: 'uninstall',
  created_at: 1700000000, service_state: 'installed', dispatched: false, settled: false };
const server = { id: 'fixture', deployment: { job: record.job }, lifecycle: record };
function receipt(status = 'preview', extra = {}) {
  return { schema: 1, ...record, status, stage: 'prepare', message: 'fixture', updated_at: record.created_at, resumable: false,
    units: ['gaoji-app-' + record.instance_id + '.service'], data_retained: true, shared_software_not_removed: true,
    data_directory: '/var/lib/gaoji-app/data/instances/' + record.instance_id, ...extra };
}
function reads(t, values) {
  const calls = [];
  t.mock.method(ssh, 'execute', async (_client, command) => {
    calls.push(command); assert.ok(values.length, 'unexpected read ' + command);
    const value = values.shift();
    if (value instanceof Error) throw value;
    return { code: 0, stdout: '', ...value };
  });
  return calls;
}
function gateway(t, action) {
  t.mock.method(ssh, 'inspectServer', async () => ({ probe: { uid: 1000, privilege: 'password-required' } }));
  t.mock.method(deploy, 'privileged', action);
}
test('fixed maintenance script rejects arbitrary jobs, operations and shell arguments', () => {
  for (const [action, argument] of [['prepare', 'uninstall'], ['prepare', 'reinstall'], ['dispatch', record.id]]) {
    const script = lifecycle.script(record.job, 1000, action, argument);
    if (process.platform !== 'win32') execFileSync('/bin/sh', ['-n'], { input: script });
    assert.match(script, /PYTHONPATH= PYTHONHOME=/);
    assert.match(script, /owner.json/); assert.match(script, /load_release/);
    assert.doesNotMatch(script, /nixos-rebuild|docker .*prune|ssh .*h310/);
  }
  for (const args of [['$(reboot)', 1000, 'prepare', 'uninstall'], [record.job, -1, 'prepare', 'uninstall'],
    [record.job, 1000, 'prepare', 'delete'], [record.job, 1000, 'dispatch', '../other']]) assert.throws(() => lifecycle.script(...args));
});
test('public receipts cannot import foreign identities, units or data paths', () => {
  const valid = lifecycle.receipt(receipt('preview', { password: 'not-public', unit_body: 'secret' }), record);
  assert.equal(valid.password, undefined); assert.equal(valid.unit_body, undefined);
  for (const fields of [{ schema: 2 }, { id: 'd'.repeat(32) }, { job: 'd'.repeat(32) }, { instance_id: 'd'.repeat(32) },
    { kind: 'reinstall' }, { units: ['ssh.service'] }, { units: Array(2).fill(valid.units[0]) }, { data_directory: '/home' },
    { updated_at: -1 }, { data_retained: false }, { shared_software_not_removed: false }]) {
    assert.throws(() => lifecycle.receipt(receipt('preview', fields), record), { code: 'invalid_lifecycle_status' });
  }
});
test('preview is saved without password and does not dispatch', async t => {
  let saved;
  gateway(t, async (_client, script, privilege, password) => {
    assert.match(script, /'prepare' 'uninstall'/); assert.equal(privilege, 'password-required'); assert.equal(password, 'ephemeral');
    return { code: 0, stdout: JSON.stringify({ ok: true, data: receipt() }) };
  });
  const result = await lifecycle.prepare({}, { ...server, lifecycle: undefined }, { id: server.id, kind: 'uninstall', password: 'ephemeral' }, value => { saved = value; });
  assert.equal(result.status, 'preview'); assert.deepEqual(saved, record);
});
test('unsupported controllers get an upgrade message, never a fallback installation', async t => {
  gateway(t, async () => ({ code: 0, stdout: JSON.stringify({ ok: false, error: { code: 'lifecycle_unsupported', message: '请先升级管理器' } }) }));
  await assert.rejects(lifecycle.prepare({}, server, { id: server.id, kind: 'uninstall' }, () => assert.fail()), { code: 'lifecycle_unsupported' });
});
test('uncertain and interrupted status never launch anything during polling', async t => {
  const calls = reads(t, [{ code: 1 }, { stdout: JSON.stringify(receipt()) },
    { stdout: JSON.stringify(receipt('running')) }, { stdout: 'active\n' },
    { stdout: JSON.stringify(receipt('queued')) }, { stdout: 'failed\n' }]);
  const submitted = { ...record, dispatched: true };
  assert.equal((await lifecycle.status({}, submitted)).status, 'unconfirmed');
  assert.equal((await lifecycle.status({}, submitted)).status, 'unconfirmed');
  assert.equal((await lifecycle.status({}, submitted)).status, 'running');
  const interrupted = await lifecycle.status({}, submitted);
  assert.equal(interrupted.status, 'interrupted'); assert.equal(interrupted.resumable, true);
  assert.ok(calls.every(command => command.startsWith('cat ') || command.startsWith('systemctl show ')));
});
test('completed or live submitted tasks are not dispatched again', async t => {
  reads(t, [{ stdout: JSON.stringify(receipt('completed', { service_state: 'retained' })) },
    { stdout: JSON.stringify(receipt('running')) }, { stdout: 'active' }]);
  gateway(t, async () => assert.fail('must not dispatch'));
  const options = { id: server.id, operationId: record.id, confirm: true };
  assert.equal((await lifecycle.submit({}, server, options, () => assert.fail())).status, 'completed');
  assert.equal((await lifecycle.submit({}, { ...server, lifecycle: { ...record, dispatched: true } }, options, () => assert.fail())).status, 'running');
});
test('lost acknowledgement preserves one task and forbids creating a second', async t => {
  let persisted;
  reads(t, [{ stdout: JSON.stringify(receipt()) }, { code: 1 }, { stdout: JSON.stringify(receipt()) }]);
  let dispatches = 0;
  gateway(t, async (_client, script) => {
    dispatches++; assert.equal(persisted.id, record.id); assert.equal(persisted.dispatched, true);
    assert.match(script, new RegExp("'dispatch' '" + record.id + "'"));
    if (dispatches === 1) return { code: 1, stdout: '' };
    return { code: 0, stdout: JSON.stringify({ ok: true, data: receipt('queued', { resumable: true }) }) };
  });
  const options = { id: server.id, operationId: record.id, confirm: true, password: 'private' };
  await assert.rejects(lifecycle.submit({}, server, options, value => { persisted = value; }), { code: 'lifecycle_unconfirmed' });
  assert.equal(persisted.password, undefined);
  const resumedServer = { ...server, lifecycle: persisted };
  await assert.rejects(lifecycle.prepare({}, resumedServer, { id: server.id, kind: 'uninstall' }, () => assert.fail()), { code: 'lifecycle_busy' });
  const result = await lifecycle.submit({}, resumedServer, options, value => assert.deepEqual(value, persisted));
  assert.equal(result.id, record.id); assert.equal(result.status, 'queued'); assert.equal(dispatches, 2);
});
test('expired previews permit a new preview; confirmed fields cannot be extended', async t => {
  reads(t, [{ stdout: JSON.stringify(receipt('failed')) }]);
  gateway(t, async () => ({ code: 0, stdout: JSON.stringify({ ok: true, data: receipt('preview', { id: 'd'.repeat(32) }) }) }));
  assert.equal((await lifecycle.prepare({}, { ...server, lifecycle: { ...record, dispatched: true } }, { id: server.id, kind: 'uninstall' }, () => {})).id, 'd'.repeat(32));
  await assert.rejects(lifecycle.submit({}, server, { operationId: record.id, confirm: false }, () => {}), { code: 'confirmation_required' });
  await assert.rejects(lifecycle.submit({}, server, { operationId: record.id, confirm: true, command: 'reboot' }, () => {}), { code: 'confirmation_required' });
  await assert.rejects(lifecycle.submit({}, server, { operationId: 'd'.repeat(32), confirm: true }, () => {}), { code: 'invalid_lifecycle' });
});
test('reconnect preserves lifecycle identity without preserving sudo passwords', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-lifecycle-connections-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const encryption = { isEncryptionAvailable: () => false };
  const store = new Connections(dir, encryption);
  const input = { name: 'fixture', host: 'fixture.invalid', port: 22, username: 'tester', fingerprint: 'SHA256:' + 'A'.repeat(43), remember: false };
  const saved = store.save(input, { type: 'password', password: 'ssh-private' });
  store.setDeployment(saved.id, { job: record.job, checksum: 'd'.repeat(64), version: 'fixture', startedAt: 1 });
  store.setLifecycle(saved.id, { ...record, dispatched: true, password: 'sudo-private' });
  store.save({ ...input, id: saved.id }, { type: 'password', password: 'replacement-private' });
  assert.deepEqual(new Connections(dir, encryption).get(saved.id).lifecycle, { ...record, dispatched: true });
  assert.doesNotMatch(fs.readFileSync(store.filename, 'utf8'), /ssh-private|sudo-private|replacement-private/);
  assert.throws(() => store.setLifecycle(saved.id, { ...record, job: 'd'.repeat(32) }));
});

test('purge needs an additional instance-specific confirmation and complete deletion inventory', async t => {
  const purgeRecord = { ...record, kind: 'purge', service_state: 'retained' };
  const purgeReceipt = receipt('preview', { kind: 'purge', service_state: 'retained', data_retained: false, units: [], audit_retained: true,
    data_bytes: 12345, delete_resources: { containers: [], volumes: [], networks: [] } });
  assert.equal(lifecycle.receipt(purgeReceipt, purgeRecord).data_retained, false);
  assert.throws(() => lifecycle.receipt({ ...purgeReceipt, delete_resources: undefined }, purgeRecord));
  assert.throws(() => lifecycle.receipt({ ...purgeReceipt, delete_resources: {
    ...purgeReceipt.delete_resources, containers: [{ id: 'f'.repeat(64), name: 'gaoji-' + 'f'.repeat(32) + '-bot-1' }],
  } }, purgeRecord));
  const target = { ...server, lifecycle: purgeRecord };
  await assert.rejects(lifecycle.submit({}, target, { id: server.id, operationId: record.id, confirm: true }, () => {}), { code: 'confirmation_required' });
  await assert.rejects(lifecycle.submit({}, target, { id: server.id, operationId: record.id, confirm: true, eraseInstanceId: 'f'.repeat(32) }, () => {}), { code: 'confirmation_required' });
  reads(t, [{ stdout: JSON.stringify(purgeReceipt) }]);
  gateway(t, async (_client, script, _privilege, _password, limits) => {
    assert.equal(limits.maxBytes, 2 * 1024 ** 2);
    assert.match(script, new RegExp("'dispatch' '" + record.id + "' '" + record.instance_id + "'"));
    return { code: 0, stdout: JSON.stringify({ ok: true, data: { ...purgeReceipt, status: 'queued', resumable: true } }) };
  });
  const result = await lifecycle.submit({}, target, { id: server.id, operationId: record.id, confirm: true, eraseInstanceId: record.instance_id }, () => {});
  assert.equal(result.status, 'queued');
});
