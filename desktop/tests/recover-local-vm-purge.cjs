'use strict';
// Re-enable only the disposable target after its verified rejected purge.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
process.env.GAOJI_QA_DISTRIBUTION = 'ubuntu';
process.env.GAOJI_QA_SLOT = 'restore-target';
const vm = require('../scripts/local-vm.cjs'), lifecycle = require('../lib/lifecycle.cjs');
const records = fs.readdirSync(vm.directory).filter(n => /^purge-acceptance-[a-f0-9]{64}\.json$/.test(n));
assert.equal(records.length, 1, 'Choose no ambiguous QA records');
const filename = path.join(vm.directory, records[0]);
let state = JSON.parse(fs.readFileSync(filename));
function save(changes) {
  state = { ...state, ...changes };
  fs.writeFileSync(filename + '.partial', JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(filename + '.partial', filename);
}
async function main() {
  let client = await vm.connection();
  try {
    assert.equal(state.phase, 'purge');
    assert.equal(state.lifecycle.kind, 'purge');
    const failed = await lifecycle.status(client, state.lifecycle);
    assert.equal(failed.status, 'failed');
    assert.equal(failed.resumable, false);
    assert.equal(failed.service_state, 'retained');
    // The fixed guest witness also checks that no real configuration was added.
    const witness = fs.readFileSync(path.join(__dirname, 'qa-purge-guest.py'), 'utf8');
    const data = Buffer.from(JSON.stringify({ action: 'snapshot', vm_id: vm.metadata().id,
      job: state.deployment.job, instance_id: state.instance_id, run: state.run })).toString('base64');
    const { execute } = require('../lib/ssh.cjs');
    const result = await execute(client, 'sudo -n python3 -', { timeoutMs: 240000,
      stdin: witness + '\nimport base64\nmain(json.loads(base64.b64decode("' + data + '")))\n' });
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout).bystander, state.before.bystander);
    if (!state.recovery) {
      await lifecycle.prepare(client, state, { kind: 'reinstall' }, recovery => save({ recovery, rejected_purge: failed }));
    }
    let current = await lifecycle.status(client, state.recovery);
    if (['preview', 'unconfirmed', 'interrupted'].includes(current.status)) {
      current = await lifecycle.submit(client, { deployment: state.deployment, lifecycle: state.recovery },
        { operationId: state.recovery.id, confirm: true }, recovery => save({ recovery }));
    }
    client.end(); client = await vm.connection();
    const deadline = Date.now() + 300000;
    while (['queued', 'running', 'unconfirmed'].includes(current.status)) {
      assert.ok(Date.now() < deadline, 'Recovery observer expired; inspect the same operation');
      await new Promise(resolve => setTimeout(resolve, 3000));
      current = await lifecycle.status(client, state.recovery);
    }
    save({ recovery_result: current });
    assert.equal(current.status, 'completed', current.message);
    assert.equal(current.service_state, 'installed');
    console.log(JSON.stringify({ recovered_for_update: true, operation: current.id }));
  } finally { client.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
