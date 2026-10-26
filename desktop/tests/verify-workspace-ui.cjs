'use strict';

// Isolated renderer fixtures: no user connections or remote servers are contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

async function main() {
  const { _electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-workspace-'));
  const version = process.env.GAOJI_TEST_VERSION || require('../package.json').version;
  assert.match(version, /^\d+\.\d+\.\d+$/);
  const output = path.resolve(__dirname, '../release/qa/workspace-' + version);
  fs.mkdirSync(output, { recursive: true });
  let app;
  try {
    const packaged = process.env.GAOJI_PACKAGED_EXECUTABLE;
    app = await _electron.launch({ executablePath: packaged || require('electron'),
      args: [...(packaged ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await app.evaluate(({ ipcMain }) => {
      const servers = [
        { id: 'alpha', name: '开发环境', host: 'dev.example.invalid', updatedAt: 300 },
        { id: 'beta', name: '测试环境', host: 'test.example.invalid', updatedAt: 200 },
        { id: 'gamma', name: '一台名称很长的服务器与测试工作区', host: 'a-very-long-hostname.private.example.invalid', updatedAt: 100 },
      ].map(server => ({ ...server, username: 'fixture', port: 22, authenticated: true, remembered: true, authType: 'agent', fingerprint: 'TEST_ONLY', password: 'NEVER_EXPORT_THIS' }));
      for (const name of ['servers', 'inspect', 'install-status']) ipcMain.removeHandler('gaoji:' + name);
      ipcMain.handle('gaoji:servers', () => ({ ok: true, data: servers }));
      ipcMain.handle('gaoji:inspect', (_event, id) => ({ ok: true, data: {
        probe: { os: 'ubuntu', version: '24.04', label: 'Ubuntu 24.04 LTS', arch: 'x86_64', freeBytes: 125 * 1024 ** 3, uid: 1000, privilege: 'passwordless' },
        plan: { eligible: id !== 'beta', blockers: id === 'beta' ? ['测试阻断'] : [], warnings: [], steps: [], boundaries: [] },
      } }));
      ipcMain.handle('gaoji:install-status', () => ({ ok: true, data: null }));
    });
    await page.reload();
    await page.getByRole('button', { name: '置顶 测试环境', exact: true }).waitFor();
    await page.getByLabel('搜索服务器').fill('test.example');
    assert.equal(await page.locator('.connection-row').count(), 1);
    await page.getByRole('button', { name: '清空搜索' }).click();
    await page.getByRole('button', { name: '置顶 测试环境', exact: true }).click();
    assert.equal(await page.locator('.connection-name strong').first().textContent(), '测试环境');
    await page.reload();
    await page.getByRole('button', { name: '取消置顶 测试环境', exact: true }).waitFor();
    await page.getByRole('tab', { name: '已置顶' }).click();
    assert.equal(await page.locator('.connection-row').count(), 1);
    await page.getByRole('tab', { name: '全部', exact: true }).click();
    for (const name of ['开发环境', '测试环境']) {
      await page.locator('.connection-row').filter({ has: page.locator('strong', { hasText: name }) }).locator('.connection-main').click();
      await page.getByRole('heading', { name, exact: true }).waitFor();
      await page.getByRole('button', { name: '工作台', exact: true }).click();
    }
    await page.getByRole('tab', { name: '需处理' }).click();
    assert.equal(await page.locator('.connection-row').count(), 1);
    assert.equal(await page.locator('.connection-name strong').textContent(), '测试环境');
    await page.getByRole('tab', { name: '全部', exact: true }).click();
    await app.evaluate(({ dialog }, filename) => { dialog.showSaveDialog = async () => ({ canceled: false, filePath: filename }); }, path.join(output, 'diagnostics.json'));
    await page.getByRole('button', { name: '导出诊断报告' }).click();
    await page.getByText('诊断报告已保存，未包含地址、密码或密钥。').waitFor();
    const report = fs.readFileSync(path.join(output, 'diagnostics.json'), 'utf8');
    assert.doesNotMatch(report, /example|NEVER_EXPORT_THIS|fingerprint|fixture|TEST_ONLY/);
    assert.deepEqual(JSON.parse(report).servers.map(server => server.status), ['eligible', 'blocked', 'not_checked']);
    await page.setViewportSize({ width: 1280, height: 860 });
    await page.getByRole('button', { name: '外观设置' }).click();
    await page.getByRole('radio', { name: '浅色', exact: true }).click();
    await page.keyboard.press('Escape');
    await page.screenshot({ path: path.join(output, 'light.png'), fullPage: true });
    await page.getByRole('button', { name: '外观设置' }).click();
    await page.getByRole('radio', { name: '深色', exact: true }).click();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.screenshot({ path: path.join(output, 'dark.png'), fullPage: true });
    const paletteColors = { light: [], dark: [] };
    const themeShapes = [];
    assert.equal(await page.evaluate(async () => {
      for (const palette of ['forest', 'ocean', 'blossom', 'graphite']) {
        for (const asset of ['edge', 'watermark']) {
          const image = new Image();
          image.src = `./themes/${palette}-${asset}.svg`;
          await image.decode();
          if (!image.naturalWidth || !image.naturalHeight) return false;
        }
      }
      return true;
    }), true, 'All eight theme drawings must load in the packaged protocol');
    for (const [id, label] of [['forest', '森林'], ['ocean', '海盐'], ['graphite', '石墨'], ['blossom', '花信']]) {
      await page.getByRole('button', { name: '外观设置' }).click();
      await page.getByRole('radio', { name: label, exact: true }).click();
      for (const [mode, modeLabel] of [['light', '浅色'], ['dark', '深色']]) {
        await page.getByRole('radio', { name: modeLabel, exact: true }).click();
        assert.equal(await page.locator('html').getAttribute('data-palette'), id);
        assert.equal(await page.locator('html').getAttribute('data-theme'), mode);
        assert.equal(await page.getByRole('radio', { name: label, exact: true }).getAttribute('aria-checked'), 'true');
        await page.screenshot({ path: path.join(output, `chooser-${id}-${mode}.png`) });
        await page.keyboard.press('Escape');
        const colors = await page.evaluate(() => ['.sidebar', '.workspace-heading .primary', '.workspace-search input', '.appearance-trigger'].map(selector => getComputedStyle(document.querySelector(selector)).backgroundColor));
        paletteColors[mode].push(colors);
        const drawing = await page.evaluate(() => {
          const button = document.querySelector('.workspace-heading .primary');
          const edge = getComputedStyle(button, '::after');
          const watermark = getComputedStyle(document.querySelector('main'), '::before');
          const shape = getComputedStyle(button);
          return { edge: edge.backgroundImage, edgePointer: edge.pointerEvents, watermark: watermark.maskImage,
            watermarkPointer: watermark.pointerEvents, opacity: Number(watermark.opacity), radius: shape.borderRadius, border: shape.borderStyle, shadow: shape.boxShadow };
        });
        assert.ok(drawing.edge.includes(`${id}-edge.svg`));
        assert.ok(drawing.watermark.includes(`${id}-watermark.svg`));
        assert.equal(drawing.edgePointer, 'none');
        assert.equal(drawing.watermarkPointer, 'none');
        assert.ok(drawing.opacity > 0 && drawing.opacity <= .1);
        if (mode === 'light') themeShapes.push([drawing.radius, drawing.border, drawing.shadow].join('|'));
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        await page.screenshot({ path: path.join(output, `${id}-${mode}.png`), fullPage: true });
        await page.getByRole('button', { name: '外观设置' }).click();
      }
      await page.keyboard.press('Escape');
    }
    for (const mode of ['light', 'dark']) {
      for (let element = 0; element < 4; element++) assert.equal(new Set(paletteColors[mode].map(colors => colors[element])).size, 4, `${mode} theme element ${element} must change with palette`);
    }
    assert.equal(new Set(themeShapes).size, 4, 'Each theme needs a different control design, not just colors');
    await page.reload();
    await page.getByRole('button', { name: '取消置顶 测试环境', exact: true }).waitFor();
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    assert.equal(await page.locator('html').getAttribute('data-palette'), 'blossom');
    await page.getByRole('button', { name: '外观设置' }).click();
    await page.getByRole('radio', { name: '跟随系统', exact: true }).click();
    await page.keyboard.press('Escape');
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme });
      await page.waitForFunction(mode => document.documentElement.dataset.theme === mode, colorScheme);
    }
    await page.reload();
    await page.getByRole('button', { name: '外观设置' }).click();
    assert.equal(await page.getByRole('radio', { name: '跟随系统', exact: true }).getAttribute('aria-checked'), 'true');
    await page.getByRole('radio', { name: '深色', exact: true }).click();
    await page.keyboard.press('Escape');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    await page.getByRole('button', { name: '外观设置' }).click();
    assert.equal(await page.locator('.appearance-dialog').evaluate(element => element.scrollWidth > element.clientWidth), false);
    await page.screenshot({ path: path.join(output, 'chooser-mobile.png'), fullPage: true });
    await page.getByRole('button', { name: '完成', exact: true }).click();
    assert.equal(await page.locator('.appearance-dialog').evaluate(element => element.open), false);
    await page.getByRole('button', { name: '添加服务器', exact: true }).last().click();
    await page.getByLabel('服务器 IP 或域名').fill('a-long-hostname.example.invalid');
    await page.getByLabel('服务器密码', { exact: true }).fill('test-password');
    assert.equal(await page.getByLabel('服务器密码', { exact: true }).getAttribute('type'), 'password');
    await page.getByLabel('服务器密码', { exact: true }).fill('');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: path.join(output, 'connect-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('Workspace passed: search, pin persistence, filters, allowlisted diagnostic download, four palettes with both light/dark modes, system appearance changes and persistence, desktop/mobile layout, password masking. Only synthetic local fixtures.');
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
