'use strict';

// Opt-in local QEMU acceptance. The connector verifies the VM marker and pinned
// host key; no host alias, production credential or user SSH config is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { utils } = require('ssh2');
const vm = require('./local-vm.cjs');
const ssh = require('../lib/ssh.cjs');
const deploy = require('../lib/deploy.cjs');
const { Connections, atomicJson } = require('../lib/connections.cjs');
const { Websites, identifier } = require('../lib/websites.cjs');
const control = require('../lib/control.cjs');

const directory = path.join(vm.directory, 'applications-qa');
const runtime = path.resolve(__dirname, '../../packaging/build/deployment');
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function main() {
  const action = process.argv[2];
  assert.ok(['install', 'inspect', 'wait', 'wait-failure', 'update', 'rollback', 'stop', 'start', 'uninstall', 'purge', 'resume', 'second', 'diagnostics', 'fault-previous', 'repair-previous', 'interrupt', 'coexistence', 'assert-retained', 'assert-purged', 'reboot', 'reboot-after-fix', 'backup-roundtrip'].includes(action));
  const client = await vm.connection();
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const store = new Connections(path.join(directory, 'connections'), { isEncryptionAvailable: () => false });
  const pointer = path.join(directory, 'scenario.json');
  let scenario;
  try {
    if (fs.existsSync(pointer)) scenario = JSON.parse(fs.readFileSync(pointer, 'utf8'));
    else {
      const parsed = utils.parseKey(fs.readFileSync(path.join(vm.directory, 'host-key.pub')));
      if (parsed instanceof Error) throw parsed;
      const fingerprint = 'SHA256:' + createHash('sha256').update(parsed.getPublicSSH()).digest('base64').replace(/=+$/, '');
      const host = store.save({ name: 'Isolated Ubuntu QA', host: '127.0.0.1', port: vm.metadata().port, username: 'qa', fingerprint }, { type: 'agent', agent: 'unused-test-only' });
      const site = store.createApplication(host.id, 'static-site', 'QA primary website');
      scenario = { server: host.id, site: site.id, second: null, operations: [] };
      atomicJson(pointer, scenario);
    }
    const websites = new Websites(path.join(directory, 'cache'), runtime, store, async id => {
      assert.equal(store.connectionId(id), scenario.server);
      return client;
    });
    let target = process.argv[3] === 'second' ? scenario.second : scenario.site;
    if (action === 'backup-roundtrip') {
      assert.ok(!scenario.backup_acceptance, 'Do not repeat an already verified destructive QA sequence');
      const receipts = [];
      async function observe(id, failure = false) {
        let last = '';
        for (const deadline = Date.now() + 1200000; Date.now() < deadline;) {
          const value = await websites.status(id);
          const report = JSON.stringify({ operation: value.site?.operation?.request?.operation, job: value.task?.job, state: value.task?.status, message: value.task?.message });
          if (last !== report) { console.log(report); last = report; }
          if (value.task?.status === 'failed') {
            assert.ok(failure, value.task.message); assert.equal(value.health, 'ready');
            receipts.push(value); return value;
          }
          if (value.task?.status === 'completed') {
            assert.ok(!failure, 'Injected failure did not fail');
            if (value.site.status === 'running') assert.equal(value.health, 'ready');
            receipts.push(value); return value;
          }
          await wait(2000);
        }
        throw Error('Original QA operation still pending; no replacement issued');
      }
      async function submit(id, operation, extra = {}, failure = false) {
        const before = await websites.status(id);
        const result = await websites.submit({ id, operation, revision: before.site?.revision || 0, confirm: true, ...extra });
        scenario.operations.push({ id, action: operation, job: result.job }); atomicJson(pointer, scenario);
        return observe(id, failure);
      }
      if ((await websites.status(target)).site?.status === 'removed') {
        scenario.previous_backup_attempts = [...(scenario.previous_backup_attempts || []), { site: target, second: scenario.second, operations: scenario.operations,
          result: 'Observer interrupted during final purge; status publication permissions corrected before retest' }];
        const retry = store.createApplication(scenario.server, 'static-site', 'QA backup acceptance retry');
        scenario.site = retry.id; target = retry.id; scenario.operations = []; atomicJson(pointer, scenario);
        const selection = await websites.select(path.join(directory, 'content-install'));
        await submit(target, 'install', { selection: selection.selection });
      }
      const original = await websites.status(target);
      assert.equal(original.health, 'ready');
      const backed = await submit(target, 'backup');
      const backup = backed.site.backups[0];
      const exported = path.join(directory, 'website.gaojibak');
      const password = 'isolated-fixture-backup-password';
      await websites.exportBackup(target, backup.id, password, undefined, exported);
      assert.ok(fs.existsSync(exported));
      const other = store.createApplication(scenario.server, 'static-site', 'Restored independent website');
      scenario.second = other.id; atomicJson(pointer, scenario);
      await assert.rejects(websites.importBackup(exported, 'incorrect-fixture-password'));
      const imported = await websites.importBackup(exported, password);
      const restored = await submit(other.id, 'install', { selection: imported.selection });
      assert.equal(restored.site.current.index_sha256, original.site.current.index_sha256);
      const updated = path.join(directory, 'updated-for-backup');
      fs.mkdirSync(updated, { recursive: true });
      fs.writeFileSync(path.join(updated, 'index.html'), '<h1>Updated after backup</h1>');
      const selected = await websites.select(updated);
      const changed = await submit(target, 'update', { selection: selected.selection });
      assert.notEqual(changed.site.current.index_sha256, original.site.current.index_sha256);
      const archive = `/var/lib/gaoji-app/applications/${identifier(store.instance(target))}/backups/${deploy.jobId(backup.id)}.tar.gz`;
      const injected = await ssh.execute(client, `sudo -n cp ${archive} ${archive}.qa-copy\nprintf damaged | sudo -n tee ${archive} >/dev/null`);
      assert.equal(injected.code, 0);
      const failed = await submit(target, 'restore', { backupId: backup.id }, true);
      assert.deepEqual(failed.site.current, changed.site.current);
      const repaired = await ssh.execute(client, `sudo -n mv ${archive}.qa-copy ${archive}`);
      assert.equal(repaired.code, 0);
      const failedJob = failed.task.job;
      await websites.resume({ id: target });
      const recovered = await observe(target);
      assert.equal(recovered.task.job, failedJob);
      assert.equal(recovered.site.current.index_sha256, original.site.current.index_sha256);
      const retained = await submit(target, 'uninstall');
      assert.equal(retained.site.status, 'retained');
      assert.equal(retained.site.backups[0].sha256, backup.sha256);
      await websites.exportBackup(target, backup.id, password, undefined, path.join(directory, 'retained.gaojibak'));
      const removed = await submit(target, 'purge', { eraseInstanceId: identifier(store.instance(target)) });
      assert.equal(removed.site.status, 'removed'); assert.deepEqual(removed.site.backups, []);
      const exists = await ssh.execute(client, `sudo -n test ! -e ${archive}`); assert.equal(exists.code, 0);
      const independent = await websites.status(other.id);
      assert.equal(independent.health, 'ready');
      assert.equal(independent.site.current.index_sha256, original.site.current.index_sha256);
      scenario.backup_acceptance = { recorded_at: Date.now(), receipts, encrypted_export: true, wrong_password_rejected: true,
        new_instance_restored: true, failed_restore_preserved_current: true, same_job_recovered: true,
        retained_uninstall_exported: true, purge_isolated: true, production_hosts_contacted: false };
      atomicJson(pointer, scenario);
      console.log('Website real backup acceptance passed: encrypted export/import, independent recovery, corrupt archive failure preserving current, original-job retry, retained uninstall and isolated purge');
      return;
    }
    if (action === 'reboot' || action === 'reboot-after-fix') {
      const snapshot = async connection => {
        const read = async command => {
          const result = await ssh.execute(connection, command);
          assert.equal(result.code, 0); return result.stdout.trim();
        };
        const observer = new Websites(path.join(directory, 'cache'), runtime, store, async () => connection);
        const sites = [];
        for (const id of [scenario.site, scenario.second]) {
          const value = await observer.status(id);
          assert.equal(value.health, 'ready');
          assert.equal(value.task.status, 'completed');
          const current = value.site.current;
          assert.match(current.container, /^gaoji-site-[a-f0-9]{32}-[a-f0-9]{32}$/);
          const binding = JSON.parse(await read(`sudo -n docker inspect ${current.container} --format '{{json .HostConfig.PortBindings}}'`));
          assert.deepEqual(binding['8080/tcp'], [{ HostIp: '127.0.0.1', HostPort: String(current.port) }]);
          sites.push({ id, revision: value.site.revision, checksum: current.checksum, container: current.container, port: current.port });
        }
        const bot = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
        assert.equal((await deploy.status(connection, bot.job)).status, 'awaiting_setup');
        const access = await control.accessFor(connection, bot.job);
        const instance = deploy.jobId(access.instance_id);
        const settings = await control.request(connection, access, 'status');
        assert.equal(settings.desired.bot_enabled, false);
        const { services } = await control.request(connection, access, 'services');
        assert.equal(services.find(item => item.name === 'database')?.health, 'healthy');
        const sql = 'SELECT system_identifier FROM pg_control_system(); SELECT version_num FROM qq_bot.alembic_version;';
        const database = await read(`sudo -n docker compose --file /var/lib/gaoji-app/data/instances/${instance}/compose.json exec -T database psql -U gaoji -d gaoji -Atc '${sql}'`);
        return { boot: await read('cat /proc/sys/kernel/random/boot_id'), sites, instance, revision: settings.instance.revision, database };
      };
      if (action === 'reboot-after-fix') {
        assert.ok(scenario.reboot && !scenario.reboot.after, 'Only a failed prior reboot may be retested');
        const current = await snapshot(client);
        assert.notEqual(current.boot, scenario.reboot.before.boot, 'The earlier reboot must already have happened');
        scenario.previous_reboots = [...(scenario.previous_reboots || []), { ...scenario.reboot, result: 'failed: ephemeral Docker port changed; fixed with explicit binding' }];
        delete scenario.reboot;
        atomicJson(pointer, scenario);
      }
      if (!scenario.reboot) {
        scenario.reboot = { before: await snapshot(client), requested_at: Date.now() };
        atomicJson(pointer, scenario);
        try { await ssh.execute(client, 'sudo -n systemctl reboot', { timeoutMs: 15000 }); }
        catch { /* Lost response is not permission for a second reboot. */ }
        client.end();
      }
      let lastError;
      const deadline = Date.now() + 600000;
      do {
        let connection;
        try {
          connection = await vm.connection();
          const after = await snapshot(connection);
          assert.notEqual(after.boot, scenario.reboot.before.boot);
          for (const field of ['sites', 'instance', 'revision', 'database']) assert.deepEqual(after[field], scenario.reboot.before[field]);
          scenario.reboot.after = after; atomicJson(pointer, scenario);
          console.log('Real guest reboot passed: both websites recovered, original database identity/revision preserved, bot still disabled'); return;
        } catch (error) { lastError = error.message; }
        finally { connection?.end(); }
        await wait(5000);
      } while (Date.now() < deadline);
      throw Error('Requested reboot not verified; do not reissue: ' + lastError);
    }
    if (action === 'fault-previous' || action === 'repair-previous') {
      const value = await websites.status(target);
      const checksum = value.site.previous?.checksum;
      assert.match(checksum, /^[a-f0-9]{64}$/);
      assert.notEqual(checksum, value.site.current.checksum);
      const filename = `/var/lib/gaoji-app/applications/${identifier(store.instance(target))}/releases/${checksum}/html/index.html`;
      const result = await ssh.execute(client, `sudo -n chmod ${action === 'fault-previous' ? '000' : '644'} ${filename}`);
      assert.equal(result.code, 0);
      assert.equal((await websites.status(target)).health, 'ready', 'Current website must remain available');
      console.log(action + ': previous fixture only; current HTTP is healthy'); return;
    }
    if (action === 'interrupt') {
      const deadline = Date.now() + 600000;
      while (Date.now() < deadline) {
        const value = await websites.status(target);
        if (value.task?.stage === 'verify' && value.task.status === 'running') {
          const job = deploy.jobId(value.task.job);
          const result = await ssh.execute(client, `sudo -n systemctl stop gaoji-app-install-${job}.service`);
          assert.equal(result.code, 0);
          const after = await websites.status(target);
          assert.equal(after.task.status, 'failed');
          assert.equal(after.health, 'ready', 'Original site must survive worker interruption');
          console.log('Interrupted candidate worker; original site remains healthy; same job is resumable'); return;
        }
        assert.notEqual(value.task?.status, 'failed');
        await wait(1000);
      }
      throw Error('Candidate verification was not observed; no worker was stopped');
    }
    if (action === 'coexistence') {
      for (const id of [scenario.site, scenario.second]) assert.equal((await websites.status(id)).health, 'ready');
      const bot = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
      store.setDeployment(scenario.server, bot);
      assert.equal(store.applications(scenario.server).length, 3);
      assert.equal(store.applications(scenario.server)[0].legacy, true);
      const status = await deploy.status(client, bot.job);
      assert.equal(status.status, 'awaiting_setup');
      const access = await control.accessFor(client, bot.job);
      const { services } = await control.request(client, access, 'services');
      assert.equal(services.find(item => item.name === 'database')?.health, 'healthy');
      assert.equal(services.find(item => item.name === 'napcat')?.state, 'running');
      const settings = await control.request(client, access, 'status');
      assert.equal(settings.desired.bot_enabled, false);
      console.log('Actual coexistence passed: two HTTP-verified sites plus legacy bot entry, healthy database and NapCat, bot not activated'); return;
    }
    if (action === 'assert-retained' || action === 'assert-purged') {
      const value = await websites.status(target);
      assert.equal(value.site.status, action === 'assert-retained' ? 'retained' : 'removed');
      const root = `/var/lib/gaoji-app/applications/${identifier(store.instance(target))}/releases`;
      const result = await ssh.execute(client, `sudo -n test ${action === 'assert-retained' ? '-d' : '! -e'} ${root}`);
      assert.equal(result.code, 0);
      assert.equal((await websites.status(scenario.second)).health, 'ready');
      const bot = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
      const access = await control.accessFor(client, bot.job);
      const { services } = await control.request(client, access, 'services');
      assert.equal(services.find(item => item.name === 'database')?.health, 'healthy');
      console.log(action + ': exact resource/data boundary passed; other website and bot database remain healthy'); return;
    }
    if (action === 'diagnostics') {
      const instance = store.instance(target);
      const job = deploy.jobId(instance.deployment.job);
      const result = await ssh.execute(client, `systemctl show gaoji-app-install-${job}.service --property=ActiveState,SubState,Result,ExecMainStatus\nsudo -n tail -n 50 /var/lib/gaoji-app/jobs/${job}.log`, { maxBytes: 16000 });
      console.log(result.stdout); assert.equal(result.code, 0); return;
    }
    if (action === 'inspect' || action === 'wait' || action === 'wait-failure') {
      let previous = '';
      const deadline = Date.now() + (action !== 'inspect' ? 4200000 : 1);
      do {
        const value = await websites.status(target);
        const summary = JSON.stringify({ instance: value.site?.id, revision: value.site?.revision, status: value.site?.status,
          health: value.health, task: value.task?.job, stage: value.task?.stage, state: value.task?.status, message: value.task?.message });
        if (summary !== previous) { console.log(summary); previous = summary; }
        if (action === 'inspect') return;
        if (action === 'wait-failure' && value.task?.status === 'failed') {
          assert.equal(value.health, 'ready', 'Previous active site must survive the failed candidate');
          console.log('Candidate failed as injected; original website still passes HTTP checksum'); return;
        }
        assert.notEqual(value.task?.status, 'failed', value.task?.message);
        if (value.task?.status === 'completed') {
          if (value.site.status === 'running') assert.equal(value.health, 'ready');
          scenario.receipts = [...(scenario.receipts || []), { ...value, recorded_at: Date.now() }];
          atomicJson(pointer, scenario);
          return;
        }
        await wait(5000);
      } while (Date.now() < deadline);
      throw Error('Observation deadline reached; inspect the same task, do not launch a replacement');
    }
    if (action === 'resume') { console.log(JSON.stringify(await websites.resume({ id: target }))); return; }
    let id = target;
    if (action === 'second') {
      assert.equal(scenario.second, null, 'A second instance is already recorded');
      const other = store.createApplication(scenario.server, 'static-site', 'QA unrelated website');
      scenario.second = other.id; id = other.id; atomicJson(pointer, scenario);
    }
    const value = await websites.status(id);
    let selection;
    if (['install', 'update', 'second'].includes(action)) {
      const folder = path.join(directory, 'content-' + action);
      fs.mkdirSync(folder, { recursive: true });
      fs.writeFileSync(path.join(folder, 'index.html'), `<!doctype html><html lang="zh"><meta charset="utf-8"><title>Local acceptance</title><h1>Gaoji ${action}</h1><p>Independent local website fixture.</p></html>`);
      selection = (await websites.select(folder)).selection;
    }
    const operation = action === 'second' ? 'install' : action;
    const result = await websites.submit({ id, operation, revision: value.site?.revision || 0, selection, confirm: true,
      ...(operation === 'purge' ? { eraseInstanceId: identifier(store.instance(id)) } : {}) });
    scenario.operations.push({ action: operation, id, job: result.job, submitted_at: Date.now() });
    atomicJson(pointer, scenario);
    console.log(JSON.stringify(result));
    // Close SSH immediately: the next invocation must recover the original task
    // from disk and observe the independent systemd worker.
  } finally { client.end(); }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
