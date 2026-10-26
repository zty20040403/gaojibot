'use strict';
// Read-only acceptance of this project's disposable localhost guest.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('../scripts/local-vm.cjs');
const ssh = require('../lib/ssh.cjs');
const control = require('../lib/control.cjs');
const deploy = require('../lib/deploy.cjs');
const { shellLiteral } = require('../lib/contracts.cjs');

async function main() {
  const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
  const client = await vm.connection();
  try {
    const status = await deploy.status(client, deployment.job);
    assert.equal(status.status, 'awaiting_setup');
    assert.deepEqual(status.completed, ['prepare', 'engine', 'runtime', 'database', 'service', 'verify']);
    const access = await control.accessFor(client, deployment.job);
    assert.equal(access.instance_id, status.instance_id);
    const settings = await control.request(client, access, 'status');
    assert.equal(settings.instance.id, access.instance_id);
    assert.equal(settings.desired.bot_enabled, false);
    assert.deepEqual(settings.instance.settings.models, []);
    assert.equal(settings.instance.settings.bot_qq, '');
    const { services } = await control.request(client, access, 'services');
    for (const name of ['database', 'napcat']) {
      assert.equal(services.find(row => row.name === name)?.state, 'running', name + ' is not running');
    }
    assert.ok([undefined, 'exited'].includes(services.find(row => row.name === 'bot')?.state),
      'An unconfigured bot must not start replying');
    assert.equal(services.find(row => row.name === 'database').health, 'healthy');
    const qq = await control.request(client, access, 'qqStatus');
    assert.equal(qq.online, false);
    assert.equal(qq.qq, '');
    assert.equal(qq.has_error, false);
    assert.match(qq.qrcode, /^https?:\/\//);
    const read = async command => {
      const result = await ssh.execute(client, command);
      assert.equal(result.code, 0, 'Guest read-only check failed');
      return result.stdout.trim();
    };
    const boot = await read('cat /proc/sys/kernel/random/boot_id');
    assert.match(boot, /^[a-f0-9-]{36}$/);
    const compose = '/var/lib/gaoji-app/data/instances/' + access.instance_id + '/compose.json';
    const sql = "SELECT version_num FROM qq_bot.alembic_version; SELECT extversion FROM pg_extension WHERE extname='vector';";
    const database = await read('sudo -n docker compose --file ' + shellLiteral(compose)
      + ' exec -T database psql -U gaoji -d gaoji -Atc ' + shellLiteral(sql));
    const [revision, vector] = database.split('\n');
    assert.equal(revision, '0029_native_ssh_operations');
    assert.equal(vector, '0.8.6');
    for (const unit of ['gaoji-app-' + access.instance_id, 'gaoji-app-control-' + access.instance_id]) {
      assert.equal(await read('systemctl show --property=ActiveState ' + unit + '.service'), 'ActiveState=active');
    }
    const installer = 'gaoji-app-install-' + deployment.job + '.service';
    const installerState = await read('systemctl show --property=ActiveState --value ' + installer);
    assert.ok(['active', 'inactive'].includes(installerState), 'Original installer is failed or still running');
    if (status.checksum !== deployment.checksum) {
      const bootLink = '/usr/local/lib/systemd/system/multi-user.target.wants/' + installer;
      await read('test ! -e ' + shellLiteral(bootLink) + ' && test ! -L ' + shellLiteral(bootLink));
    }
    const result = { verified_at: new Date().toISOString(), localhost_only: true, boot_id: boot,
      job: status.job, instance_id: access.instance_id, checksum: status.checksum,
      status: status.status, installed_at: status.updated_at, database_revision: revision,
      pgvector: vector, services, qq_logged_in: false, qr_available: true, configured_models: 0,
      bot_enabled: false };
    fs.writeFileSync(path.join(vm.directory, 'acceptance.json'), JSON.stringify(result, null, 2) + '\n', { mode: 0o600 });
    console.log(JSON.stringify(result));
  } finally { client.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
