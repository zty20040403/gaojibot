'use strict';
// Opt-in real packaged UI against the fixed localhost VM, never a user's host.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('../scripts/local-vm.cjs');
const { Connections } = require('../lib/connections.cjs');

async function main() {
  const metadata = vm.metadata();
  const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
  const client = await vm.connection(); client.end();
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-local-vm-ui-'));
  const executable = process.env.GAOJI_PACKAGED_EXECUTABLE;
  let app;
  async function launch() {
    app = await electron.launch({ executablePath: executable || require('electron'),
      args: [...(executable ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    page.setDefaultTimeout(60000);
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    // Replace only the native chooser with the generated QA key. SSH, IPC,
    // identity checks, inspection, status and rendering remain real.
    await app.evaluate(({ dialog }, keyFile) => {
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [keyFile] });
    }, path.join(vm.directory, 'client-key'));
    return page;
  }
  async function selectKey(page) {
    await page.getByLabel('登录方式').selectOption('private-key');
    await page.locator('button.key-picker').click();
    await page.locator('button.key-picker').getByText('client-key', { exact: true }).waitFor();
    await page.getByRole('button', { name: '连接并检测', exact: true }).click();
  }
  try {
    let page = await launch();
    await page.getByRole('button', { name: '连接第一台服务器' }).click();
    await page.getByLabel('名称', { exact: true }).fill('本机隔离验收');
    await page.getByLabel('服务器 IP 或域名').fill('127.0.0.1');
    await page.getByLabel('登录账户').fill('qa');
    await page.getByText('更多连接设置', { exact: true }).click();
    await page.getByLabel('SSH 端口').fill(String(metadata.port));
    await selectKey(page);
    await page.getByRole('heading', { name: '首次连接此服务器' }).waitFor();
    await page.getByRole('button', { name: '信任并连接' }).click();
    await page.getByRole('heading', { name: '安装清单' }).waitFor();
    const first = await page.evaluate(() => window.gaojiDesktop.servers());
    assert.equal(first.ok, true); assert.equal(first.data.length, 1);
    const id = first.data[0].id;
    assert.equal(first.data[0].remembered, false);
    await app.close(); app = null;

    // Attach the installation already dispatched by the real deploy module.
    // This test must never launch a second instance for a screenshot.
    const records = new Connections(path.join(temporary, 'connections'), {});
    records.setDeployment(id, deployment);
    page = await launch();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.getByRole('navigation', { name: '服务器' }).getByRole('button', { name: /本机隔离验收/ }).click();
    await page.getByRole('heading', { name: '重新登录服务器' }).waitFor();
    await selectKey(page);
    await page.getByRole('heading', { name: '服务器详情' }).waitFor();
    const status = await page.evaluate(id => window.gaojiDesktop.installStatus(id), id);
    assert.equal(status.ok, true);
    assert.equal(status.data.job, deployment.job);
    assert.ok(['running', 'queued', 'awaiting_setup', 'failed'].includes(status.data.status));
    if (status.data.status === 'awaiting_setup') {
      await page.getByRole('tab', { name: 'QQ 与模型' }).waitFor();
      await page.locator('.qr-frame svg[width="212"]').waitFor();
      assert.ok(await page.locator('.qr-frame svg[width="212"] path').count() >= 2);
    }
    else await page.getByText(status.data.message, { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    const pictures = path.resolve(__dirname, '../release/qa'); fs.mkdirSync(pictures, { recursive: true });
    const picturePrefix = metadata.slot && metadata.slot !== 'primary'
      ? 'local-vm-' + metadata.slot + '-' + status.data.stage : 'local-vm';
    for (const [name, width, height] of [['desktop', 1320, 900], ['narrow', 390, 844]]) {
      await page.setViewportSize({ width, height });
      await page.evaluate(() => window.scrollTo(0, 0));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: path.join(pictures, picturePrefix + '-' + name + '.png'), fullPage: true });
    }
    let capacity;
    if (process.argv.includes('--capacity')) {
      assert.equal(status.data.status, 'awaiting_setup', 'Capacity acceptance requires the completed original installation');
      await page.setViewportSize({ width: 1320, height: 900 });
      await page.getByRole('tab', { name: '存储管理', exact: true }).click();
      const region = page.getByRole('region', { name: '实例容量状态' });
      await region.getByText('可接收新任务', { exact: true }).waitFor();
      await page.locator('.wizard-steps li.current').filter({ hasText: '登录 QQ' }).waitFor();
      assert.equal(await page.locator('.wizard-steps li.done').filter({ hasText: '登录 QQ' }).count(), 0,
        'Opening storage must not mark the offline QQ account as logged in');
      const result = await page.evaluate(id => window.gaojiDesktop.instance({ id, action: 'storageStatus' }), id);
      assert.equal(result.ok, true);
      capacity = result.data.capacity;
      assert.equal(capacity.state, 'ready');
      assert.equal(capacity.complete, true);
      assert.equal(capacity.instance_id, status.data.instance_id);
      assert.equal(capacity.disks.length, 2);
      assert.ok(capacity.used_bytes > 0 && capacity.data_bytes > 0);
      await region.getByText(capacity.policy.reserve_free_gib + ' GiB', { exact: true }).waitFor();
      for (const [name, width, height] of [['desktop', 1320, 900], ['narrow', 390, 844]]) {
        await page.setViewportSize({ width, height });
        await region.scrollIntoViewIfNeeded();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        const clipped = await region.locator('strong, .tag').evaluateAll(elements =>
          elements.some(element => element.scrollWidth > element.clientWidth + 1));
        assert.equal(clipped, false, 'Capacity values overflow their elements');
        await page.screenshot({ path: path.join(pictures, picturePrefix + '-capacity-' + name + '.png'), fullPage: true });
      }
    }
    assert.deepEqual(errors, []);
    const final = await page.evaluate(() => window.gaojiDesktop.servers());
    assert.equal(final.data.length, 1);
    assert.equal(final.data[0].deployment.job, deployment.job);
    console.log(JSON.stringify({ packaged: Boolean(executable), localhostOnly: true,
      actualSSH: true, reopenedWithOriginalJob: true, installationState: status.data.status,
      installationStage: status.data.stage, newInstallDispatched: false, browserErrors: errors,
      capacity: capacity && { state: capacity.state, policy: capacity.policy, used_bytes: capacity.used_bytes } }));
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
