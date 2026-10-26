'use strict';
// Real Electron/preload/React, local IPC fixtures only. No server is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-update-ui-'));
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
      const server = { id: 'f'.repeat(32), name: '本地更新验收', host: 'fixture.invalid', username: 'test', authenticated: true };
      const fixture = globalThis.updateFixture = {
        calls: [], polls: 0, offline: false, task: null, loseAcknowledgement: true,
        plan: { current: { checksum: 'a'.repeat(64), version: '0.20.0', revision: 2, maintenance_pending: false },
          available: { checksum: 'b'.repeat(64), version: '0.20.1' }, canUpdate: true,
          rollbacks: Array.from({ length: 7 }, (_, i) => ({ id: (i + 1).toString(16).padStart(32, '0'), target_checksum: 'c'.repeat(63) + i,
            version: `0.19.${i}`, created_at: 1789420800 - i * 3600 })) },
        status: { instance: { id: 'e'.repeat(32), revision: 2, settings: {
          name: '我的高级', bot_qq: '12345678', admin_qq: '87654321', group_ids: [], allow_all_groups: false, system_prompt: '测试人设',
          models: [{ id: 'mine', label: '我的模型', model: 'fixture', provider: 'openai', protocol: 'openai-chat',
            base_url: 'https://fixture.invalid/v1', api_key_required: false, fallbacks: [], tools: true, vision: true, reasoning_effort: 'auto',
            max_input_tokens: 0, max_output_tokens: 0 }], default_model: 'mine', vision_model: '', features: {},
        } }, desired: { bot_enabled: true }, operation: null },
      };
      const ok = data => ({ ok: true, data });
      const failure = () => ({ ok: false, error: { code: 'connection_lost', message: '本地模拟管理器重启' } });
      const handlers = {
        servers: () => ok([server]),
        inspect: () => ok({ probe: { label: '本地界面测试', freeBytes: 20 * 1024 ** 3, privilege: 'password-required' },
          plan: { eligible: true, steps: [], blockers: [], warnings: [], boundaries: [] } }),
        'install-status': () => ok({ status: 'awaiting_setup', job: '9'.repeat(32), completed: [], stage: 'verify', message: 'fixture' }),
        instance: (_event, { action }) => {
          if (fixture.offline) return failure();
          if (action === 'status') return ok(fixture.status);
          if (action === 'qqStatus') return ok({ online: true, qq: '12345678', nickname: '高级', phase: '', qrcode: '' });
          if (action === 'services') return ok({ services: [] });
          if (action === 'backups') return ok({ backups: [] });
          throw new Error('Unexpected instance action: ' + action);
        },
        'update-plan': () => { fixture.polls++; return fixture.offline ? failure() : ok(fixture.plan); },
        'update-status': () => ok(fixture.task),
        update: (_event, request) => {
          fixture.calls.push(request);
          fixture.task = { id: '1'.repeat(32), job: '9'.repeat(32), target_checksum: 'b'.repeat(64), status: 'queued',
            stage: 'prepare', message: '更新请求已保存', updated_at: 1789420800, finished: false, resumable: true };
          if (fixture.loseAcknowledgement) {
            fixture.loseAcknowledgement = false; fixture.task.status = 'unconfirmed';
            return { ok: false, error: { code: 'update_unconfirmed', message: '本地模拟提交回执丢失' } };
          }
          return ok(fixture.task);
        },
        'update-resume': (_event, request) => { fixture.calls.push({ ...request, kind: 'resume' }); fixture.task.status = 'running'; return ok(fixture.task); },
        discover: () => { throw new Error('No network discovery in UI test'); },
        connect: () => { throw new Error('No server connection in UI test'); },
        install: () => { throw new Error('No server installation in UI test'); },
      };
      for (const [name, handler] of Object.entries(handlers)) {
        ipcMain.removeHandler('gaoji:' + name); ipcMain.handle('gaoji:' + name, handler);
      }
    });
    await page.reload();
    await page.getByRole('navigation', { name: '服务器' }).getByRole('button').click();
    await page.getByRole('tab', { name: '更新与回退', exact: true }).click();
    await page.getByRole('button', { name: '更新此实例', exact: true }).waitFor();
    await page.getByLabel('回退点', { exact: true }).selectOption('2'.padStart(32, '0'));
    const previous = await app.evaluate(() => globalThis.updateFixture.polls);
    await page.getByLabel('回退点', { exact: true }).focus();
    await page.evaluate(() => { window.selectedControl = document.activeElement; });
    await page.waitForTimeout(3300);
    assert.ok(await app.evaluate(() => globalThis.updateFixture.polls) > previous);
    assert.equal(await page.getByLabel('回退点', { exact: true }).inputValue(), '2'.padStart(32, '0'));
    assert.equal(await page.evaluate(() => document.activeElement === window.selectedControl && window.selectedControl.isConnected), true);
    assert.equal(await page.locator('.version-history-row').count(), 5);
    await page.getByRole('button', { name: '全部 7 个回退点', exact: true }).click();
    assert.equal(await page.locator('.version-history-row').count(), 7);
    await page.getByRole('button', { name: '回退', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    assert.equal(await app.evaluate(() => globalThis.updateFixture.calls.length), 0);
    await page.getByLabel('服务器 sudo 密码', { exact: true }).fill('only-ui-fixture');
    assert.equal(await page.getByLabel('服务器 sudo 密码', { exact: true }).getAttribute('type'), 'password');
    await page.getByRole('button', { name: '取消版本操作', exact: true }).click();
    assert.equal(await app.evaluate(() => globalThis.updateFixture.calls.length), 0);
    await page.getByRole('button', { name: '更新此实例', exact: true }).click();
    assert.equal(await page.getByLabel('服务器 sudo 密码', { exact: true }).inputValue(), '');
    await page.getByLabel('服务器 sudo 密码', { exact: true }).fill('only-ui-fixture');
    await page.getByRole('button', { name: '确认更新', exact: true }).click();
    await page.getByRole('heading', { name: '继续原版本任务？', exact: true }).waitFor();
    assert.equal(await page.getByLabel('服务器 sudo 密码', { exact: true }).inputValue(), '');
    await page.getByLabel('服务器 sudo 密码', { exact: true }).fill('resume-after-lost-ack');
    await page.getByRole('button', { name: '继续任务', exact: true }).click();
    await page.getByText('更新请求已保存', { exact: true }).waitFor();
    const submitted = await app.evaluate(() => globalThis.updateFixture.calls);
    assert.equal(submitted.length, 2);
    assert.equal(submitted[1].kind, 'resume');
    assert.equal(submitted[0].confirm, true);
    assert.equal(submitted[0].expectedChecksum, 'a'.repeat(64));
    assert.equal(submitted[0].revision, 2);
    await app.evaluate(() => { globalThis.updateFixture.offline = true; globalThis.updateFixture.task.status = 'running'; });
    await page.getByText('管理器暂时不可读；更新进度仍通过独立任务记录获取。', { exact: true }).waitFor();
    await page.getByText('执行中', { exact: true }).waitFor();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(output, 'updates-running-desktop.png'), fullPage: true });
    await app.evaluate(() => { const fixture = globalThis.updateFixture; fixture.task.status = 'interrupted'; fixture.task.message = '执行器中断，数据保留'; });
    await page.getByRole('button', { name: '继续原版本任务', exact: true }).click();
    await page.getByLabel('服务器 sudo 密码', { exact: true }).fill('resume-ui-fixture');
    await page.getByRole('button', { name: '继续任务', exact: true }).click();
    assert.equal(await app.evaluate(() => globalThis.updateFixture.calls.at(-1).kind), 'resume');
    await app.evaluate(() => { const fixture = globalThis.updateFixture; fixture.offline = false; fixture.task.status = 'completed';
      fixture.task.finished = true; fixture.task.message = '版本与数据库已通过检查'; fixture.plan.current.checksum = fixture.plan.available.checksum;
      fixture.plan.current.version = fixture.plan.available.version; fixture.plan.canUpdate = false; });
    await page.getByRole('button', { name: '已是此 App 版本', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '已是此 App 版本', exact: true }).isDisabled(), true);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: path.join(output, 'updates-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('Update UI passed: stable selection, five-row expansion, explicit confirmation, masked password, independent progress, resume and desktop/mobile. No server contacted.');
  } catch (error) {
    if (app) await (await app.firstWindow()).screenshot({ path: path.join(output, 'updates-failure.png'), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
