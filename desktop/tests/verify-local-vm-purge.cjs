'use strict';
// Real deletion of the fixed migrated QA target, never an arbitrary server.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto'), net = require('node:net');
const args = process.argv.slice(2);
const afterReboot = args[0] === '--after-reboot';
if (afterReboot) args.shift();
assert.ok(args.length === 0 || args.length === 2 && args[0] === '--source-checksum' && /^[a-f0-9]{64}$/.test(args[1]));
process.env.GAOJI_QA_DISTRIBUTION = 'ubuntu';
process.env.GAOJI_QA_SLOT = 'restore-target';
const vm = require('../scripts/local-vm.cjs');
const control = require('../lib/control.cjs'), lifecycle = require('../lib/lifecycle.cjs');
const { bundle } = require('../lib/deploy.cjs'), { execute } = require('../lib/ssh.cjs');
const qaRoot = path.resolve(__dirname, '../../packaging/build');
const runtimeChecksum = bundle(path.join(qaRoot, 'deployment')).files['gaoji-runtime.tar.gz'].sha256;
const checksum = args[1] || runtimeChecksum;
const file = path.join(vm.directory, 'purge-acceptance-' + checksum + '.json');
const migration = JSON.parse(fs.readFileSync(path.join(qaRoot, 'linux-qa-ubuntu', 'migration-' + checksum + '.json')));
const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json')));
const code = fs.readFileSync(path.join(__dirname, 'qa-purge-guest.py'), 'utf8');
let state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : null;
function save(change) {
  state = { ...state, ...change };
  fs.writeFileSync(file + '.partial', JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(file + '.partial', file);
}
async function guest(client, action) {
  const payload = Buffer.from(JSON.stringify({ action, vm_id: vm.metadata().id, job: deployment.job,
    instance_id: state.instance_id, run: state.run })).toString('base64');
  const result = await execute(client, 'sudo -n python3 -', { timeoutMs: 240000, maxBytes: 128 * 1024,
    stdin: code + '\nimport base64\nmain(json.loads(base64.b64decode("' + payload + '")))\n' });
  assert.equal(result.code, 0, result.stderr);
  return JSON.parse(result.stdout);
}
async function ready(client) {
  const access = await control.accessFor(client, deployment.job);
  const settings = await control.request(client, access, 'status');
  assert.equal(access.instance_id, migration.target.instance_id);
  assert.equal((await control.request(client, access, 'versions')).checksum, runtimeChecksum);
  assert.equal(settings.desired.bot_enabled, false);
  assert.deepEqual(settings.instance.settings.models, []);
  assert.equal(settings.instance.settings.bot_qq, '');
  assert.equal((await control.request(client, access, 'qqStatus')).online, false);
  return access;
}
async function main() {
  assert.equal(migration.completed, true);
  assert.ok(migration.maintenance_verified_at);
  assert.ok(fs.statSync(migration.archive).isFile(), 'Keep the source QA backup outside this guest');
  await new Promise((resolve, reject) => {
    const socket = net.createConnection(path.join(qaRoot, 'linux-qa-ubuntu/qmp.sock'));
    socket.once('connect', () => { socket.destroy(); reject(Error('Source QA guest must be off')); });
    socket.once('error', error => ['ENOENT', 'ECONNREFUSED'].includes(error.code) ? resolve() : reject(error));
    socket.setTimeout(3000, () => { socket.destroy(); reject(Error('Source QA state unknown')); });
  });
  let client = await vm.connection();
  try {
    if (!state) {
      assert.equal(afterReboot, false, 'There is no completed purge to verify');
      const access = await ready(client);
      save({ schema: 1, deployment, instance_id: access.instance_id, checksum,
        run: crypto.randomUUID().replaceAll('-', ''), phase: 'seed' });
    }
    assert.equal(state.checksum, checksum);
    assert.equal(state.deployment.job, deployment.job);
    assert.equal(state.instance_id, migration.target.instance_id);
    if (!afterReboot && runtimeChecksum !== (state.verified_runtime || state.checksum)) {
      assert.equal(state.phase, 'purge');
      assert.equal(state.recovery_result?.status, 'completed', 'Re-enable the retained QA instance through the App first');
      const update = JSON.parse(fs.readFileSync(path.join(vm.directory, 'update-acceptance-' + runtimeChecksum + '.json')));
      assert.equal(update.completed, true, 'Use the actual App update before retesting');
      assert.equal(update.instance_id, state.instance_id);
      await ready(client);
      const failed = await lifecycle.status(client, state.lifecycle);
      assert.equal(failed.status, 'failed');
      assert.equal(failed.resumable, false);
      save({ attempts: [...(state.attempts || []), { checksum: state.verified_runtime || state.checksum,
        lifecycle: state.lifecycle, preview: state.preview, failure: failed }],
        verified_runtime: runtimeChecksum, lifecycle: null, preview: null, initial: null, phase: 'uninstall' });
    }
    if (afterReboot) {
      assert.equal(state.phase, 'complete', 'Finish purge and fixture cleanup first');
      const after = await guest(client, 'verify-tombstone');
      assert.notEqual(after.boot_id, state.after.boot_id, 'No reboot has occurred');
      assert.equal(after.purge_task, state.lifecycle.id);
      assert.deepEqual(after.images, state.after.images);
      const removedFixture = { container: state.before.bystander.id, volume: 'gaoji-qa-bystander-' + state.run };
      for (const kind of ['container', 'volume']) {
        assert.deepEqual(after.resources[kind], state.after.resources[kind].filter(id => id !== removedFixture[kind]));
      }
      // Docker can recreate its built-in bridge on boot. The purge itself must
      // preserve exact IDs; this isolated post-boot guest must have only defaults.
      assert.deepEqual(after.networks.map(({ name, driver, scope }) => ({ name, driver, scope })), [
        { name: 'bridge', driver: 'bridge', scope: 'local' },
        { name: 'host', driver: 'host', scope: 'local' },
        { name: 'none', driver: 'null', scope: 'local' },
      ]);
      assert.deepEqual(after.resources.network, after.networks.map(network => network.id).sort());
      assert.equal(state.after.resources.network.length, 3);
      for (const network of after.networks.filter(network => network.name !== 'bridge')) {
        assert.ok(state.after.resources.network.includes(network.id), 'A non-bridge default network changed');
      }
      save({ reboot_verified_at: new Date().toISOString(), reboot_boot_id: after.boot_id,
        reboot_snapshot: after });
      console.log(JSON.stringify({ reboot_verified: true, instance_still_removed: true, no_resource_resurrection: true }));
      return;
    }
    if (state.phase === 'complete') { console.log('This QA purge is already verified; no operation repeated.'); return; }
    if (state.phase === 'seed') {
      await ready(client);
      const before = await guest(client, 'seed');
      save({ before, phase: 'uninstall' });
    }
    for (const kind of ['uninstall', 'purge']) {
      if (state.phase !== kind) continue;
      if (state.lifecycle?.kind !== kind) {
        await lifecycle.prepare(client, state, { kind }, record => save({ lifecycle: record }));
      }
      if (kind === 'purge' && !state.preview) {
        const preview = await lifecycle.status(client, state.lifecycle);
        assert.equal(preview.status, 'preview');
        const initial = await guest(client, 'snapshot');
        assert.equal(initial.directory_exists, true, 'Preview must not delete data');
        assert.deepEqual(initial.bystander, state.before.bystander);
        assert.ok(preview.delete_resources.volumes.some(v => v.name === state.before.own_volume));
        const own = preview.delete_resources.containers.map(v => v.id);
        assert.ok(!own.includes(initial.bystander.id));
        save({ preview, initial });
      }
      // Preparing and dispatching are distinct; preserve the ID before either
      // acknowledgement can be lost. A stopped observer never creates a new ID.
      let result = await lifecycle.status(client, state.lifecycle);
      if (['preview', 'unconfirmed', 'interrupted'].includes(result.status) || result.status === 'failed' && result.resumable) {
        result = await lifecycle.submit(client, state, { operationId: state.lifecycle.id, confirm: true,
          ...(kind === 'purge' ? { eraseInstanceId: state.instance_id } : {}) }, record => save({ lifecycle: record }));
      }
      client.end(); client = await vm.connection();
      const deadline = Date.now() + 5 * 60 * 1000;
      let stage;
      while (['queued', 'running', 'unconfirmed'].includes(result.status)) {
        assert.ok(Date.now() < deadline, 'Observer expired; inspect the same durable task');
        if (stage !== result.stage) { stage = result.stage; console.log(JSON.stringify({ kind, stage, status: result.status })); }
        await new Promise(resolve => setTimeout(resolve, 3000));
        result = await lifecycle.status(client, state.lifecycle);
      }
      save({ [kind + '_result']: result });
      assert.equal(result.status, 'completed', result.message);
      assert.equal(result.service_state, kind === 'purge' ? 'removed' : 'retained');
      save({ lifecycle: { ...state.lifecycle, settled: true }, phase: kind === 'uninstall' ? 'purge' : 'verify' });
      console.log(JSON.stringify({ kind, status: result.status, service_state: result.service_state }));
    }
    if (state.phase === 'verify') {
      // The public receipt precedes the oneshot's own unit retirement.
      const unit = 'gaoji-app-lifecycle-' + state.lifecycle.id + '.service';
      const deadline = Date.now() + 60000;
      while (true) {
        const status = await execute(client, 'systemctl show ' + unit + ' --property=MainPID,LoadState,ActiveState');
        const values = Object.fromEntries(status.stdout.trim().split('\n').filter(line => line.includes('=')).map(line => line.split('=')));
        if (values.MainPID === '0' && ['inactive', 'failed'].includes(values.ActiveState)
            && ['loaded', 'not-found'].includes(values.LoadState)) break;
        assert.ok(Date.now() < deadline, 'Purge executor has not exited');
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      const after = await guest(client, 'verify-removed');
      assert.equal(after.purge_task, state.lifecycle.id);
      assert.equal(after.directory_exists, false);
      assert.deepEqual(after.bystander, state.before.bystander, 'Unrelated resource changed');
      assert.deepEqual(after.images, state.initial.images, 'Shared images changed');
      for (const [kind, plural] of [['container', 'containers'], ['volume', 'volumes'], ['network', 'networks']]) {
        const deleted = new Set(state.preview.delete_resources[plural].map(v => v.id));
        assert.deepEqual(after.resources[kind], state.initial.resources[kind].filter(id => !deleted.has(id)), kind + ' inventory differs from approved deletion');
      }
      save({ after, verified_at: new Date().toISOString(), phase: 'cleanup' });
    }
    if (state.phase === 'cleanup') {
      assert.equal((await guest(client, 'cleanup')).fixture_cleaned, true);
      save({ phase: 'complete', fixture_cleaned: true });
      console.log(JSON.stringify({ completed: true, data_removed: true, units_removed: true, access_revoked: true,
        unrelated_container_untouched: true, shared_images_untouched: true, fixture_cleaned: true }));
    }
  } finally { client.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
