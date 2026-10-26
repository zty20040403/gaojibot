'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { LocalUpdates } = require('../lib/local-updates.cjs');

const job = 'a'.repeat(32), id = 'b'.repeat(32), old = 'c'.repeat(64), next = 'd'.repeat(64);
function fixture() {
  let record = { job, checksum: old, port: 18080, botPort: 18081, credential: 'unchanged-private-credential',
    update: { id, job, kind: 'update', expected_checksum: old, target_checksum: next, revision: 2, runnerChecksum: next } };
  let receipt = { schema: 1, id, job, target_checksum: next, current_checksum: old, controller_checksum: old,
    status: 'running', finished: false, message: 'progress', stage: 'backup' };
  const row = { Id: 'e'.repeat(64), Image: 'sha256:' + 'f'.repeat(64), State: { Running: true },
    Config: { Image: `gaoji-desktop-controller:${next}`, Cmd: ['update', id], Labels: { 'gaoji.desktop.update': id } },
    Mounts: [{ Name: `gaoji-desktop-${job}-data`, Destination: '/var/lib/gaoji-app', Type: 'volume', RW: true },
      { Type: 'bind', Source: '/var/run/docker.sock', Destination: '/var/run/docker.sock', RW: true }],
    HostConfig: { NetworkMode: `gaoji-desktop-${job}` } };
  const calls = [];
  const local = { read: () => record, write: value => { record = value; }, inspect: async () => ({ ready: true }),
    owned: async kind => kind === 'container' ? row : {}, checked: async args => {
      calls.push(args);
      if (args[0] === 'image') return JSON.stringify([{ Config: { Labels: { 'gaoji.desktop.runtime-digest': next } } }]);
      if (args[0] === 'logs') return JSON.stringify(receipt);
      return '';
    } };
  return { local, updates: new LocalUpdates(local), row, calls, record: () => record, receipt: value => { receipt = value; } };
}
test('only a verified final receipt changes runtime identity, never credentials or ports', async () => {
  const f = fixture();
  await f.updates.status(); assert.equal(f.record().checksum, old);
  f.receipt({ id, job, target_checksum: next, current_checksum: next, controller_checksum: next,
    status: 'completed', finished: true, message: 'done' });
  await f.updates.status();
  assert.equal(f.record().checksum, next);
  assert.equal(f.record().controllerChecksum, next);
  assert.equal(f.record().credential, 'unchanged-private-credential');
  assert.equal(f.record().port, 18080);
});
test('wrong mounts and foreign receipts are refused without mutation', async () => {
  const f = fixture(); f.row.Mounts[0].Name = 'somebody-else';
  await assert.rejects(f.updates.status(), { code: 'update_ownership' });
  assert.equal(f.calls.length, 0);
  const other = fixture(); other.receipt({ id: '0'.repeat(32) });
  await assert.rejects(other.updates.status(), { code: 'update_status' });
  assert.equal(other.record().checksum, old);
});
test('resume restarts only the original stopped worker and does not rebuild', async () => {
  const f = fixture(); f.row.State.Running = false;
  const status = await f.updates.resume();
  assert.equal(status.status, 'queued');
  assert.deepEqual(f.calls.filter(a => !['image','logs'].includes(a[0])), [['start', f.row.Id]]);
});
test('completed workers are not replayed; unfinished tasks block new updates', async () => {
  const f = fixture();
  await assert.rejects(f.updates.start({ kind: 'update', confirm: true }), { code: 'update_busy' });
  f.receipt({ id, job, target_checksum: next, current_checksum: old, controller_checksum: next,
    status: 'failed', finished: true });
  assert.equal((await f.updates.resume()).finished, true);
  assert.equal(f.calls.filter(a => ['start','restart','create','run'].includes(a[0])).length, 0);
});
test('concurrent clicks cannot replace the original pending task', async () => {
  const f = fixture(); let release;
  f.updates.prepareStart = () => new Promise(resolve => { release = resolve; });
  const first = f.updates.start({});
  await assert.rejects(f.updates.start({}), { code: 'update_busy' });
  await assert.rejects(f.updates.resume(), { code: 'update_busy' });
  release('same task');
  assert.equal(await first, 'same task');
});
