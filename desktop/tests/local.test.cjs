'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { endpoint, LocalDeployment } = require('../lib/local.cjs');

test('local endpoint ignores remote Docker environment and context', () => {
  assert.equal(endpoint('darwin', '/Users/test').host, 'unix:///Users/test/.docker/run/docker.sock');
  assert.equal(endpoint('win32').host, 'npipe:////./pipe/dockerDesktopLinuxEngine');
  assert.throws(() => endpoint('linux'));
});
test('concurrent clicks share one preparation and persisted credentials are opaque', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-local-unit-'));
  try {
    const local = new LocalDeployment(directory, directory, {});
    let calls = 0;
    local.prepareStart = async () => { calls++; await new Promise(resolve => setTimeout(resolve, 5)); return { status: 'running' }; };
    const results = await Promise.all([local.start(), local.start()]);
    assert.equal(calls, 1); assert.deepEqual(results[0], results[1]);
    local.write({ schema: 1, job: 'a'.repeat(32), checksum: 'b'.repeat(64), port: 54321,
      botPort: 54322, engineId: 'test', credential: 'encrypted-data' });
    assert.equal(local.read().engineId, 'test');
    local.write({ ...local.read(), port: 54322 });
    assert.throws(() => local.read());
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

function originalFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-local-resume-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  // No deployment bundle exists here: reconnect must not need a matching old installer.
  const local = new LocalDeployment(directory, path.join(directory, 'no-bundle'), {});
  const record = { schema: 1, job: 'a'.repeat(32), checksum: 'b'.repeat(64), port: 54321,
    botPort: 54322, engineId: 'test', credential: 'encrypted-data' };
  const network = { Name: `gaoji-desktop-${record.job}`, Id: 'c'.repeat(64) };
  const controller = { Id: 'd'.repeat(64), Image: 'sha256:' + 'e'.repeat(64),
    Config: { Image: `gaoji-desktop-controller:${record.checksum}`, Cmd: ['serve'] },
    State: { Running: true },
    Mounts: [{ Type: 'volume', Name: `gaoji-desktop-${record.job}-data`, Destination: '/var/lib/gaoji-app', RW: true }],
    HostConfig: { NetworkMode: network.Name, PortBindings: { '8090/tcp': [{ HostIp: '127.0.0.1', HostPort: '54321' }] } },
    NetworkSettings: { Networks: { [network.Name]: { NetworkID: network.Id } } },
  };
  const resources = { container: controller, network, volume: {} }, mutations = [];
  local.write(record);
  local.inspect = async () => ({ ready: true });
  local.owned = async kind => resources[kind];
  local.checked = async args => {
    if (args[0] === 'image') {
      assert.deepEqual(args, ['image', 'inspect', controller.Image]);
      return JSON.stringify([{ Id: controller.Image, Config: { Labels: { 'gaoji.desktop.runtime-digest': record.checksum } } }]);
    }
    mutations.push(args);
    assert.ok(['start', 'restart'].includes(args[0]));
    assert.equal(args[1], controller.Id);
  };
  const status = { desktop_installation: { status: 'awaiting_setup', stage: 'ready' }, operation: null };
  local.request = async action => action === 'versions' ? { checksum: record.checksum } : status;
  return { local, record, controller, resources, mutations, status };
}

test('new client reconnects an older running instance without build, init or restart', async t => {
  const f = originalFixture(t);
  assert.equal((await f.local.start()).status, 'awaiting_setup');
  assert.equal(f.local.read().checksum, f.record.checksum);
  assert.deepEqual(f.mutations, []);
});

test('stopped older controller is started by immutable ID without replacing data', async t => {
  const f = originalFixture(t);
  f.controller.State.Running = false;
  assert.equal((await f.local.start()).stage, 'reconnect');
  assert.deepEqual(f.mutations, [['start', f.controller.Id]]);
  assert.equal(f.local.read().credential, f.record.credential);
});

test('missing legacy data and wrong mounts cannot create an empty replacement', async t => {
  for (const mode of ['missing-volume', 'wrong-volume', 'wrong-network', 'public-port']) await t.test(mode, async child => {
    const f = originalFixture(child);
    if (mode === 'missing-volume') f.resources.volume = null;
    if (mode === 'wrong-volume') f.controller.Mounts[0].Name = 'another-user-volume';
    if (mode === 'wrong-network') f.resources.network.Id = 'f'.repeat(64);
    if (mode === 'public-port') f.controller.HostConfig.PortBindings['8090/tcp'][0].HostIp = '0.0.0.0';
    await assert.rejects(f.local.start(), { code: mode === 'missing-volume' ? 'local_data_missing' : 'local_ownership' });
    assert.deepEqual(f.mutations, []);
  });
});

test('unreachable or busy running controller is not restarted', async t => {
  for (const mode of ['unreachable', 'busy', 'maintenance']) await t.test(mode, async child => {
    const f = originalFixture(child);
    if (mode === 'unreachable') f.local.request = async () => { throw new Error('test timeout'); };
    else {
      f.status.desktop_installation.status = 'failed';
      if (mode === 'busy') f.status.operation = { status: 'running' };
      else f.status.maintenance_pending = true;
    }
    await assert.rejects(f.local.start(), { code: mode === 'unreachable' ? 'local_controller_unavailable' : 'local_busy' });
    assert.deepEqual(f.mutations, []);
  });
});

test('confirmed failed initialization resumes the same controller', async t => {
  const f = originalFixture(t);
  f.status.desktop_installation.status = 'failed';
  assert.equal((await f.local.start()).stage, 'reconnect');
  assert.deepEqual(f.mutations, [['restart', f.controller.Id]]);
});
