'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('../scripts/local-vm.cjs');
const control = require('../lib/control.cjs');
const updates = require('../lib/updates.cjs');
const { bundle } = require('../lib/deploy.cjs');

async function main() {
  const directory = path.resolve(__dirname, '../../packaging/build/deployment');
  const target = bundle(directory).files['gaoji-runtime.tar.gz'].sha256;
  const file = path.join(vm.directory, process.argv.includes('--current-bundle')
    ? 'update-acceptance-' + target + '.json' : 'update-acceptance.json');
  const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
  let state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : { deployment };
  assert.equal(state.deployment.job, deployment.job);
  if (state.completed) { console.log(JSON.stringify(state)); return; }
  const save = changes => {
    state = { ...state, ...changes };
    fs.writeFileSync(file + '.partial', JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
    fs.renameSync(file + '.partial', file);
  };
  let client = await vm.connection();
  try {
    let result;
    if (state.update && process.argv.includes('--new-attempt')) {
      const prior = await updates.status(client, state.update);
      assert.equal(prior.status, 'failed', 'Only a verified terminal failure permits a new attempt');
      assert.equal(prior.finished, true);
      assert.equal(prior.resumable, false);
      save({ attempts: [...(state.attempts || []), { update: state.update, result: prior }],
        update: undefined, result: undefined });
    }
    if (!state.update) {
      const access = await control.accessFor(client, deployment.job);
      const settings = await control.request(client, access, 'status');
      assert.equal(settings.desired.bot_enabled, false);
      assert.deepEqual(settings.instance.settings.models, []);
      assert.equal((await control.request(client, access, 'qqStatus')).online, false);
      const plan = await updates.plan(client, directory, deployment);
      assert.equal(plan.canUpdate, true);
      save({ instance_id: access.instance_id });
      result = await updates.start(client, directory, state, { kind: 'update', confirm: true,
        revision: plan.current.revision, expectedChecksum: plan.current.checksum }, update => save({ update }));
    } else result = await updates.resume(client, directory, state.update, undefined, update => save({ update }));
    client.end(); client = await vm.connection();
    const deadline = Date.now() + 600000;
    let stage;
    while (!['completed', 'failed', 'interrupted'].includes(result.status) && Date.now() < deadline) {
      if (result.stage !== stage) {
        stage = result.stage;
        console.log(JSON.stringify({ status: result.status, stage, message: result.message }));
      }
      await new Promise(resolve => setTimeout(resolve, 3000));
      result = await updates.status(client, state.update);
    }
    save({ result });
    assert.equal(result.status, 'completed', result.message);
    assert.equal(result.finished, true);
    const access = await control.accessFor(client, deployment.job);
    assert.equal(access.instance_id, state.instance_id);
    const version = await control.request(client, access, 'versions');
    assert.equal(version.checksum, state.update.target_checksum);
    const settings = await control.request(client, access, 'status');
    assert.equal(settings.desired.bot_enabled, false);
    assert.deepEqual(settings.instance.settings.models, []);
    const services = await control.request(client, access, 'services');
    assert.equal(services.services.find(row => row.name === 'database')?.health, 'healthy');
    assert.equal(services.services.find(row => row.name === 'napcat')?.state, 'running');
    save({ completed: true, localhost_only: true, verified_at: new Date().toISOString(), services: services.services });
    console.log(JSON.stringify({ completed: true, id: state.update.id, checksum: version.checksum, instance_id: access.instance_id }));
  } finally { client.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
