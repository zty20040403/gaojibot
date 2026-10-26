'use strict';
// Real Electron/preload with local fixture IPC, no production network or accounts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

async function main() {
  const { _electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-applications-ui-'));
  const output = path.resolve(__dirname, '../release/qa/applications');
  fs.mkdirSync(output, { recursive: true });
  let app;
  try {
    app = await _electron.launch({ executablePath: process.env.GAOJI_PACKAGED_EXECUTABLE || require('electron'),
      args: [...(process.env.GAOJI_PACKAGED_EXECUTABLE ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:/, route => route.abort());
    await app.evaluate(({ ipcMain }) => {
      const ok = data => ({ ok: true, data });
      const fixture = globalThis.applicationFixture = {
        server: { id: 'fixture-server', name: '本地验收服务器', host: 'fixture.invalid', port: 22, username: 'fixture', authenticated: true },
        applications: [{ id: 'fixture-server', serverId: 'fixture-server', appId: 'gaoji', name: '已有高级', legacy: true, deployment: { job: 'a'.repeat(32), version: 'test' } },
          { id: 'app:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', serverId: 'fixture-server', appId: 'static-site', name: '我的作品网站' }],
        website: { site: null, task: null, health: 'stopped' },
        submitted: [], polls: 0, statusPolls: 0,
      };
      const handlers = {
        servers: () => ok([fixture.server]),
        inspect: () => ok({ probe: { os: 'ubuntu', version: '24.04', arch: 'x86_64', label: 'Ubuntu 24.04 LTS', freeBytes: 30 * 1024 ** 3, uid: 1000, privilege: 'password-required', systemd: true, dockerPath: '/usr/bin/docker' },
          plan: { eligible: true, blockers: [], warnings: [], boundaries: [], steps: [{ id: 'prepare', title: '检查安装权限与独立目录' }] } }),
        applications: () => ok(fixture.applications),
        'application-status': (_event, id) => {
          fixture.statusPolls++;
          return ok({ state: id === 'fixture-server' ? 'working' : 'draft', label: id === 'fixture-server' ? '正在安装' : '待部署', message: '只读检查', checkedAt: Date.now() });
        },
        'application-rename': (_event, request) => {
          const item = fixture.applications.find(value => value.id === request.id);
          item.name = request.name; return ok(item);
        },
        'application-remove-draft': (_event, request) => {
          fixture.applications = fixture.applications.filter(item => item.id !== request.id);
          return ok(fixture.applications);
        },
        'application-create': (_event, request) => {
          const record = { id: 'app:cccccccc-cccc-cccc-cccc-cccccccccccc', serverId: fixture.server.id, appId: request.appId, name: request.name };
          fixture.applications.push(record); return ok(record);
        },
        'install-status': () => ok({ job: 'a'.repeat(32), status: 'running', stage: 'prepare', completed: [], created_at: Math.floor(Date.now() / 1000) - 90, updated_at: Math.floor(Date.now() / 1000), message: '正在继续原高级安装任务' }),
        'website-status': () => { fixture.polls++; return ok(fixture.website); },
        'website-select': () => ok({ selection: 'fixture-selection', checksum: 'c'.repeat(64), name: '作品网页', files: 3, bytes: 2000 }),
        'website-backup-export': (_event, request) => { fixture.exported = request; return ok({ exported: true }); },
        'website-backup-import': () => ok({ selection: 'imported-fixture', checksum: 'e'.repeat(64), name: '导入的网站备份', files: 2, bytes: 500 }),
        'website-submit': (_event, request) => {
          fixture.submitted.push(request);
          fixture.website.task = { job: 'd'.repeat(32), status: 'running', stage: 'service', completed: ['prepare', 'engine', 'runtime'], created_at: Math.floor(Date.now() / 1000), updated_at: Math.floor(Date.now() / 1000), message: '正在验证新网站，原网站保留' };
          return ok(fixture.website.task);
        },
      };
      for (const [name, handler] of Object.entries(handlers)) { ipcMain.removeHandler('gaoji:' + name); ipcMain.handle('gaoji:' + name, handler); }
    });
    await page.reload();
    await page.locator('.server-link').filter({ hasText: '本地验收服务器' }).click();
    await page.getByText('正在安装', { exact: true }).waitFor();
    await page.getByRole('button', { name: '进行中', exact: true }).click();
    assert.equal(await page.locator('.application-row').count(), 1);
    await page.getByRole('button', { name: '全部', exact: true }).click();
    await page.getByRole('button', { name: '重命名 我的作品网站', exact: true }).click();
    await page.getByLabel('软件名称', { exact: true }).fill('作品展示');
    const statusBefore = await app.evaluate(() => globalThis.applicationFixture.statusPolls);
    await page.waitForTimeout(8500);
    assert.ok(await app.evaluate(() => globalThis.applicationFixture.statusPolls) > statusBefore);
    assert.equal(await page.getByLabel('软件名称', { exact: true }).inputValue(), '作品展示');
    await page.getByRole('button', { name: '保存名称', exact: true }).click();
    await page.getByText('作品展示', { exact: true }).waitFor();
    await page.getByLabel('搜索软件', { exact: true }).fill('作品');
    assert.equal(await page.locator('.application-row').count(), 1);
    await page.getByRole('button', { name: '移除未部署记录 作品展示', exact: true }).click();
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByLabel('搜索软件', { exact: true }).fill('');
    await page.locator('.application-row').filter({ hasText: '已有高级' }).locator('.application-main').click();
    await page.getByText('正在继续原高级安装任务').waitFor();
    await page.getByRole('button', { name: '已部署软件', exact: true }).click();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(output, 'applications-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: path.join(output, 'applications-mobile.png'), fullPage: true });
    await page.locator('.application-row').filter({ hasText: '作品展示' }).locator('.application-main').click();
    await page.getByRole('button', { name: '选择网页文件夹', exact: true }).click();
    await page.getByRole('button', { name: '部署网站', exact: true }).click();
    await page.getByLabel('服务器 sudo 密码').fill('fixture-password');
    const before = await app.evaluate(() => globalThis.applicationFixture.polls);
    await page.waitForTimeout(4500);
    assert.ok(await app.evaluate(() => globalThis.applicationFixture.polls) > before);
    assert.equal(await page.getByLabel('服务器 sudo 密码').inputValue(), 'fixture-password');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await page.getByRole('dialog').evaluate(element => element.scrollWidth > element.clientWidth), false);
    await page.screenshot({ path: path.join(output, 'website-confirm-mobile.png'), fullPage: true });
    await page.getByRole('dialog').getByRole('button', { name: '部署网站', exact: true }).click();
    await page.getByText('正在验证新网站，原网站保留').waitFor();
    await page.getByText('任务详情', { exact: true }).click();
    await page.getByText('d'.repeat(32), { exact: true }).waitFor();
    assert.match(await page.locator('.deployment-progress-summary').innerText(), /任务历时：\d+ 分 \d+ 秒/);
    const submitted = await app.evaluate(() => globalThis.applicationFixture.submitted);
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].operation, 'install');
    assert.equal(submitted[0].confirm, true);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(output, 'website-progress-desktop.png'), fullPage: true });
    await app.evaluate(() => {
      const fixture = globalThis.applicationFixture;
      fixture.website = { health: 'ready', task: { ...fixture.website.task, status: 'completed', message: '网站已部署' },
        site: { id: 'b'.repeat(32), revision: 2, status: 'running', current: { checksum: 'c'.repeat(64), port: 12345 }, previous: null,
          data_directory: '/var/lib/gaoji-app/applications/' + 'b'.repeat(32), data_bytes: 500,
          backups: [{ id: 'f'.repeat(32), created_at: 1789800000, bytes: 500, files: 2, sha256: 'e'.repeat(64) }] } };
    });
    await page.getByRole('button', { name: '恢复此备份', exact: true }).click();
    await page.getByRole('dialog').getByText('f'.repeat(32), { exact: true }).waitFor();
    await page.getByLabel('服务器 sudo 密码').fill('fixture-password');
    await page.waitForTimeout(4500);
    assert.equal(await page.getByLabel('服务器 sudo 密码').inputValue(), 'fixture-password');
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.screenshot({ path: path.join(output, 'website-backups-desktop.png'), fullPage: true });
    await page.getByRole('button', { name: '加密导出', exact: true }).click();
    await page.getByLabel('设置独立备份密码（至少 12 个字符）').fill('fixture-backup-password');
    await page.getByLabel('再次输入备份密码').fill('wrong');
    assert.equal(await page.getByRole('button', { name: '选择保存位置并导出' }).isEnabled(), false);
    await page.getByLabel('再次输入备份密码').fill('fixture-backup-password');
    await page.getByLabel('服务器 sudo 密码').fill('fixture-sudo');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.getByRole('dialog').evaluate(element => element.scrollWidth > element.clientWidth), false);
    await page.screenshot({ path: path.join(output, 'website-backup-export-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: '选择保存位置并导出' }).click();
    await page.getByText('加密备份已导出到所选位置', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.applicationFixture.exported.backupId), 'f'.repeat(32));
    await page.getByRole('button', { name: '导入备份', exact: true }).click();
    await page.getByLabel('备份密码', { exact: true }).fill('fixture-backup-password');
    await page.getByRole('button', { name: '选择备份并校验' }).click();
    await page.getByText('备份校验通过，等待部署确认，服务器尚未变更').waitFor();
    await page.getByText('导入的网站备份', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.applicationFixture.submitted.length), 1, 'Import must not deploy without confirmation');
    await page.getByRole('button', { name: '已部署软件', exact: true }).click();
    await page.getByRole('button', { name: '添加软件', exact: true }).click();
    await page.getByRole('radio', { name: /静态网站/ }).check();
    await page.getByLabel('实例名称').fill('另一个独立网站');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'catalog-mobile.png'), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.getByRole('button', { name: '配置实例', exact: true }).click();
    await page.getByRole('heading', { name: '另一个独立网站', exact: true }).waitFor();
    await page.getByRole('button', { name: '已部署软件', exact: true }).click();
    await page.getByRole('button', { name: '移除未部署记录 另一个独立网站', exact: true }).click();
    await page.getByRole('button', { name: '移除记录', exact: true }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.locator('.application-row').count(), 2);
    assert.equal(await app.evaluate(() => globalThis.applicationFixture.submitted.length), 1);
    assert.deepEqual(errors, []);
    console.log('Application UI passed: status filtering, rename survives live polling, search, safe draft removal, legacy entry, bot/site detail, deployment timestamps and original job ID, confirmation survives polling, one dispatch, desktop/mobile overflow. Local fixtures only.');
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
