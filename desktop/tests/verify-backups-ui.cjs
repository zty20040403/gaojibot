'use strict';
// Real Electron/preload/React with local IPC fixtures. No SSH or QQ connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-backups-ui-'));
  const output = path.resolve(__dirname, '../release/qa');
  fs.mkdirSync(output, { recursive: true });
  let app;
  try {
    const packaged = process.env.GAOJI_PACKAGED_EXECUTABLE;
    app = await electron.launch({ executablePath: packaged || require('electron'),
      args: [...(packaged ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const cryptography = await app.evaluate(async ({ app }, directory) => {
      const require = process.getBuiltinModule('module').createRequire(app.getAppPath() + '/package.json');
      const crypto = require(app.getAppPath() + '/lib/backup-crypto.cjs');
      const { createHash } = require('node:crypto');
      const { Readable } = require('node:stream');
      const fs = require('node:fs/promises');
      const bytes = Buffer.from('private-native-electron-fixture'.repeat(200));
      const info = { schema: 1, id: 'a'.repeat(32), source_instance_id: 'b'.repeat(32), runtime_checksum: 'c'.repeat(64),
        bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), database_bytes: 4096, created_at: 100, revision: 2, files: 3 };
      const filename = require('node:path').join(directory, 'electron-crypto.gaojibak');
      await crypto.encrypt(Readable.from([bytes]), info, 'native-electron-test-password', filename);
      const archive = await crypto.openEncrypted(filename, 'native-electron-test-password');
      try {
        const chunks = []; for await (const chunk of archive.stream()) chunks.push(chunk);
        return { matches: Buffer.concat(chunks).equals(bytes), containsPlaintext: (await fs.readFile(filename)).includes(bytes) };
      } finally { await archive.close(); }
    }, temporary);
    assert.deepEqual(cryptography, { matches: true, containsPlaintext: false });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:/, route => route.abort());
    await app.evaluate(({ ipcMain }) => {
      const fixture = globalThis.backupFixture = { calls: [], polls: 0, transfer: null, wrongPassword: true,
        backups: Array.from({ length: 6 }, (_, i) => ({ id: (i + 1).toString(16).padStart(32, '0'), created_at: 1789420800 - i * 3600,
          bytes: 1024 ** 3, files: 8000, revision: 2, sha256: 'a'.repeat(64), imported: i === 0, source_instance_id: 'd'.repeat(32) })),
        status: { instance: { id: 'e'.repeat(32), revision: 2, settings: { name: '本地备份测试', bot_qq: '12345678', admin_qq: '87654321',
          group_ids: [], allow_all_groups: false, system_prompt: '测试人设', models: [{ id: 'mine', label: '我的模型', model: 'fixture',
            provider: 'openai', protocol: 'openai-chat', base_url: 'https://fixture.invalid/v1', api_key_required: false, fallbacks: [],
            tools: true, vision: true, reasoning_effort: 'auto', max_input_tokens: 0, max_output_tokens: 0 }],
          default_model: 'mine', vision_model: '', features: {} } }, desired: { bot_enabled: false }, operation: null },
      };
      const ok = data => ({ ok: true, data });
      const handlers = {
        servers: () => ok([{ id: 'f'.repeat(32), name: '本地备份验收', host: 'fixture.invalid', username: 'test', authenticated: true }]),
        inspect: () => ok({ probe: { label: '本机测试', freeBytes: 20 * 1024 ** 3, privilege: 'root' }, plan: { eligible: true, steps: [], blockers: [], warnings: [], boundaries: [] } }),
        'install-status': () => ok({ status: 'awaiting_setup', job: '9'.repeat(32), completed: [], stage: 'verify', message: 'fixture' }),
        instance: (_event, request) => {
          if (request.action === 'status') return ok(fixture.status);
          if (request.action === 'qqStatus') return ok({ online: true, qq: '12345678', nickname: '高级', phase: '', qrcode: '' });
          if (request.action === 'services') return ok({ services: [] });
          if (request.action === 'backups') { fixture.polls++; return ok({ backups: fixture.backups, transfer: { schema: 1 } }); }
          if (request.action === 'operation') { fixture.calls.push(request); return ok({ id: request.payload.id, kind: request.payload.kind, status: 'queued' }); }
          throw new Error('Unexpected instance action: ' + request.action);
        },
        'backup-select': () => ok({ selection: 'test-selection', name: '跨服务器加密备份'.repeat(14) + '.gaojibak', bytes: 1024 ** 3 }),
        'backup-transfer-status': () => ok(fixture.transfer),
        'backup-export': (_event, request) => {
          fixture.calls.push({ action: 'export', request });
          fixture.transfer = { status: 'running', phase: 'downloading', bytes: 12345, total: 1024 ** 3, name: '本地测试备份.gaojibak', message: '正在下载并加密' };
          return new Promise(resolve => { fixture.finishExport = () => { fixture.transfer.status = 'completed';
            fixture.transfer.message = '加密备份已保存到电脑'; resolve(ok({ exported: true })); }; });
        },
        'backup-import': (_event, request) => {
          fixture.calls.push({ action: 'import', request });
          if (fixture.wrongPassword) { fixture.wrongPassword = false; return { ok: false, error: { code: 'backup_password_or_damage', message: '备份密码错误或文件已损坏，未导入' } }; }
          fixture.transfer = { status: 'completed', phase: 'uploading', bytes: 1024 ** 3, total: 1024 ** 3, name: '本地测试备份.gaojibak', message: '备份已导入；尚未覆盖当前实例，请单独选择恢复' };
          return ok({ imported: true });
        },
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
    await page.getByRole('tab', { name: '模型与启用', exact: true }).click();
    await page.locator('.backup-row').first().waitFor();
    assert.equal(await page.locator('.backup-row').count(), 5);
    await page.locator('.backup-row').first().getByTitle('导出加密备份', { exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '导出加密备份', exact: true });
    await dialog.waitFor();
    await dialog.getByLabel('备份密码', { exact: true }).fill('ui-backup-test-password');
    await dialog.getByLabel('再次输入备份密码', { exact: true }).fill('wrong-confirmation');
    assert.equal(await dialog.getByRole('button', { name: '加密并保存', exact: true }).isDisabled(), true);
    await dialog.getByLabel('再次输入备份密码', { exact: true }).fill('ui-backup-test-password');
    assert.equal(await dialog.getByLabel('备份密码', { exact: true }).getAttribute('type'), 'password');
    await dialog.getByLabel('备份密码', { exact: true }).focus();
    await page.evaluate(() => { window.backupInput = document.activeElement; });
    const polls = await app.evaluate(() => globalThis.backupFixture.polls);
    await page.waitForTimeout(5500);
    assert.ok(await app.evaluate(() => globalThis.backupFixture.polls) > polls);
    assert.equal(await page.evaluate(() => document.activeElement === window.backupInput && window.backupInput.isConnected), true);
    assert.equal(await dialog.getByLabel('备份密码', { exact: true }).inputValue(), 'ui-backup-test-password');
    await dialog.getByRole('button', { name: '加密并保存', exact: true }).click();
    await dialog.getByText('正在下载并加密', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '创建备份', exact: true }).isDisabled(), true);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(output, 'backups-export-desktop.png') });
    await app.evaluate(() => globalThis.backupFixture.finishExport());
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '导入加密备份', exact: true }).click();
    const importing = page.getByRole('dialog', { name: '导入加密备份', exact: true });
    await importing.waitFor();
    assert.equal(await importing.getByLabel('备份密码', { exact: true }).inputValue(), '');
    await importing.getByLabel('备份密码', { exact: true }).fill('wrong-ui-password');
    await importing.getByRole('button', { name: '校验并导入', exact: true }).click();
    await importing.getByRole('alert').filter({ hasText: '备份密码错误' }).waitFor();
    assert.equal(await importing.getByLabel('备份密码', { exact: true }).inputValue(), '');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await importing.evaluate(element => element.scrollWidth > element.clientWidth), false);
    await page.screenshot({ path: path.join(output, 'backups-import-mobile.png') });
    await importing.getByLabel('备份密码', { exact: true }).fill('correct-ui-password');
    await importing.getByRole('button', { name: '校验并导入', exact: true }).click();
    await importing.waitFor({ state: 'hidden' });
    assert.equal(await app.evaluate(() => globalThis.backupFixture.calls.some(call => call.action === 'operation')), false);
    await page.locator('.backup-row').first().getByTitle('恢复此备份', { exact: true }).click();
    await page.getByText(/请先停止原服务器的机器人和 QQ/).waitFor();
    await page.getByRole('button', { name: '取消维护操作', exact: true }).click();
    await page.locator('.backup-section').scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: path.join(output, 'backups-list-mobile.png') });
    assert.deepEqual(errors, []);
    console.log('Backup UI passed: masked password, confirmation, stable polling, import retry, no implicit restore, cross-instance warning and responsive layout. No server contacted.');
  } catch (error) {
    if (app) await (await app.firstWindow()).screenshot({ path: path.join(output, 'backups-failure.png') }).catch(() => {});
    throw error;
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
