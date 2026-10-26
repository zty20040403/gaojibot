'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-update-ui-'));
  let app;
  try {
    app = await electron.launch({ executablePath: process.env.GAOJI_PACKAGED_EXECUTABLE || require('electron'),
      args: [...(process.env.GAOJI_PACKAGED_EXECUTABLE ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + directory] });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    await page.route(/^https?:/, route => route.abort());
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await app.evaluate(({ ipcMain }) => {
      const state = globalThis.updateFixture = { phase: 'idle', currentVersion: '0.1.23', targetVersion: '', checkedAt: '', notes: '',
        mode: 'installer', bytes: 0, total: 0, percent: 0, message: '尚未检查更新' };
      for (const name of ['status', 'check', 'download', 'install']) {
        ipcMain.removeHandler('gaoji:app-update-' + name);
        ipcMain.handle('gaoji:app-update-' + name, () => {
          if (name === 'check') Object.assign(state, { phase: 'available', targetVersion: '0.1.24', message: '发现新版本', notes: 'fixture release notes' });
          if (name === 'download') Object.assign(state, { phase: 'ready', percent: 100, message: '下载完成，SHA-256 校验通过' });
          if (name === 'install') state.message = '安装包已打开';
          return { ok: true, data: state };
        });
      }
    });
    await page.getByRole('button', { name: '添加服务器', exact: true }).first().click();
    await page.getByLabel('名称', { exact: true }).fill('未保存的表单必须保留');
    await page.getByRole('button', { name: '检查 App 更新', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '高级 App 更新' });
    await dialog.getByText('尚未检查更新', { exact: true }).waitFor();
    await dialog.getByRole('button', { name: '检查更新', exact: true }).click();
    await dialog.getByRole('button', { name: '下载更新', exact: true }).click();
    await dialog.getByRole('button', { name: '打开安装包', exact: true }).waitFor();
    await dialog.getByRole('button', { name: '关闭更新窗口' }).click();
    assert.equal(await page.getByLabel('名称', { exact: true }).inputValue(), '未保存的表单必须保留');
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.send('gaoji:show-app-update'));
    await dialog.waitFor();
    await app.evaluate(() => Object.assign(globalThis.updateFixture, { phase: 'downloading', bytes: 60e6, total: 120e6, percent: 50, message: '正在下载并校验安装包' }));
    await dialog.getByRole('progressbar').waitFor();
    for (const [width, height] of [[1180, 800], [390, 844]]) {
      await page.setViewportSize({ width, height });
      assert.equal(await dialog.evaluate(element => element.scrollWidth > element.clientWidth + 1), false);
      fs.mkdirSync(path.resolve(__dirname, '../release/qa'), { recursive: true });
      await page.screenshot({ path: path.resolve(__dirname, `../release/qa/app-update-${width}.png`) });
    }
    assert.deepEqual(errors, []);
    console.log('App update dialog, download progress, menu, preserved form and desktop/mobile layout passed');
  } finally { await app?.close(); fs.rmSync(directory, { recursive: true, force: true }); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
