'use strict';
// Electron UI fixtures only; no SSH, Docker or user data are accessed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-storage-ui-'));
  const output = path.resolve(__dirname, '../release/qa');
  fs.mkdirSync(output, { recursive: true });
  let app;
  try {
    const packaged = process.env.GAOJI_PACKAGED_EXECUTABLE;
    app = await electron.launch({ executablePath: packaged || require('electron'),
      args: [...(packaged ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:/, route => route.abort());
    await app.evaluate(({ ipcMain }) => {
      const copies = Array.from({ length: 7 }, (_, i) => ({ id: 'backup:' + (i + 1).toString(16).padStart(32, '0'), kind: 'backup',
        created_at: 1789420800 - i * 86400, bytes: (i + 1) * 1024 ** 3, paths: ['backups/' + (i + 1).toString(16).padStart(32, '0')],
        protected: i < 2 ? '最近保留份数' : '', eligible: i >= 2 }));
      const fixture = globalThis.storageFixture = { calls: [], polls: 0, plans: 0,
        storage: { revision: 1, policy: { enabled: false, keep_backups: 5, keep_restore_points: 2, min_age_days: 7 },
          capacity: { state: 'unknown', reason: '容量尚未确认，暂停新的大文件操作' },
          disk: { total_bytes: 100 * 1024 ** 3, free_bytes: 30 * 1024 ** 3 }, job: null,
          scan: { created_at: 1789420800, live: [{ name: 'database', bytes: 8 * 1024 ** 3, complete: true }, { name: 'files', bytes: 2 * 1024 ** 3, complete: true }],
            items: copies, warnings: ['保留一个没有完整完成记录的恢复目录'], candidate_bytes: 25 * 1024 ** 3 } },
        status: { instance: { id: 'e'.repeat(32), revision: 2, settings: { name: '测试实例', bot_qq: '12345678', admin_qq: '87654321',
          group_ids: [], allow_all_groups: false, system_prompt: '测试人设', models: [], default_model: '', vision_model: '', features: {} } },
          desired: { bot_enabled: false }, operation: null },
      };
      const ok = data => ({ ok: true, data });
      const handlers = {
        servers: () => ok([{ id: 'f'.repeat(32), name: '本地存储验收', host: 'fixture.invalid', username: 'test', authenticated: true }]),
        inspect: () => ok({ probe: { label: '本机测试', freeBytes: 30 * 1024 ** 3, privilege: 'root' }, plan: { eligible: true, steps: [], blockers: [], warnings: [], boundaries: [] } }),
        'install-status': () => ok({ status: 'awaiting_setup', job: '9'.repeat(32), completed: [], stage: 'verify', message: 'fixture' }),
        instance: (_event, request) => {
          if (request.action === 'status') return ok(fixture.status);
          if (request.action === 'qqStatus') return ok({ online: true, qq: '12345678', nickname: '高级', phase: '', qrcode: '' });
          if (request.action === 'services') return ok({ services: [] });
          if (request.action === 'backups') return ok({ backups: [] });
          if (request.action === 'storageStatus') { fixture.polls++; return ok(fixture.storage); }
          fixture.calls.push(request);
          if (request.action === 'storageScan') return ok(fixture.storage.scan);
          if (request.action === 'storagePolicy') {
            if (request.payload.revision !== fixture.storage.revision) return { ok: false, error: { code: 'conflict', message: '策略已被修改' } };
            fixture.storage.policy = request.payload.policy; fixture.storage.revision++;
            return ok({ policy: fixture.storage.policy, revision: fixture.storage.revision });
          }
          if (request.action === 'storagePlan') return ok({ id: (++fixture.plans).toString(16).padStart(32, '0'), created_at: 1789420800,
            items: copies.filter(item => item.eligible), estimated_bytes: 25 * 1024 ** 3, warnings: [] });
          if (request.action === 'storageCleanup') {
            if (fixture.stalePlan) { fixture.stalePlan = false; return { ok: false, error: { code: 'stale_plan', message: '副本发生变化，请重新预览' } }; }
            fixture.storage.job = { id: request.payload.id, status: 'queued', message: '清理已排队', updated_at: 1789420800, items_done: 0,
              estimated_bytes: 25 * 1024 ** 3, free_before: 30 * 1024 ** 3, resumable: false };
            return ok(fixture.storage.job);
          }
          throw new Error('Unexpected fixture action: ' + request.action);
        },
        discover: () => { throw new Error('No network discovery'); }, connect: () => { throw new Error('No remote connection'); }, install: () => { throw new Error('No remote installation'); },
      };
      for (const [name, handler] of Object.entries(handlers)) { ipcMain.removeHandler('gaoji:' + name); ipcMain.handle('gaoji:' + name, handler); }
    });
    await page.reload();
    await page.getByRole('navigation', { name: '服务器' }).getByRole('button').click();
    await page.getByRole('tab', { name: '存储管理', exact: true }).click();
    await page.getByText('磁盘可用', { exact: true }).waitFor();
    await page.getByText('容量待确认', { exact: true }).waitFor();
    assert.ok((await page.getByRole('region', { name: '实例容量状态' }).textContent()).includes('未完整统计'));
    await app.evaluate(() => { globalThis.storageFixture.storage.capacity = {
      state: 'blocked', reason: '本实例的数据额度不足；已有文件未删除', complete: true,
      sampled_at: 1789420800, policy: { max_data_gib: 30, reserve_free_gib: 2 }, used_bytes: 35 * 1024 ** 3,
      data_bytes: 25 * 1024 ** 3, sandbox_bytes: 9 * 1024 ** 3, writable_bytes: 1024 ** 3,
      disks: [{ name: 'instance', free_bytes: 30 * 1024 ** 3 }, { name: 'docker', free_bytes: 10 * 1024 ** 3 }],
    }; });
    await page.getByText('新大文件操作已暂停', { exact: true }).waitFor();
    await page.getByText('35.00 GiB / 30 GiB', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '扫描空间', exact: true }).isDisabled(), false);
    await page.getByLabel('至少保留完整备份（份）').fill('3');
    await page.getByLabel('至少保留完整备份（份）').focus();
    await page.evaluate(() => { window.storageInput = document.activeElement; });
    const polls = await app.evaluate(() => globalThis.storageFixture.polls);
    await page.waitForTimeout(5500);
    assert.ok(await app.evaluate(() => globalThis.storageFixture.polls) > polls);
    assert.equal(await page.getByLabel('至少保留完整备份（份）').inputValue(), '3');
    assert.equal(await page.evaluate(() => document.activeElement === window.storageInput && window.storageInput.isConnected), true);
    assert.equal(await app.evaluate(() => globalThis.storageFixture.calls.filter(call => call.action === 'storageScan').length), 0);
    await page.getByRole('tab', { name: 'QQ 与模型', exact: true }).click();
    await page.getByRole('tab', { name: '存储管理', exact: true }).click();
    assert.equal(await page.getByLabel('至少保留完整备份（份）').inputValue(), '3');
    await page.getByLabel('每小时自动清理过期副本').check();
    await page.getByRole('button', { name: '保存保留策略', exact: true }).click();
    await page.getByRole('heading', { name: '应用自动清理策略？' }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.storageFixture.calls.length), 0);
    await page.getByRole('button', { name: '取消清理操作', exact: true }).click();
    assert.equal(await app.evaluate(() => globalThis.storageFixture.calls.length), 0);
    await page.getByRole('button', { name: '保存保留策略', exact: true }).click();
    await page.getByRole('button', { name: '确认应用策略', exact: true }).click();
    await page.getByText('保留策略已保存', { exact: true }).waitFor();
    await page.getByRole('button', { name: '扫描空间', exact: true }).click();
    await page.getByRole('button', { name: '预览可清理内容', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '清理预览', exact: true });
    await dialog.waitFor();
    assert.equal(await dialog.locator('.storage-copy').count(), 5);
    await dialog.locator('summary').first().click();
    await page.setViewportSize({ width: 1280, height: 900 });
    const button = await dialog.getByRole('button', { name: '确认清理所列副本', exact: true }).boundingBox();
    assert.ok(button.y >= 0 && button.y + button.height <= 900);
    await page.screenshot({ path: path.join(output, 'storage-preview-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await dialog.evaluate(element => element.scrollWidth > element.clientWidth), false);
    await page.screenshot({ path: path.join(output, 'storage-preview-mobile.png') });
    await page.setViewportSize({ width: 1280, height: 900 });
    await app.evaluate(() => { globalThis.storageFixture.stalePlan = true; });
    await dialog.getByRole('button', { name: '确认清理所列副本', exact: true }).click();
    await dialog.getByRole('alert').filter({ hasText: '副本发生变化' }).waitFor();
    await page.getByRole('button', { name: '取消清理操作', exact: true }).click();
    await page.getByRole('button', { name: '预览可清理内容', exact: true }).click();
    await page.getByRole('button', { name: '确认清理所列副本', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await page.getByText('清理已排队', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '预览可清理内容', exact: true }).isDisabled(), true);
    const submitted = await app.evaluate(() => globalThis.storageFixture.calls.filter(call => call.action === 'storageCleanup').map(call => call.payload));
    assert.equal(submitted.length, 2); assert.equal(submitted[0].confirm, true); assert.notEqual(submitted[0].id, submitted[1].id);
    await app.evaluate(() => { const value = globalThis.storageFixture.storage;
      value.capacity = { ...value.capacity, state: 'ready', reason: '', used_bytes: 10 * 1024 ** 3, data_bytes: 5 * 1024 ** 3, sandbox_bytes: 4 * 1024 ** 3 };
      value.job = { ...value.job, status: 'completed', message: '过期副本已清理，当前数据未删除', items_done: 5, free_after: 55 * 1024 ** 3 };
      value.disk.free_bytes = 55 * 1024 ** 3; value.scan.items = value.scan.items.filter(item => !item.eligible); value.scan.candidate_bytes = 0;
    });
    await page.getByText('过期副本已清理，当前数据未删除', { exact: true }).waitFor();
    await page.getByText('可接收新任务', { exact: true }).waitFor();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole('region', { name: '实例容量状态' }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'capacity-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('.storage-panel').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'storage-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('Storage UI passed: unknown/blocked/recovered capacity, stable drafts/focus/tabs, cached polling, retention confirmation, stale preview rejection and completed-job reporting. No server contacted.');
  } catch (error) {
    if (app) await (await app.firstWindow()).screenshot({ path: path.join(output, 'storage-failure.png'), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
