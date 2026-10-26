'use strict';
// Actual Electron IPC/storage/transport code, backed only by in-memory SSH fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-lifecycle-ui-'));
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
    await app.evaluate(({ app }) => {
      const require = process.getBuiltinModule('module').createRequire(app.getAppPath() + '/package.json');
      const { EventEmitter } = require('node:events');
      const ssh = require('./lib/ssh.cjs'), deploy = require('./lib/deploy.cjs'), control = require('./lib/control.cjs');
      const f = globalThis.lifecycleFixture = { job: 'b'.repeat(32), instance: 'c'.repeat(32), prepares: 0, dispatches: [], polls: 0,
        running: true, state: 'installed', records: {}, latest: null, configReads: 0, lostAck: true,
        settings: { name: '原实例', bot_qq: '12345678', admin_qq: '87654321', group_ids: [], allow_all_groups: false,
          system_prompt: '', models: [{ id: 'primary', label: '测试模型', provider: 'openai-compatible', protocol: 'openai-chat', base_url: 'https://fixture.invalid/v1',
            model: 'fixture', api_key_required: false, tools: true, vision: false, reasoning_effort: '', max_input_tokens: 32000, max_output_tokens: 2048, fallbacks: [] }],
          default_model: 'primary', vision_model: '', features: {} },
      };
      ssh.connect = async target => {
        if (target.host !== 'fixture.invalid') throw new Error('Real SSH forbidden');
        const client = new EventEmitter(); client.end = () => client.emit('close'); return client;
      };
      ssh.discoverHost = async () => { throw new Error('Discovery forbidden'); };
      ssh.inspectServer = async () => ({ probe: { uid: 1000, label: '本机模拟 Linux', freeBytes: 20 * 1024 ** 3, privilege: 'password-required' },
        plan: { eligible: true, adapter: 'fixture', steps: [], blockers: [], warnings: [], boundaries: [] } });
      deploy.status = async () => ({ status: 'awaiting_setup', job: f.job, completed: [], stage: 'verify', message: 'fixture' });
      deploy.start = async (_client, _directory, options) => {
        options.onPrepared({ job: f.job, checksum: 'd'.repeat(64), version: 'fixture', startedAt: 1 }); return deploy.status();
      };
      deploy.retry = async () => { throw new Error('Must not reinstall'); };
      deploy.privileged = async (_client, script, _privilege, password) => {
        if (password !== 'fixture-sudo') throw new Error('Unexpected credentials');
        const prepared = script.match(/'prepare' '(uninstall|reinstall|purge)' '' <</);
        if (prepared) {
          const id = (++f.prepares).toString(16).padStart(32, '0');
          const value = { schema: 1, id, job: f.job, instance_id: f.instance, kind: prepared[1], status: 'preview', stage: 'prepare',
            message: '维护清单已生成，尚未执行', created_at: 1789420800, updated_at: 1789420800, resumable: false, service_state: f.state,
            units: [`gaoji-app-install-${f.job}.service`, `gaoji-app-${f.instance}.service`, `gaoji-app-sandbox-${f.instance}.service`, `gaoji-app-control-${f.instance}.service`],
            data_directory: '/var/lib/gaoji-app/data/instances/' + f.instance, data_retained: true, shared_software_not_removed: true };
          if (prepared[1] === 'purge') Object.assign(value, { data_retained: false, units: [], audit_retained: true, data_bytes: 3 * 1024 ** 3,
            delete_resources: { containers: [{ id: 'a'.repeat(64), name: 'gaoji-' + f.instance + '-bot-1' }],
              volumes: [{ id: 'gaoji-app-' + f.instance + '-nix', name: 'gaoji-app-' + f.instance + '-nix' }], networks: [] } });
          f.records[id] = value; f.latest = id;
          return { code: 0, stdout: JSON.stringify({ ok: true, data: value }) };
        }
        const sent = script.match(/'dispatch' '([a-f0-9]{32})' '([a-f0-9]*)' <</);
        if (!sent || !f.records[sent[1]]) throw new Error('Unexpected privileged operation');
        const value = f.records[sent[1]];
        if (value.kind === 'purge' && sent[2] !== f.instance) throw new Error('Separate erase identity missing');
        f.dispatches.push(sent[1]); f.running = true;
        Object.assign(value, { status: 'running', stage: 'stopping', service_state: value.kind === 'purge' ? 'purging' : value.kind === 'uninstall' ? 'removing' : 'restoring',
          resumable: true, message: '服务维护正在执行' });
        if (f.lostAck) { f.lostAck = false; return { code: 1, stdout: '' }; }
        return { code: 0, stdout: JSON.stringify({ ok: true, data: value }) };
      };
      ssh.execute = async (_client, command) => {
        const read = command.match(/^cat \/var\/lib\/gaoji-app\/status\/lifecycles\/([a-f0-9]{32})\.json$/);
        if (read) { f.polls++; return f.records[read[1]] ? { code: 0, stdout: JSON.stringify(f.records[read[1]]) } : { code: 1, stdout: '' }; }
        if (/^systemctl show gaoji-app-lifecycle-[a-f0-9]{32}\.service --property=ActiveState --value$/.test(command)) return { code: 0, stdout: f.running ? 'active' : 'failed' };
        throw new Error('Unexpected SSH command');
      };
      control.accessFor = async () => ({});
      control.request = async (_client, _access, action) => {
        f.configReads++;
        if (action === 'status') return { instance: { id: f.instance, revision: 2, settings: f.settings }, desired: { bot_enabled: true }, operation: null };
        if (action === 'qqStatus') return { online: true, qq: '12345678', nickname: '高级' };
        if (action === 'services') return { services: [] };
        if (action === 'backups') return { backups: [] };
        throw new Error('Unexpected instance action');
      };
    });
    const serverId = await page.evaluate(async () => {
      const connected = await window.gaojiDesktop.connect({ name: '本地维护验收', host: 'fixture.invalid', port: 22, username: 'tester',
        fingerprint: 'SHA256:' + 'A'.repeat(43), remember: false, authentication: { type: 'password', password: 'fixture-ssh' } });
      if (!connected.ok) throw new Error(connected.error.message);
      const installed = await window.gaojiDesktop.install({ id: connected.data.server.id, password: 'fixture-sudo' });
      if (!installed.ok) throw new Error(installed.error.message);
      return connected.data.server.id;
    });
    await page.reload();
    const select = async () => {
      await page.getByRole('navigation', { name: '服务器' }).getByRole('button').click();
      await page.getByRole('tab', { name: '服务维护', exact: true }).click();
    };
    await select();
    const openUninstall = page.getByRole('button', { name: '卸载服务，保留数据', exact: true });
    await openUninstall.click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('服务器 sudo 密码').fill('fixture-sudo');
    await dialog.getByRole('button', { name: '生成维护清单', exact: true }).click();
    await dialog.getByLabel('维护服务清单').waitFor();
    assert.equal(await dialog.locator('li').count(), 4);
    assert.equal(await app.evaluate(() => globalThis.lifecycleFixture.dispatches.length), 0);
    await dialog.getByRole('button', { name: '取消服务维护', exact: true }).click();
    await page.getByRole('button', { name: '查看待确认清单', exact: true }).click();
    assert.equal(await dialog.getByLabel('服务器 sudo 密码').inputValue(), '');
    await dialog.getByLabel('服务器 sudo 密码').fill('fixture-sudo');
    await dialog.getByLabel('服务器 sudo 密码').focus();
    const polls = await app.evaluate(() => globalThis.lifecycleFixture.polls);
    await page.waitForTimeout(3500);
    assert.ok(await app.evaluate(() => globalThis.lifecycleFixture.polls) > polls);
    assert.equal(await dialog.getByLabel('服务器 sudo 密码').inputValue(), 'fixture-sudo');
    assert.equal(await dialog.getByLabel('服务器 sudo 密码').evaluate(el => document.activeElement === el), true);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(output, 'lifecycle-preview-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await dialog.evaluate(el => el.scrollWidth > el.clientWidth), false);
    const button = await dialog.getByRole('button', { name: '确认卸载服务', exact: true }).boundingBox();
    assert.ok(button.y >= 0 && button.y + button.height <= 844);
    await page.screenshot({ path: path.join(output, 'lifecycle-preview-mobile.png') });
    await dialog.getByRole('button', { name: '确认卸载服务', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await page.getByText('服务维护正在执行', { exact: true }).waitFor();
    const originalId = await app.evaluate(() => globalThis.lifecycleFixture.latest);
    assert.deepEqual(await app.evaluate(() => globalThis.lifecycleFixture.dispatches), [originalId]);
    // Renderer restart uses the real persisted main-process record; no new installation.
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.reload(); await select();
    await page.getByText('服务维护正在执行', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.lifecycleFixture.prepares), 1);
    const blocked = await page.evaluate(id => window.gaojiDesktop.instance({ id, action: 'save', payload: {} }), serverId);
    assert.equal(blocked.ok, false); assert.equal(blocked.error.code, 'service_maintenance');
    await app.evaluate(() => { globalThis.lifecycleFixture.running = false; });
    await page.getByRole('button', { name: '继续原维护任务', exact: true }).click();
    await dialog.getByLabel('服务器 sudo 密码').fill('fixture-sudo');
    await dialog.getByRole('button', { name: '继续此任务', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(await app.evaluate(() => globalThis.lifecycleFixture.dispatches), [originalId, originalId]);
    await app.evaluate(() => { const f = globalThis.lifecycleFixture; f.state = 'retained'; f.running = false;
      Object.assign(f.records[f.latest], { status: 'completed', stage: 'verified', resumable: false, service_state: f.state, message: '服务已卸载，数据未删除' }); });
    await page.getByText('服务已卸载，数据未删除', { exact: true }).waitFor();
    const count = await app.evaluate(() => globalThis.lifecycleFixture.configReads);
    await page.getByRole('tab', { name: 'QQ 与模型', exact: true }).click();
    await page.getByText('此实例正在维护或已卸载服务。请到“服务维护”查看进度或重新启用。', { exact: true }).waitFor();
    await page.waitForTimeout(4500);
    assert.equal(await app.evaluate(() => globalThis.lifecycleFixture.configReads), count);
    await page.getByRole('tab', { name: '服务维护', exact: true }).click();
    await page.getByRole('button', { name: '重新启用原实例', exact: true }).click();
    await dialog.getByLabel('服务器 sudo 密码').fill('fixture-sudo');
    await dialog.getByRole('button', { name: '生成维护清单', exact: true }).click();
    await dialog.getByLabel('维护服务清单').waitFor();
    assert.equal(await app.evaluate(() => globalThis.lifecycleFixture.dispatches.length), 2);
    await dialog.getByRole('button', { name: '确认重新启用', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await app.evaluate(() => { const f = globalThis.lifecycleFixture; f.state = 'installed'; f.running = false;
      Object.assign(f.records[f.latest], { status: 'completed', stage: 'verified', resumable: false, service_state: f.state, message: '原实例已恢复' }); });
    await page.getByText('原实例已恢复', { exact: true }).waitFor();
    await page.getByRole('tab', { name: 'QQ 与模型', exact: true }).click();
    await page.getByLabel('机器人名称', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('机器人名称', { exact: true }).inputValue(), '原实例');
    await page.getByRole('tab', { name: '服务维护', exact: true }).click();
    await openUninstall.click();
    await dialog.getByLabel('服务器 sudo 密码').fill('fixture-sudo');
    await dialog.getByRole('button', { name: '生成维护清单', exact: true }).click();
    await dialog.getByRole('button', { name: '确认卸载服务', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await app.evaluate(() => { const f = globalThis.lifecycleFixture; f.state = 'retained'; f.running = false;
      Object.assign(f.records[f.latest], { status: 'completed', stage: 'verified', resumable: false, service_state: f.state, message: '可以单独确认删除数据' }); });
    await page.getByRole('button', { name: '彻底删除实例', exact: true }).click();
    await dialog.getByLabel('服务器 sudo 密码').fill('fixture-sudo');
    await dialog.getByRole('button', { name: '生成维护清单', exact: true }).click();
    await dialog.getByLabel('输入“删除”以确认永久删除').waitFor();
    assert.equal(await dialog.getByRole('button', { name: '永久删除上述数据', exact: true }).isDisabled(), true);
    const beforePurge = await app.evaluate(() => globalThis.lifecycleFixture.dispatches.length);
    const eraseId = await app.evaluate(() => globalThis.lifecycleFixture.latest);
    const denied = await page.evaluate(({ id, operationId }) => window.gaojiDesktop.lifecycleSubmit({ id, operationId, confirm: true, password: 'fixture-sudo' }), { id: serverId, operationId: eraseId });
    assert.equal(denied.ok, false); assert.equal(denied.error.code, 'confirmation_required');
    assert.equal(await app.evaluate(() => globalThis.lifecycleFixture.dispatches.length), beforePurge);
    await dialog.getByLabel('输入“删除”以确认永久删除').fill('删除');
    await dialog.locator('summary').first().click();
    await page.screenshot({ path: path.join(output, 'purge-preview-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await dialog.evaluate(el => el.scrollWidth > el.clientWidth), false);
    const eraseButton = await dialog.getByRole('button', { name: '永久删除上述数据', exact: true }).boundingBox();
    assert.ok(eraseButton.y >= 0 && eraseButton.y + eraseButton.height <= 844);
    await page.screenshot({ path: path.join(output, 'purge-preview-mobile.png') });
    await dialog.getByRole('button', { name: '永久删除上述数据', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(await app.evaluate(() => globalThis.lifecycleFixture.dispatches.length), beforePurge + 1);
    await app.evaluate(() => { const f = globalThis.lifecycleFixture; f.state = 'removed'; f.running = false;
      Object.assign(f.records[f.latest], { status: 'completed', stage: 'verified', resumable: false, service_state: f.state, message: '实例数据已永久删除' }); });
    await page.getByText('实例数据已永久删除', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '重新启用原实例', exact: true }).count(), 0);
    const blockedInstall = await page.evaluate(id => window.gaojiDesktop.install({ id, password: 'fixture-sudo' }), serverId);
    assert.equal(blockedInstall.ok, false); assert.equal(blockedInstall.error.code, 'service_maintenance');
    const localData = fs.readFileSync(path.join(temporary, 'connections/servers.json'), 'utf8');
    assert.doesNotMatch(localData, /fixture-sudo|fixture-ssh/);
    assert.equal(JSON.parse(localData).servers[0].lifecycle.settled, true);
    assert.deepEqual(errors, []);
    console.log('Lifecycle UI passed: actual IPC/persistence, confirm-only dispatch, lost acknowledgement, same-ID resume, paused polling, restore, separate purge confirmation, removed-instance guard and narrow-screen layout. No server contacted.');
  } catch (error) {
    if (app) await (await app.firstWindow()).screenshot({ path: path.join(output, 'lifecycle-failure.png'), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
