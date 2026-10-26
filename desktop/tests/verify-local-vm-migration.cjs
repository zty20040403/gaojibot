'use strict';
// Real controller/export/import/restore in the two fixed disposable QA guests.
// No arbitrary hosts, credentials, instance paths or production QQ accounts.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const role = process.argv[2];
assert.ok(['source', 'target', 'verify-reboot', 'handover', 'verify-maintenance'].includes(role));
process.env.GAOJI_QA_DISTRIBUTION = 'ubuntu';
process.env.GAOJI_QA_SLOT = role === 'source' ? 'primary' : 'restore-target';
const vm = require('../scripts/local-vm.cjs');
const control = require('../lib/control.cjs');
const transfer = require('../lib/backup-transfer.cjs');
const { execute } = require('../lib/ssh.cjs');
const { bundle } = require('../lib/deploy.cjs');
const qaRoot = path.resolve(__dirname, '../../packaging/build');
const sourceRoot = path.join(qaRoot, 'linux-qa-ubuntu');
const checksum = bundle(path.join(qaRoot, 'deployment')).files['gaoji-runtime.tar.gz'].sha256;
const recordPath = path.join(sourceRoot, 'migration-' + checksum + '.json');
const guestScript = fs.readFileSync(path.join(__dirname, 'qa-migration-guest.py'), 'utf8');
let state = fs.existsSync(recordPath) ? JSON.parse(fs.readFileSync(recordPath, 'utf8')) : null;
function save(changes) {
  state = { ...state, ...changes };
  fs.writeFileSync(recordPath + '.partial', JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(recordPath + '.partial', recordPath);
}
async function guest(client, action, witnessRole = role) {
  const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
  const request = { action, role: witnessRole, vm_id: vm.metadata().id, job: deployment.job, run: state.run };
  const encoded = Buffer.from(JSON.stringify(request)).toString('base64');
  const result = await execute(client, 'sudo -n python3 -', { timeoutMs: 120000, maxBytes: 128 * 1024,
    stdin: guestScript + '\nimport base64\nmain(json.loads(base64.b64decode("' + encoded + '")))\n' });
  assert.equal(result.code, 0, result.stderr);
  return JSON.parse(result.stdout);
}
async function ready(client, access) {
  const current = await control.request(client, access, 'status');
  assert.equal((await control.request(client, access, 'versions')).checksum, checksum);
  assert.equal(current.desired.bot_enabled, false);
  assert.deepEqual(current.instance.settings.models, []);
  assert.equal(current.instance.settings.bot_qq, '');
  const deadline = Date.now() + 120000;
  while (true) {
    let qq;
    try { qq = await control.request(client, access, 'qqStatus'); }
    catch (error) {
      if (error.code !== 'setup_failed' || Date.now() >= deadline) throw error;
      await new Promise(resolve => setTimeout(resolve, 3000));
      continue;
    }
    assert.equal(qq.online, false, 'A real QQ login must never be used for this test');
    break;
  }
  return current;
}
async function operation(client, access, name, kind, extra = {}) {
  const before = await ready(client, access);
  if (!state[name]) save({ [name]: { id: crypto.randomUUID().replaceAll('-', ''), kind,
    revision: before.instance.revision, ...extra } });
  const request = state[name];
  let current = before.operation;
  if (current?.id !== request.id) {
    assert.ok(!current || !['queued', 'running'].includes(current.status), 'Another operation is running');
    // ID was persisted before dispatch. A lost reply is observed, never replaced.
    current = await control.request(client, access, 'operation', request);
  }
  const deadline = Date.now() + 10 * 60 * 1000;
  let stage;
  while (['queued', 'running'].includes(current.status)) {
    assert.ok(Date.now() < deadline, 'Observer expired; query this same operation before proceeding');
    if (stage !== current.stage) {
      stage = current.stage;
      console.log(JSON.stringify({ role, kind, stage, status: current.status }));
    }
    await new Promise(resolve => setTimeout(resolve, 3000));
    current = (await control.request(client, access, 'status')).operation;
    assert.equal(current?.id, request.id, 'Operation ownership changed');
  }
  save({ [name + '_result']: current });
  assert.equal(current.status, 'completed', current.message);
  return current;
}
function assertFixture(value) {
  assert.equal(value.witness.conversations.length, 2);
  assert.equal(value.witness.messages.length, 4);
  assert.equal(value.witness.vectors.length, 2);
  assert.equal(value.witness.files.length, 2);
  for (const c of value.witness.conversations) {
    const rows = value.witness.messages.filter(m => m.conversation_id === c.conversation_id);
    const question = rows.find(m => m.native_message_id === 'qa-question');
    const reply = rows.find(m => m.native_message_id === 'qa-reply');
    assert.equal(reply.reply_to_canonical_message_id, question.canonical_message_id);
    assert.ok(question.rendered_text.includes('中文消息'));
  }
  for (const v of value.witness.vectors) assert.equal(JSON.parse(v.embedding).length, 1024);
}
async function checkRestoredData(client, access) {
  const current = await ready(client, access);
  const actual = await guest(client, 'witness', 'source');
  assertFixture(actual);
  assert.deepEqual(actual.witness, state.source.witness);
  assert.deepEqual(actual.settings, state.source.settings);
  assert.equal(actual.instance_id, state.target.instance_id);
  assert.equal(actual.revision, state.target.revision + 1);
  assert.deepEqual(actual.ports, state.target.ports);
  assert.equal(actual.fingerprints['control.json'], state.target.fingerprints['control.json']);
  for (const name of ['admin.secret', 'postgres.password']) assert.equal(actual.fingerprints[name], state.source.fingerprints[name]);
  const old = await guest(client, 'witness', 'target');
  assert.deepEqual(old.witness, { conversations: [], messages: [], vectors: [], files: [] });
  const services = await control.request(client, access, 'services');
  assert.equal(services.services.find(s => s.name === 'database')?.health, 'healthy');
  return { current, actual, services };
}
async function checkTarget(client, access) {
  const { current, actual, services } = await checkRestoredData(client, access);
  assert.ok(!services.services.some(s => ['bot', 'napcat'].includes(s.name) && s.state === 'running'));
  assert.equal(current.qq_handover?.state, 'pending');
  assert.equal(current.qq_handover?.source_instance_id, state.source.instance_id);
  assert.equal((await control.request(client, access, 'qqStatus')).phase, 'handover_required');
  return actual;
}
async function main() {
  // The two guests must not overlap while moving QQ session data.
  if (role !== 'source') {
    // This module selects the target, so check the source's exact QMP endpoint
    // independently without allowing an arbitrary host or sending a command.
    const net = require('node:net');
    await new Promise((resolve, reject) => {
      const socket = net.createConnection(path.join(sourceRoot, 'qmp.sock'));
      socket.once('connect', () => { socket.destroy(); reject(Error('Source QA VM must be off before target restore')); });
      socket.once('error', error => ['ENOENT', 'ECONNREFUSED'].includes(error.code) ? resolve() : reject(error));
      socket.setTimeout(3000, () => { socket.destroy(); reject(Error('Source VM state is unknown')); });
    });
  }
  if (!state) {
    assert.equal(role, 'source', 'Run and verify the source first');
    save({ schema: 1, checksum, run: crypto.randomUUID().replaceAll('-', ''), password: crypto.randomBytes(32).toString('base64url') });
  }
  assert.equal(state.checksum, checksum);
  const client = await vm.connection();
  try {
    const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
    const access = await control.accessFor(client, deployment.job);
    await ready(client, access);
    if (role === 'source') {
      if (state.source_complete) { console.log('Source migration fixture already exported; no operation repeated.'); return; }
      const witness = await guest(client, state.source ? 'witness' : 'seed');
      assertFixture(witness);
      if (state.source) assert.deepEqual(witness.witness, state.source.witness);
      else save({ source: witness });
      const receipt = await operation(client, access, 'backup', 'backup');
      assert.match(receipt.backup_id, /^[a-f0-9]{32}$/);
      const filename = path.join(sourceRoot, 'migration-' + state.run + '.gaojibak');
      await transfer.exportBackup(client, access, receipt.backup_id, state.password, filename);
      assert.deepEqual((await guest(client, 'witness')).witness, state.source.witness);
      save({ source_complete: true, archive: filename });
      console.log(JSON.stringify({ source_complete: true, messages: 4, vectors: 2, files: 2, archive_bytes: fs.statSync(filename).size }));
    } else if (role === 'target') {
      assert.equal(state.source_complete, true);
      assert.ok(!state.handover, 'Handover already requested; never replay the previous restore');
      if (!state.target) save({ target: await guest(client, 'seed') });
      assert.notEqual(access.instance_id, state.source.instance_id);
      if (!state.restore) {
        const imported = await transfer.importBackup(client, access, state.archive, state.password);
        assert.equal(imported.imported, true);
        assert.deepEqual((await guest(client, 'witness')).witness, state.target.witness, 'Import changed live data');
        const second = await transfer.importBackup(client, access, state.archive, state.password);
        assert.equal(second.id, imported.id);
        assert.equal(second.already_imported, true);
        save({ imported_id: imported.id });
      }
      await operation(client, access, 'restore', 'restore', { backup_id: state.imported_id, confirm: true });
      const restored = await checkTarget(client, access);
      save({ target_complete: true, restored_boot_id: restored.boot_id });
      console.log(JSON.stringify({ target_complete: true, data_identical: true, target_identity_preserved: true, qq_pending: true }));
    } else if (role === 'verify-reboot') {
      assert.equal(state.target_complete, true);
      const current = await checkTarget(client, access);
      assert.notEqual(current.boot_id, state.restored_boot_id, 'Reboot has not occurred');
      save({ reboot_verified: true });
      console.log(JSON.stringify({ reboot_verified: true, data_identical: true, qq_still_pending: true }));
    } else if (role === 'handover') {
      assert.equal(state.reboot_verified, true);
      await operation(client, access, 'handover', 'resume_qq', { confirm: true });
      const current = await ready(client, access);
      assert.equal(current.qq_handover, null);
      assert.ok((await control.request(client, access, 'qqStatus')).qrcode);
      assert.deepEqual((await guest(client, 'witness', 'source')).witness, state.source.witness);
      save({ completed: true, verified_at: new Date().toISOString() });
      console.log(JSON.stringify({ completed: true, qq_login_entry_ready: true, bot_disabled: true, real_account_login_tested: false }));
    } else {
      assert.equal(state.completed, true, 'Finish migration and handover first');
      const maintenance = JSON.parse(fs.readFileSync(path.join(vm.directory, 'lifecycle-acceptance-' + checksum + '.json')));
      assert.equal(maintenance.instance_id, state.target.instance_id);
      assert.equal(maintenance.phase, 'complete');
      assert.equal(maintenance.uninstall_result.status, 'completed');
      assert.equal(maintenance.reinstall_result.status, 'completed');
      const { current, services } = await checkRestoredData(client, access);
      assert.equal(current.qq_handover, null);
      assert.equal(services.services.find(s => s.name === 'napcat')?.state, 'running');
      assert.ok(!services.services.some(s => s.name === 'bot' && s.state === 'running'));
      assert.ok((await control.request(client, access, 'qqStatus')).qrcode);
      save({ maintenance_verified_at: new Date().toISOString() });
      console.log(JSON.stringify({ maintenance_verified: true, data_identical: true, target_identity_preserved: true, qq_login_entry_ready: true }));
    }
  } finally { client.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
