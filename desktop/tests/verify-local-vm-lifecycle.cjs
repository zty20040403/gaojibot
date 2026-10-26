'use strict';
// Real maintenance only inside the marker-verified disposable localhost VM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('../scripts/local-vm.cjs');
const ssh = require('../lib/ssh.cjs');
const control = require('../lib/control.cjs');
const lifecycle = require('../lib/lifecycle.cjs');
const { shellLiteral } = require('../lib/contracts.cjs');
const { bundle } = require('../lib/deploy.cjs');

async function main() {
  const target = bundle(path.resolve(__dirname, '../../packaging/build/deployment')).files['gaoji-runtime.tar.gz'].sha256;
  const file = path.join(vm.directory, process.argv.includes('--current-bundle')
    ? 'lifecycle-acceptance-' + target + '.json' : 'lifecycle-acceptance.json');
  const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
  let state = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : { deployment, phase: 'prepare' };
  assert.equal(state.deployment.job, deployment.job);
  if (state.phase === 'complete') { console.log(JSON.stringify(state)); return; }
  let client = await vm.connection();
  const save = changes => {
    state = { ...state, ...changes };
    const temporary = file + '.partial';
    fs.writeFileSync(temporary, JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
    fs.renameSync(temporary, file);
  };
  const run = async command => {
    const result = await ssh.execute(client, command, { timeoutMs: 60000 });
    assert.equal(result.code, 0, 'Local guest acceptance command failed');
    return result.stdout.trim();
  };
  const databaseIdentity = async () => run('sudo -n docker compose --file '
    + shellLiteral('/var/lib/gaoji-app/data/instances/' + state.instance_id + '/compose.json')
    + ' exec -T database psql -U gaoji -d gaoji -Atc '
    + shellLiteral('SELECT system_identifier FROM pg_control_system(); SELECT version_num FROM qq_bot.alembic_version;'));
  try {
    if (state.phase === 'prepare') {
      const access = await control.accessFor(client, deployment.job);
      if (process.argv.includes('--current-bundle')) {
        assert.equal((await control.request(client, access, 'versions')).checksum, target);
      }
      const settings = await control.request(client, access, 'status');
      const qq = await control.request(client, access, 'qqStatus');
      assert.equal(qq.online, false, 'Do not operate a logged-in QQ');
      assert.equal(settings.desired.bot_enabled, false);
      assert.deepEqual(settings.instance.settings.models, []);
      save({ instance_id: access.instance_id, revision: settings.instance.revision });
      save({ database_before: await databaseIdentity(), phase: 'uninstall' });
    }
    for (const kind of ['uninstall', 'reinstall']) {
      if (state.phase !== kind) continue;
      if (state.lifecycle?.kind !== kind) {
        await lifecycle.prepare(client, state, { kind }, value => save({ lifecycle: value }));
      }
      let result = await lifecycle.status(client, state.lifecycle);
      if (['preview', 'interrupted', 'unconfirmed'].includes(result.status) || result.status === 'failed' && result.resumable) {
        result = await lifecycle.submit(client, state, { operationId: state.lifecycle.id, confirm: true },
          value => save({ lifecycle: value }));
      }
      // The worker must survive losing the desktop connection after dispatch.
      client.end(); client = await vm.connection();
      const deadline = Date.now() + 240000;
      while (!['completed', 'failed'].includes(result.status) && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        result = await lifecycle.status(client, state.lifecycle);
      }
      save({ [kind + '_result']: result });
      assert.equal(result.status, 'completed', result.message);
      assert.equal(result.service_state, kind === 'uninstall' ? 'retained' : 'installed');
      if (kind === 'uninstall') {
        const services = ['gaoji-app-', 'gaoji-app-control-', 'gaoji-app-sandbox-'].map(prefix => prefix + state.instance_id + '.service');
        services.push('gaoji-app-install-' + deployment.job + '.service');
        for (const unit of services) {
          const result = await ssh.execute(client, 'systemctl show --property=LoadState,ActiveState,MainPID,FragmentPath ' + unit);
          const values = Object.fromEntries(result.stdout.trim().split('\n').map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)]));
          assert.equal(values.LoadState, 'not-found', unit + ' still has a loaded definition');
          assert.equal(values.FragmentPath, '');
          assert.equal(values.MainPID, '0');
          assert.ok(['inactive', 'failed'].includes(values.ActiveState), unit + ' has not stopped');
          const link = '/usr/local/lib/systemd/system/multi-user.target.wants/' + unit;
          await run('test ! -e ' + shellLiteral(link) + ' && test ! -L ' + shellLiteral(link));
        }
        save({ lifecycle: { ...state.lifecycle, settled: true }, phase: 'reinstall' });
      } else {
        const installer = 'gaoji-app-install-' + deployment.job + '.service';
        const bootLink = '/usr/local/lib/systemd/system/multi-user.target.wants/' + installer;
        await run('test ! -e ' + shellLiteral(bootLink) + ' && test ! -L ' + shellLiteral(bootLink));
        const installerState = await run('systemctl show --property=ActiveState --value ' + installer);
        assert.ok(['inactive', 'failed'].includes(installerState), 'Completed installer must not run');
        assert.equal(await run('systemctl show --property=MainPID --value ' + installer), '0');
        assert.equal(await databaseIdentity(), state.database_before, 'Original database identity or schema changed');
        const access = await control.accessFor(client, deployment.job);
        assert.equal(access.instance_id, state.instance_id);
        const settings = await control.request(client, access, 'status');
        assert.equal(settings.instance.revision, state.revision);
        assert.equal(settings.desired.bot_enabled, false);
        const components = await control.request(client, access, 'services');
        assert.equal(components.services.find(item => item.name === 'database')?.health, 'healthy');
        assert.equal(components.services.find(item => item.name === 'napcat')?.state, 'running');
        save({ phase: 'complete', verified_at: new Date().toISOString(), localhost_only: true,
          database_preserved: true, installer_state: installerState, desktop_disconnected_after_dispatch: true });
      }
      console.log(JSON.stringify({ kind, status: result.status, service_state: result.service_state, instance_id: state.instance_id }));
    }
  } finally { client.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
