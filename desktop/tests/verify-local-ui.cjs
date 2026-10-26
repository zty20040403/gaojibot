'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  const { _electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-local-ui-'));
  let app;
  try {
    app = await _electron.launch({ executablePath: process.env.GAOJI_EXECUTABLE || require('electron'),
      args: [...(process.env.GAOJI_EXECUTABLE ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    await page.getByRole('button', { name: '部署到这台电脑', exact: true }).click();
    await page.getByRole('heading', { name: '这台电脑上的高级' }).waitFor();
    await page.getByRole('button', { name: '安装到这台电脑', exact: true }).waitFor();
    const result = await page.evaluate(() => window.gaojiDesktop.localInspect());
    assert.equal(result.ok, true);
    await page.waitForFunction(() => !document.querySelector('.stats')?.textContent.includes('- GiB'));
    // Inspect only: no installation, login, model keys or external navigation.
    for (const [width, height] of [[1180, 800], [780, 650]]) {
      await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setSize(...size), [width, height]);
      await page.waitForTimeout(250);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: path.resolve(__dirname, `../../packaging/build/local-ui-${width}.png`), fullPage: true });
    }
    await page.getByRole('button', { name: '添加服务器', exact: true }).click();
    await page.getByLabel('服务器 IP 或域名').waitFor();
    console.log('Local entry, real preload, layout and remote-mode navigation passed');
  } finally { await app?.close(); fs.rmSync(temporary, { recursive: true, force: true }); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
