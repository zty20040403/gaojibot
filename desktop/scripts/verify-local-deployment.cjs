'use strict';
// One synthetic instance on this Mac's Docker Desktop; never SSH or a real QQ login.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { LocalDeployment } = require('../lib/local.cjs');
const root = path.resolve(__dirname, '../../packaging/build');
const directory = fs.mkdtempSync(path.join(root, 'desktop-local-check-'));
const safeStorage = { isEncryptionAvailable: () => true,
  encryptString: value => Buffer.from(value), decryptString: value => value.toString() };
const local = new LocalDeployment(directory, path.join(root, 'deployment'), safeStorage);

async function main() {
  let record;
  try {
    const inspection = await local.inspect();
    assert.ok(inspection.ready, inspection.blockers.join('; '));
    console.log('Local Docker Desktop ready; creating isolated test instance');
    await local.start();
    record = local.read();
    let last = '';
    const deadline = Date.now() + 3600000;
    while (Date.now() < deadline) {
      const state = await local.status();
      if (last !== state.message) { console.log(state.status + ': ' + state.message); last = state.message; }
      assert.notEqual(state.status, 'failed', state.message);
      if (state.status === 'awaiting_setup') break;
      await new Promise(resolve => setTimeout(resolve, 5000));
    }
    assert.equal((await local.status()).status, 'awaiting_setup');
    const status = await local.request('status');
    const qq = await local.request('qqStatus');
    assert.equal(status.instance.settings.bot_qq, '');
    assert.equal(status.instance.settings.models.length, 0);
    assert.equal(status.desired.bot_enabled, false);
    assert.ok(qq.qrcode || qq.online);
    assert.equal(qq.online, false);
    const services = await local.request('services');
    assert.ok(services.services.some(row => row.name === 'database' && row.health === 'healthy'));
    const name = `gaoji-desktop-${record.job}-controller`;
    await local.checked(['restart', name]);
    await new Promise(resolve => setTimeout(resolve, 4000));
    assert.equal((await local.request('status')).instance.id, status.instance.id);
    assert.equal((await local.status()).status, 'awaiting_setup');
    const receipt = { version: require('../package.json').version, checksum: record.checksum,
      instance: status.instance.id, database: 'healthy', qr: 'available', restart: 'same-instance',
      qqLoggedIn: false, windowsExecuted: false };
    fs.writeFileSync(path.join(root, 'desktop-local-check.json'), JSON.stringify(receipt, null, 2));
    console.log(JSON.stringify(receipt));
  } finally {
    // Only the just-created test's labeled containers/network/volume are removed.
    if (record) {
      const names = (await local.checked(['ps', '-aq', '--filter', `label=gaoji.desktop.job=${record.job}`])).trim().split('\n').filter(Boolean);
      for (const id of names) {
        const row = JSON.parse(await local.checked(['inspect', id]))[0];
        assert.equal(row.Config.Labels['gaoji.desktop.job'], record.job);
        await local.checked(['rm', '-f', id]);
      }
      const network = `gaoji-desktop-${record.job}`, volume = `${network}-data`;
      if (await local.owned('network', network, record)) await local.checked(['network', 'rm', network]);
      if (await local.owned('volume', volume, record)) await local.checked(['volume', 'rm', volume]);
    }
    fs.rmSync(directory, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
