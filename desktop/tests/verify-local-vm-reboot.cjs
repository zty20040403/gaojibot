'use strict';
// Power-cycle only the marker-verified local QA guest, never a supplied host.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('../scripts/local-vm.cjs');
const ssh = require('../lib/ssh.cjs');
const control = require('../lib/control.cjs');
const deploy = require('../lib/deploy.cjs');
const { shellLiteral } = require('../lib/contracts.cjs');

async function main() {
  assert.ok(process.argv.includes('--reboot'), 'Explicit --reboot is required for the disposable local VM');
  const target = deploy.bundle(path.resolve(__dirname, '../../packaging/build/deployment')).files['gaoji-runtime.tar.gz'].sha256;
  const file = path.join(vm.directory, 'reboot-acceptance-' + target + '.json');
  const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
  let state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : { target, job: deployment.job };
  assert.equal(state.target, target);
  assert.equal(state.job, deployment.job);
  const save = changes => {
    state = { ...state, ...changes };
    fs.writeFileSync(file + '.partial', JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
    fs.renameSync(file + '.partial', file);
  };
  const snapshot = async (client, beforeReboot = false) => {
    const read = async command => {
      const result = await ssh.execute(client, command, { timeoutMs: 60000 });
      assert.equal(result.code, 0, 'Local guest check failed');
      return result.stdout.trim();
    };
    const access = await control.accessFor(client, deployment.job);
    const versions = await control.request(client, access, 'versions');
    const settings = await control.request(client, access, 'status');
    const { services } = await control.request(client, access, 'services');
    assert.equal(versions.checksum, target);
    assert.equal(settings.desired.bot_enabled, false);
    assert.deepEqual(settings.instance.settings.models, []);
    assert.equal((await control.request(client, access, 'qqStatus')).online, false);
    assert.equal(services.find(item => item.name === 'database')?.health, 'healthy');
    assert.equal(services.find(item => item.name === 'napcat')?.state, 'running');
    const installer = 'gaoji-app-install-' + deployment.job + '.service';
    const bootLink = '/usr/local/lib/systemd/system/multi-user.target.wants/' + installer;
    const installerRetired = versions.checksum !== deployment.checksum;
    if (installerRetired) {
      await read('test ! -e ' + shellLiteral(bootLink) + ' && test ! -L ' + shellLiteral(bootLink));
    } else {
      assert.equal(await read('readlink ' + shellLiteral(bootLink)), '../' + installer,
        'Fresh installation must retain only its own original boot entry');
    }
    const installerState = await read('systemctl show --property=ActiveState --value ' + installer);
    const acceptedStates = installerRetired ? (beforeReboot ? ['inactive', 'failed'] : ['inactive']) : ['active'];
    assert.ok(acceptedStates.includes(installerState), 'Installer state does not match this runtime generation');
    assert.equal(await read('systemctl show --property=MainPID --value ' + installer), '0');
    for (const prefix of ['gaoji-app-', 'gaoji-app-control-']) {
      assert.equal(await read('systemctl show --property=ActiveState --value ' + prefix + access.instance_id + '.service'), 'active');
    }
    const installation = await deploy.status(client, deployment.job);
    assert.equal(installation.status, 'awaiting_setup');
    const compose = '/var/lib/gaoji-app/data/instances/' + access.instance_id + '/compose.json';
    const database = await read('sudo -n docker compose --file ' + shellLiteral(compose)
      + ' exec -T database psql -U gaoji -d gaoji -Atc '
      + shellLiteral('SELECT system_identifier FROM pg_control_system(); SELECT version_num FROM qq_bot.alembic_version;'));
    return { boot: await read('cat /proc/sys/kernel/random/boot_id'), instance: access.instance_id,
      revision: settings.instance.revision, checksum: versions.checksum, database,
      installed_at: installation.updated_at, installer_retired: installerRetired, services };
  };
  let client;
  try {
    if (!state.requested_at) {
      client = await vm.connection();
      const before = await snapshot(client, true);
      save({ before, requested_at: new Date().toISOString() });
      // Persist before sending. Lost acknowledgement must not trigger a second reboot.
      try {
        const result = await ssh.execute(client, 'sudo -n systemctl reboot', { timeoutMs: 15000 });
        assert.equal(result.code, 0, 'Guest reboot was rejected');
      } catch (error) { save({ dispatch_observation: error.code || error.name }); }
      client.end(); client = undefined;
      console.log(JSON.stringify({ reboot_requested: true, previous_boot: before.boot }));
    }
    const deadline = Date.now() + 600000;
    let after, lastError;
    while (Date.now() < deadline) {
      try {
        client = await vm.connection();
        const boot = await ssh.execute(client, 'cat /proc/sys/kernel/random/boot_id');
        assert.equal(boot.code, 0);
        assert.notEqual(boot.stdout.trim(), state.before.boot, 'Waiting for the requested reboot');
        after = await snapshot(client);
        break;
      } catch (error) { lastError = error.message; }
      finally { client?.end(); client = undefined; }
      await new Promise(resolve => setTimeout(resolve, 5000));
    }
    assert.ok(after, lastError || 'Reboot not verified; inspect the same local VM, do not reissue automatically');
    for (const field of ['instance', 'revision', 'checksum', 'database', 'installed_at']) {
      assert.equal(after[field], state.before[field], 'Reboot changed ' + field);
    }
    save({ completed: true, after, localhost_only: true, verified_at: new Date().toISOString() });
    console.log(JSON.stringify({ completed: true, previous_boot: state.before.boot, current_boot: after.boot,
      instance_id: after.instance, checksum: after.checksum, database_preserved: true,
      installer_retired: after.installer_retired }));
  } finally { client?.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
