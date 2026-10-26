'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  assert.equal(process.platform, 'win32', 'This acceptance must execute on Windows');
  const { _electron: electron } = require('playwright');
  const executable = process.env.GAOJI_PACKAGED_EXECUTABLE;
  const version = process.env.GAOJI_RELEASE_VERSION;
  assert.ok(executable && fs.existsSync(executable));
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), '高级 Windows 验收 '));
  const output = path.resolve('release/qa');
  fs.mkdirSync(output, { recursive: true });
  let app;
  let credentialId;
  try {
    app = await electron.launch({ executablePath: executable, args: ['--user-data-dir=' + directory], timeout: 60000 });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const info = await page.evaluate(() => window.gaojiDesktop.info());
    assert.equal(info.ok, true);
    assert.equal(info.data.runtimeAvailable, true);
    const local = await page.evaluate(() => window.gaojiDesktop.localInspect());
    assert.equal(local.ok, true, 'Windows local prerequisites could not be inspected');
    assert.equal(local.data.platform, 'win32');
    assert.equal(local.data.recorded, false);
    assert.ok(Number.isFinite(local.data.freeBytes) && local.data.freeBytes > 0);
    assert.ok(Array.isArray(local.data.blockers));
    if (!local.data.ready) assert.ok(local.data.blockers.length > 0, 'Missing prerequisites must explain the blocker');
    credentialId = await app.evaluate(({ app, safeStorage }) => {
      const assert = process.getBuiltinModule('assert').strict;
      const fs = process.getBuiltinModule('fs');
      const path = process.getBuiltinModule('path');
      const base = app.getAppPath();
      const require = process.getBuiltinModule('module').createRequire(path.join(base, 'package.json'));
      const { Connections } = require(path.join(base, 'lib/connections.cjs'));
      const store = new Connections(path.join(app.getPath('userData'), 'connections'), safeStorage);
      assert.equal(store.canRemember(), true, 'Windows credential encryption unavailable');
      const secret = 'local-windows-fixture-only';
      const record = store.save({ name: 'Windows CI 测试连接', host: '127.0.0.1', port: 2222, username: 'fixture', remember: true,
        fingerprint: 'SHA256:' + Buffer.alloc(32, 7).toString('base64').replace(/=+$/, '') }, { type: 'password', password: secret });
      assert.equal(fs.readFileSync(store.filename, 'utf8').includes(secret), false, 'Password persisted as plaintext');
      return record.id;
    });
    await app.close(); app = null;
    app = await electron.launch({ executablePath: executable, args: ['--user-data-dir=' + directory], timeout: 60000 });
    const reopened = await app.firstWindow();
    await reopened.getByText('Windows CI 测试连接', { exact: true }).first().waitFor();
    const persisted = await app.evaluate(({ app, safeStorage }, id) => {
      const path = process.getBuiltinModule('path');
      const base = app.getAppPath();
      const require = process.getBuiltinModule('module').createRequire(path.join(base, 'package.json'));
      const { Connections } = require(path.join(base, 'lib/connections.cjs'));
      const store = new Connections(path.join(app.getPath('userData'), 'connections'), safeStorage);
      return { decrypted: store.credentials(id).password === 'local-windows-fixture-only', version: app.getVersion() };
    }, credentialId);
    assert.equal(persisted.decrypted, true);
    assert.equal(persisted.version, version);
    await reopened.getByRole('button', { name: '检查 App 更新', exact: true }).click();
    const dialog = reopened.getByRole('dialog', { name: '高级 App 更新' });
    await dialog.getByRole('button', { name: '检查更新', exact: true }).click();
    await reopened.waitForFunction(async () => {
      const result = await window.gaojiDesktop.appUpdateStatus();
      return result.ok && ['up_to_date', 'available'].includes(result.data.phase);
    }, undefined, { timeout: 45000 });
    const update = await reopened.evaluate(() => window.gaojiDesktop.appUpdateStatus());
    assert.equal(update.ok, true);
    assert.match(update.data.targetVersion, /^\d+\.\d+\.\d+$/);
    await reopened.screenshot({ path: path.join(output, 'windows-installed-update.png') });
    await app.close(); app = null;
    const portable = process.env.GAOJI_PORTABLE_EXECUTABLE;
    assert.ok(portable && fs.existsSync(portable));
    app = await electron.launch({ executablePath: portable, args: ['--user-data-dir=' + path.join(directory, 'portable')], timeout: 60000 });
    const portablePage = await app.firstWindow();
    await portablePage.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const portableInfo = await portablePage.evaluate(() => window.gaojiDesktop.info());
    assert.equal(portableInfo.ok, true);
    assert.equal(portableInfo.data.runtimeAvailable, true);
    await portablePage.screenshot({ path: path.join(output, 'windows-portable.png') });
    fs.writeFileSync(path.join(output, 'windows-runtime.json'), JSON.stringify({ platform: process.platform, architecture: process.arch,
      windowsRelease: os.release(), version, installedExecutable: executable, portableExecutable: portable,
      unicodePaths: true, credentialEncryption: true, credentialReloadAfterRestart: true, actualUpdateCheck: true,
      publishedVersion: update.data.targetVersion, updatePhase: update.data.phase,
      localPrerequisiteInspection: { checked: true, ready: local.data.ready, blockers: local.data.blockers, freeBytes: local.data.freeBytes },
      portableLaunch: true, h310Accessed: false, qqLoggedIn: false, windowsLocalDockerDeploymentTested: false }, null, 2));
    console.log('Windows installed/portable execution, Unicode paths, DPAPI persistence and actual update lookup passed');
  } finally { await app?.close(); fs.rmSync(directory, { recursive: true, force: true }); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
