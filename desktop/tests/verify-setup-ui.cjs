'use strict';
// Real Electron/preload/UI, with test-only IPC responses. No SSH, QQ or API call.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function main() {
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-setup-ui-'));
  const directory = path.resolve(__dirname, '../release/qa');
  fs.mkdirSync(directory, { recursive: true });
  let app;
  try {
    app = await electron.launch({ executablePath: process.env.GAOJI_PACKAGED_EXECUTABLE || require('electron'),
      args: [...(process.env.GAOJI_PACKAGED_EXECUTABLE ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:/, route => route.abort());
    await app.evaluate(({ ipcMain }) => {
      const fixture = globalThis.setupFixture = {
        server: { id: 'f'.repeat(32), host: 'ui-fixture.invalid', username: 'test', port: 22, name: '本地界面验收',
          fingerprint: 'test-only', authenticated: true, remembered: false, authType: 'password', updatedAt: 0 },
        status: { instance: { id: 'e'.repeat(32), revision: 1, settings: {
          name: '我的高级', bot_qq: '', admin_qq: '', group_ids: [], allow_all_groups: false, system_prompt: '测试人设',
          models: [], default_model: '', vision_model: '', features: { search: true, subagents: true, media: false, voice: false, video: false,
            browser: false, sandbox: false, historian: false, semantic: false, proactive: false, notifications: false, cluster: false },
        } }, desired: { bot_enabled: false }, operation: null,
        video_model: { model: 'Whisper Base 多语言', total_bytes: 147951465, downloaded_bytes: 0, state: 'not_installed' } },
        qq: { online: false, qq: '', nickname: '', phase: '', qrcode: 'https://qr-fixture.invalid/not-a-real-qq-login', offline: false, has_error: false },
        calls: [], polls: 0, backups: [],
      };
      const response = data => ({ ok: true, data });
      const inspection = { probe: { label: 'Linux (test fixture)', freeBytes: 20 * 1024 ** 3, privilege: 'root' },
        plan: { eligible: true, steps: [], blockers: [], warnings: [], boundaries: [] } };
      const handlers = {
        servers: () => response([fixture.server]),
        applications: () => response([{ ...fixture.server, serverId: fixture.server.id, appId: 'gaoji', name: '已有高级', legacy: true }]),
        'application-catalog': () => response([]),
        inspect: () => response(inspection),
        'install-status': () => response({ job: 'a'.repeat(32), status: 'awaiting_setup', stage: 'verify', completed: [], message: 'fixture' }),
        instance: (_event, request) => {
          const { action, payload } = request;
          if (action === 'status') { fixture.polls++; return response(fixture.status); }
          if (action === 'backups') return response({ backups: fixture.backups });
          if (action === 'qqStatus' || action === 'qqRefresh') return response(fixture.qq);
          if (action === 'services') return fixture.failServices ? { ok: false, error: { code: 'connection_lost', message: 'fixture' } } : response({ ...fixture.serviceExtra, services: [
            { name: 'database', state: 'running', health: 'healthy' }, { name: 'napcat', state: 'running', health: '' },
            ...(fixture.botRunning ? [{ name: 'bot', state: 'running', health: '' }] : []),
          ] });
          fixture.calls.push(request);
          if (action === 'modelList') return response({ models: fixture.discoveredModels || ['fixture-model-a', 'fixture-model-b'] });
          if (action === 'modelTest') {
            if (fixture.delayNextModelTest) {
              fixture.delayNextModelTest = false;
              return new Promise(resolve => { fixture.finishModelTest = () => resolve(response({ responded: true, latency_ms: 100 })); });
            }
            return response({ responded: true, latency_ms: 100 });
          }
          if (action === 'embeddingTest') return response({ responded: true, dimensions: payload.options.dimensions, latency_ms: 100 });
          if (action === 'matrixTest') {
            if (payload.key === '') return { ok: false, error: { code: 'setup_failed', message: '请先填写 Matrix Token' } };
            return response({ responded: true, rooms: payload.options.routes.length, latency_ms: 100 });
          }
          if (action === 'imessageTest') {
            if (payload.key === '') return { ok: false, error: { code: 'setup_failed', message: '请先填写 BlueBubbles 密码' } };
            return response({ responded: true, chats: payload.options.routes.length, send_verified: false, latency_ms: 100 });
          }
          if (action === 'clusterTest') {
            if (payload.key === '') return { ok: false, error: { code: 'setup_failed', message: '请先填写集群接口地址和 Token' } };
            return response({ responded: true, available: 3, unavailable: 1, upstream_ready: fixture.clusterReady !== false, latency_ms: 100 });
          }
          if (action === 'save') {
            if (!fixture.status.cluster_configuration) assertLegacyComponents(payload.settings.components);
            if (!fixture.status.matrix_configuration && Object.hasOwn(payload.settings.components || {}, 'matrix')) throw new Error('Old manager received unsupported Matrix configuration');
            if (!fixture.status.imessage_configuration && Object.hasOwn(payload.settings.components || {}, 'imessage')) throw new Error('Old manager received unsupported iMessage configuration');
            if (!fixture.status.capacity_configuration && Object.hasOwn(payload.settings.components || {}, 'capacity')) throw new Error('Old manager received unsupported capacity configuration');
            if (payload.revision !== fixture.status.instance.revision) return { ok: false, error: { code: 'conflict', message: '配置已被更改，请重新载入' } };
            fixture.status.instance.revision++;
            const previousModels = fixture.status.instance.settings.models;
            fixture.status.instance.settings = payload.settings;
            for (const model of payload.settings.models) model.has_key = Object.hasOwn(payload.model_keys, model.id) ? Boolean(payload.model_keys[model.id]) : Boolean(previousModels.find(item => item.id === model.id)?.has_key);
            const present = new Set(fixture.status.instance.component_keys_present || []);
            for (const [name, key] of Object.entries(payload.component_keys || {})) key ? present.add(name) : present.delete(name);
            fixture.status.instance.component_keys_present = [...present];
            return response(fixture.status);
          }
          if (action === 'operation') {
            if (fixture.loseNextOperationResponse) {
              fixture.loseNextOperationResponse = false;
              return { ok: false, error: { code: 'connection_lost', message: '模拟连接中断，状态未确认' } };
            }
            fixture.status.operation = { id: payload.id, kind: payload.kind, status: 'queued', stage: 'queued', message: payload.kind === 'activate' ? '正在启用机器人' : '维护任务已排队' };
            return response(fixture.status.operation);
          }
          throw new Error('Unexpected fixture action: ' + action);
        },
        'open-console': () => { fixture.calls.push({ action: 'openConsole' }); return response(true); },
        discover: () => { throw new Error('Test must not discover a server'); },
        connect: () => { throw new Error('Test must not connect a server'); },
        install: () => { throw new Error('Test must not install on a server'); },
      };
      function assertLegacyComponents(options) {
        if (options && Object.hasOwn(options, 'cluster')) throw new Error('Old manager received unsupported cluster configuration');
      }
      for (const [name, handler] of Object.entries(handlers)) {
        ipcMain.removeHandler('gaoji:' + name);
        ipcMain.handle('gaoji:' + name, handler);
      }
    });
    if (process.argv.includes('--local-progress')) {
      await app.evaluate(({ ipcMain }) => {
        const f = globalThis.setupFixture;
        f.localProgress = { status: 'running', stage: 'database', message: '正在下载数据库与 QQ 组件',
          completed: ['runtime', 'verify', 'initialize'], stage_started_at: Math.floor(Date.now()/1000)-125,
          updated_at: Math.floor(Date.now()/1000), progress: { detail: '解压依赖', elapsed_seconds: 125, quiet_seconds: 0 } };
        for (const [name, handler] of Object.entries({
          'local-inspect': () => ({ installed: true, ready: true, blockers: [], freeBytes: 30*1024**3, platform: 'darwin' }),
          'local-status': () => f.localProgress,
          'local-start': () => { f.calls.push({ action: 'localStart' }); return f.localProgress = { ...f.localProgress, status: 'running', message: '沿原数据卷继续安装' }; },
        })) { ipcMain.removeHandler('gaoji:' + name); ipcMain.handle('gaoji:' + name, () => ({ ok: true, data: handler() })); }
      });
      await page.getByRole('button', { name: '这台电脑', exact: true }).click();
      await page.getByText('解压依赖', { exact: true }).waitFor();
      assert.equal(await page.locator('.install-step .green').count(), 3);
      assert.equal(await page.getByRole('button', { name: '正在安装', exact: true }).isDisabled(), true);
      await app.evaluate(() => { globalThis.setupFixture.localProgress = { ...globalThis.setupFixture.localProgress, status: 'failed', message: '下载地址解析失败；已有数据保留' }; });
      await page.getByText('下载地址解析失败；已有数据保留', { exact: true }).waitFor();
      await page.screenshot({ path: path.join(directory, 'local-progress-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: path.join(directory, 'local-progress-mobile.png'), fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.getByRole('button', { name: '继续本机安装 / 连接', exact: true }).click();
      await page.getByText('沿原数据卷继续安装', { exact: true }).waitFor();
      assert.equal((await app.evaluate(() => globalThis.setupFixture.calls.filter(c => c.action === 'localStart'))).length, 1);
      assert.deepEqual(errors, []);
      console.log('Local installation stages, failure reason, scoped retry and responsive layout passed. IPC fixture only.');
      return;
    }
    if (process.argv.includes('--local-updates')) {
      await app.evaluate(({ ipcMain }) => {
        const fixture = globalThis.setupFixture;
        fixture.versionTask = null;
        fixture.status.instance.settings.models = [{ id: 'primary', label: 'Fixture', model: 'fixture-model', provider: 'custom', protocol: 'openai-chat',
          base_url: 'https://fixture.invalid/v1', api_key_required: false, has_key: false, vision: false, tools: true,
          thinking: 'default', reasoning_effort: 'default', max_input_tokens: 32000, max_output_tokens: 4096, fallbacks: [] }];
        fixture.status.instance.settings.default_model = 'primary';
        fixture.versionPlan = { current: { checksum: 'a'.repeat(64), version: '0.20.0', revision: 1, maintenance_pending: false },
          available: { checksum: 'b'.repeat(64), version: '0.20.1' }, canUpdate: true, rollbacks: [] };
        const handlers = {
          'local-inspect': () => ({ installed: true, ready: true, recorded: true, blockers: [], freeBytes: 20 * 1024**3, platform: 'darwin' }),
          'local-status': () => ({ status: 'awaiting_setup', stage: 'ready' }),
          'update-plan': () => fixture.versionPlan,
          'update-status': () => fixture.versionTask,
          update: (_event, request) => {
            fixture.calls.push({ action: 'localUpdate', ...request });
            return fixture.versionTask = { id: '1'.repeat(32), status: 'running', stage: 'backup', message: '正在独立后台验证数据库副本', finished: false };
          },
        };
        for (const [name, handler] of Object.entries(handlers)) {
          ipcMain.removeHandler('gaoji:' + name);
          ipcMain.handle('gaoji:' + name, (...args) => ({ ok: true, data: handler(...args) }));
        }
      });
      await page.getByRole('button', { name: '这台电脑', exact: true }).click();
      await page.getByRole('tab', { name: '更新与回退', exact: true }).click();
      await page.getByText('本机机器人版本', { exact: true }).waitFor();
      await page.getByRole('button', { name: '更新此实例', exact: true }).click();
      await page.getByRole('dialog').waitFor();
      assert.equal(await page.getByLabel('服务器 sudo 密码').count(), 0);
      await page.getByRole('button', { name: '确认更新', exact: true }).click();
      await page.getByText('正在独立后台验证数据库副本', { exact: true }).waitFor();
      await page.waitForTimeout(3300);
      assert.equal(await page.getByRole('button', { name: '更新此实例', exact: true }).isDisabled(), true);
      await page.screenshot({ path: path.join(directory, 'local-updates-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: path.join(directory, 'local-updates-mobile.png'), fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      const calls = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'localUpdate'));
      assert.equal(calls.length, 1);
      assert.equal(calls[0].id, 'desktop-local');
      assert.deepEqual(errors, []);
      console.log('Local update UI passed: correct instance, explicit action, no sudo, stable progress, responsive. IPC fixtures only.');
      return;
    }
    if (process.argv.includes('--readiness')) {
      await app.evaluate(() => {
        const fixture = globalThis.setupFixture;
        fixture.status.instance.settings.models = [{ id: 'primary', label: 'Fixture', model: 'fixture-model', provider: 'custom', protocol: 'openai-chat',
          base_url: 'https://fixture.invalid/v1', api_key_required: true, has_key: true, vision: false, tools: true,
          thinking: 'auto', reasoning_effort: '', max_input_tokens: 0, max_output_tokens: 0, fallbacks: [] }];
        fixture.status.instance.settings.default_model = 'primary';
        fixture.status.component_requirements = { sandbox: 'not installed' };
        fixture.status.desired = { bot_enabled: true, revision: 1 };
        fixture.botRunning = true; fixture.serviceExtra = { connection: 'disconnected' };
        fixture.qq.online = true;
      });
      await page.reload();
      await page.locator('.connection-main').click();
      await page.locator('.application-row').filter({ hasText: '已有高级' }).click();
      await page.getByLabel('机器人名称', { exact: true }).fill('未保存的草稿');
      await page.getByRole('tab', { name: '运行检查', exact: true }).click();
      const row = id => page.locator(`[data-check="${id}"]`);
      await row('qq').getByText('已确认', { exact: true }).waitFor();
      await row('connection').getByText('需处理', { exact: true }).waitFor();
      await row('components').getByText('已确认', { exact: true }).waitFor();
      assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.length), 0);
      await page.getByRole('button', { name: '测试默认模型', exact: true }).click();
      await page.getByRole('heading', { name: '测试默认模型？' }).waitFor();
      assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.length), 0);
      await page.getByRole('button', { name: '确认测试', exact: true }).click();
      await row('model').getByText('已确认', { exact: true }).waitFor();
      assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(row => row.action === 'modelTest').length), 1);
      await app.evaluate(({ dialog }, directory) => { dialog.showSaveDialog = async () => ({ canceled: false, filePath: directory + '/runtime-report.json' }); }, temporary);
      await page.getByRole('button', { name: '导出匿名运行报告' }).click();
      await page.getByText('运行报告已保存。', { exact: true }).waitFor();
      const report = JSON.parse(fs.readFileSync(path.join(temporary, 'runtime-report.json'), 'utf8'));
      assert.doesNotMatch(JSON.stringify(report), /fixture|private|invalid|草稿|test-model|https/);
      assert.equal(report.runtime.checks.find(row => row.id === 'connection').state, 'attention');
      await page.screenshot({ path: path.join(directory, 'readiness-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: path.join(directory, 'readiness-mobile.png'), fullPage: true });
      await app.evaluate(() => { globalThis.setupFixture.failServices = true; });
      await page.getByRole('button', { name: '重新检查运行状态' }).click();
      await row('database').getByText('未确认', { exact: true }).waitFor();
      await row('qq').getByText('已确认', { exact: true }).waitFor();
      await app.evaluate(() => { const f = globalThis.setupFixture; f.failServices = false; f.serviceExtra = {}; f.status.instance.revision++; });
      await page.getByRole('button', { name: '重新检查运行状态' }).click();
      await row('connection').getByText('此运行包未提供连接心跳，不能仅凭 QQ 登录推断已接通。', { exact: true }).waitFor();
      await row('model').getByText('未确认', { exact: true }).waitFor();
      await row('configuration').getByText('需处理', { exact: true }).waitFor({ timeout: 15000 });
      await page.getByRole('button', { name: '查看默认聊天模型设置' }).click();
      assert.equal(await page.getByLabel('机器人名称', { exact: true }).inputValue(), '未保存的草稿');
      assert.deepEqual(errors, []);
      console.log('Readiness passed: separated login/link, explicit billed probe, partial failure, old manager, saved revision, draft preservation, report redaction, desktop/mobile. No remote servers.');
      return;
    }
    if (process.argv.includes('--handover')) {
      await app.evaluate(() => {
        globalThis.setupFixture.status.qq_handover = { schema: 1, state: 'pending', operation_id: '6'.repeat(32),
          source_instance_id: '7'.repeat(32), target_instance_id: 'e'.repeat(32) };
        globalThis.setupFixture.qq = { online: false, qq: '', nickname: '', phase: 'handover_required', qrcode: '', offline: true, has_error: false };
      });
      await page.reload();
      await page.locator('.server-row').click();
      await page.locator('.application-row').filter({ hasText: '已有高级' }).click();
      await page.getByRole('heading', { name: '接管迁移后的 QQ' }).waitFor();
      assert.equal(await page.locator('.qr-frame').count(), 0);
      const start = page.getByRole('button', { name: '启动此处 QQ', exact: true });
      assert.equal(await start.isDisabled(), true);
      await page.getByLabel('我已停止原服务器的 QQ').check();
      await page.waitForTimeout(4500);
      assert.equal(await page.getByLabel('我已停止原服务器的 QQ').isChecked(), true);
      await page.screenshot({ path: path.join(directory, 'qq-handover-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: path.join(directory, 'qq-handover-mobile.png'), fullPage: true });
      await app.evaluate(() => { globalThis.setupFixture.loseNextOperationResponse = true; });
      await start.click();
      await page.getByRole('alert').filter({ hasText: '模拟连接中断' }).waitFor();
      await start.click();
      await page.getByText('维护任务已排队', { exact: true }).waitFor();
      const submitted = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'operation'));
      assert.equal(submitted.length, 2);
      assert.deepEqual(submitted[0].payload, submitted[1].payload);
      assert.equal(submitted[0].payload.kind, 'resume_qq');
      assert.equal(submitted[0].payload.confirm, true);
      assert.equal(submitted[0].payload.revision, 1);
      assert.equal(await start.isDisabled(), true);
      await app.evaluate(() => {
        const f = globalThis.setupFixture;
        f.status.qq_handover.state = 'approved';
        f.status.operation = { ...f.status.operation, status: 'failed', message: '登录入口尚未确认' };
      });
      const retry = page.getByRole('button', { name: '重试启动此处 QQ', exact: true });
      await retry.waitFor({ timeout: 15000 });
      await retry.click();
      const retried = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'operation').at(-1).payload);
      assert.notEqual(retried.id, submitted[0].payload.id);
      await app.evaluate(() => {
        const f = globalThis.setupFixture;
        f.status.qq_handover = null;
        f.status.operation = { ...f.status.operation, status: 'completed', stage: 'qq_ready', message: 'QQ 登录入口已就绪' };
        f.qq = { ...f.qq, phase: '', qrcode: 'https://qr-fixture.invalid/not-a-real-qq-login', offline: false };
      });
      await page.locator('.qr-frame svg').waitFor({ timeout: 15000 });
      assert.equal(await page.getByRole('button', { name: '下一步', exact: true }).isDisabled(), true);
      assert.equal(await app.evaluate(() => globalThis.setupFixture.status.desired.bot_enabled), false);
      assert.deepEqual(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action !== 'operation')), []);
      assert.deepEqual(errors, []);
      console.log('QQ handover UI passed: explicit consent, no automatic activation, stable reconnect ID, retry, QR readiness, responsive layout. Local IPC fixtures only.');
      return;
    }
    await page.reload();
    await page.locator('.server-row').click();
    await page.locator('.application-row').filter({ hasText: '已有高级' }).click();
    await page.getByRole('heading', { name: '扫码登录机器人 QQ' }).waitFor();
    assert.equal(await page.locator('.qr-frame svg').count(), 1);
    assert.equal(await page.getByRole('button', { name: '下一步', exact: true }).isEnabled(), false);
    await page.screenshot({ path: path.join(directory, 'setup-qr-desktop.png'), fullPage: true });
    await app.evaluate(() => { globalThis.setupFixture.qq = { online: true, qq: '12345678', nickname: '界面测试账户', phase: '', qrcode: '', offline: false, has_error: false }; });
    await page.getByRole('heading', { name: 'QQ 已登录' }).waitFor({ timeout: 15000 });
    await page.getByRole('button', { name: '下一步', exact: true }).click();
    await page.getByLabel('你的 QQ 号（管理员）').fill('87654321');
    await page.getByLabel('API 地址', { exact: true }).fill('https://model-fixture.invalid/v1');
    await page.getByLabel('API Key', { exact: true }).fill('ui-test-key-not-real');
    if (process.argv.includes('--discovery')) await app.evaluate(() => { globalThis.setupFixture.discoveredModels = [
      'gpt-5.5', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-image-2', 'gpt-image-2.5',
      'gpt-5.3-codex-spark', 'gpt-5.6-terra', 'codex-auto-review', 'gpt-image-1.5', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst']; });
    await page.getByRole('button', { name: '读取列表' }).click();
    await page.getByRole('group', { name: '接口模型列表' }).waitFor();
    if (process.argv.includes('--discovery')) {
      await page.getByText('接口返回 12 个模型', { exact: true }).waitFor();
      assert.equal(await page.locator('.discovered-row').count(), 12);
      await page.getByRole('button', { name: '选用 gpt-5.6-luna', exact: true }).click();
      assert.equal(await page.locator('.discovered-row').count(), 12);
      assert.equal(await page.getByLabel('模型名称', { exact: true }).inputValue(), 'gpt-5.6-luna');
      await page.getByLabel('搜索接口模型', { exact: true }).fill('terra');
      assert.equal(await page.locator('.discovered-row').count(), 1);
      await page.getByLabel('搜索接口模型', { exact: true }).fill('');
      await page.getByLabel('全选可添加的聊天模型', { exact: true }).check();
      await page.getByRole('button', { name: '添加所选 (5)', exact: true }).click();
      await page.getByText('已配置 6 个', { exact: true }).waitFor();
      assert.equal(await page.getByLabel('默认聊天模型', { exact: true }).inputValue(), 'primary');
      assert.equal(await page.getByLabel('全选可添加的聊天模型', { exact: true }).isDisabled(), true);
      await page.getByRole('button', { name: '保存设置', exact: true }).click();
      await page.getByText('配置版本 2', { exact: false }).waitFor();
      const saved = await app.evaluate(() => globalThis.setupFixture.calls.find(call => call.action === 'save').payload);
      assert.equal(saved.settings.models.length, 6);
      assert.equal(Object.keys(saved.model_keys).length, 6);
      assert.equal(Object.values(saved.model_keys).every(value => value === 'ui-test-key-not-real'), true);
      await page.locator('.discovered-models').screenshot({ path: path.join(directory, 'discovery-desktop.png') });
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.locator('.discovered-models').screenshot({ path: path.join(directory, 'discovery-mobile.png') });
      await page.getByLabel(/^API Key/).fill('changed-fixture-key');
      assert.equal(await page.locator('.discovered-models').count(), 0);
      assert.deepEqual(errors, []);
      console.log('Model discovery passed: 12 visible, independent search, six chat profiles, credential copy, stable default, dedup and mobile. No remote requests.');
      return;
    }
    await page.getByLabel('模型名称', { exact: true }).fill('fixture-model-a');
    await app.evaluate(() => { globalThis.setupFixture.qq.online = false; });
    await page.locator('.wizard-steps li.current').filter({ hasText: '登录 QQ' }).waitFor({ timeout: 15000 });
    assert.equal(await page.getByLabel('模型名称', { exact: true }).inputValue(), 'fixture-model-a');
    await app.evaluate(() => { globalThis.setupFixture.qq.online = true; });
    await page.locator('.wizard-steps li.done').filter({ hasText: '登录 QQ' }).waitFor({ timeout: 15000 });
    await page.getByRole('button', { name: '测试连接' }).click();
    await page.getByText('模型已返回文字，耗时 0.1 秒', { exact: true }).waitFor();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByText('配置版本 2', { exact: false }).waitFor();
    const firstSave = await app.evaluate(() => globalThis.setupFixture.calls.find(call => call.action === 'save').payload);
    assert.deepEqual(firstSave.settings.group_ids, []);
    assert.equal(firstSave.settings.allow_all_groups, false);
    assert.deepEqual(firstSave.model_keys, { primary: 'ui-test-key-not-real' });
    assert.equal(await page.getByLabel(/^API Key/).inputValue(), '');
    assert.equal(await page.getByLabel(/^API Key/).getAttribute('type'), 'password');

    await page.getByText('模型高级设置', { exact: true }).click();
    await page.getByLabel('机器人名称', { exact: true }).fill('未保存的名称');
    await page.getByLabel('接口类型', { exact: true }).focus();
    await page.evaluate(() => { window.setupSelect = document.activeElement; });
    const polls = await app.evaluate(() => globalThis.setupFixture.polls);
    await page.waitForTimeout(4500);
    assert.ok(await app.evaluate(() => globalThis.setupFixture.polls) > polls);
    assert.equal(await page.getByLabel('机器人名称', { exact: true }).inputValue(), '未保存的名称');
    assert.equal(await page.evaluate(() => window.setupSelect === document.activeElement && window.setupSelect.isConnected), true);
    assert.equal(await page.getByLabel('接口类型', { exact: true }).isVisible(), true);

    await page.getByLabel('API 地址', { exact: true }).fill('https://replacement-fixture.invalid/v1');
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '更换地址或接口后需要重新填写' }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'save').length), 1);
    await page.getByLabel(/^API Key/).fill('replacement-fixture-key');
    await page.getByLabel('接口类型', { exact: true }).selectOption('anthropic-messages');
    assert.equal(await page.getByLabel(/^API Key/).inputValue(), '');
    await page.getByLabel(/^API Key/).fill('anthropic-fixture-key');
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByText('配置版本 3', { exact: false }).waitFor();
    await page.getByLabel('控制台密码（首次设置至少 12 位）').fill('not-a-real-admin-password');
    await app.evaluate(() => { globalThis.setupFixture.loseNextOperationResponse = true; });
    await page.getByRole('button', { name: '启用机器人', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '模拟连接中断' }).waitFor();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '上次运行操作的状态尚未确认' }).waitFor();
    await page.getByRole('button', { name: '启用机器人', exact: true }).click();
    await page.getByText('正在启用机器人', { exact: true }).waitFor();
    const operations = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'operation'));
    assert.equal(operations.length, 2);
    assert.deepEqual(operations[0].payload, operations[1].payload);
    assert.equal(await page.getByRole('button', { name: '启用机器人', exact: true }).isDisabled(), true);
    await app.evaluate(() => {
      const fixture = globalThis.setupFixture;
      fixture.status.operation.status = 'completed'; fixture.status.operation.message = '机器人已连接';
      fixture.status.desired.bot_enabled = true;
    });
    await page.getByText('机器人已连接', { exact: true }).waitFor({ timeout: 15000 });
    await page.getByRole('button', { name: '打开控制台', exact: true }).click();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'openConsole').length), 1);
    await page.getByRole('button', { name: '创建备份', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    await page.getByRole('button', { name: '取消维护操作' }).click();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'operation').length), 2);
    await page.getByRole('button', { name: '创建备份', exact: true }).click();
    await page.getByRole('button', { name: '开始备份', exact: true }).click();
    await page.getByText('维护任务已排队', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'operation').at(-1).payload.kind), 'backup');
    await app.evaluate(() => {
      const fixture = globalThis.setupFixture;
      fixture.status.operation = { ...fixture.status.operation, status: 'completed', stage: 'backed_up', message: '备份完成' };
      fixture.backups = Array.from({ length: 6 }, (_, index) => ({ id: String(index + 1).repeat(32), created_at: 1700000000 - index * 60,
        bytes: 123456789, files: 8000, revision: 3, sha256: 'a'.repeat(64) }));
    });
    await page.getByText('备份完成', { exact: true }).waitFor({ timeout: 15000 });
    await page.locator('.backup-row').first().waitFor();
    assert.equal(await page.locator('.backup-row').count(), 5);
    await page.getByRole('button', { name: '全部 6 份' }).click();
    assert.equal(await page.locator('.backup-row').count(), 6);
    await page.getByRole('button', { name: /^恢复 .* 的备份$/ }).first().click();
    await page.getByRole('dialog').waitFor();
    await page.screenshot({ path: path.join(directory, 'backup-restore-dialog.png') });
    await page.getByRole('button', { name: '确认恢复', exact: true }).click();
    await page.getByText('维护任务已排队', { exact: true }).waitFor();
    const restoreRequest = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'operation').at(-1).payload);
    assert.equal(restoreRequest.kind, 'restore'); assert.equal(restoreRequest.confirm, true); assert.equal(restoreRequest.backup_id, '1'.repeat(32));
    await app.evaluate(() => {
      const fixture = globalThis.setupFixture;
      fixture.status.operation = { ...fixture.status.operation, status: 'completed', stage: 'restored', message: '恢复已核对' };
      fixture.status.instance.revision = 4; fixture.status.instance.settings.name = '恢复后的名称';
      fixture.status.desired.bot_enabled = false;
    });
    await page.getByText('恢复已核对', { exact: true }).waitFor({ timeout: 15000 });
    assert.equal(await page.getByLabel('机器人名称', { exact: true }).inputValue(), '恢复后的名称');
    await page.getByText('功能组件', { exact: true }).click();
    assert.equal(await page.getByRole('switch', { name: '隔离沙盒', exact: true }).isDisabled(), true);
    await app.evaluate(() => { globalThis.setupFixture.status.sandbox_runtime = { supported: true, state: 'not_installed', bytes: 0 }; });
    await page.getByRole('switch', { name: '隔离沙盒', exact: true }).check({ timeout: 15000 });
    await page.getByText('应用时安装镜像', { exact: true }).waitFor();
    await page.getByRole('switch', { name: '语音回复', exact: true }).check();
    await page.getByRole('switch', { name: '语义记忆检索', exact: true }).check();
    await page.getByLabel('音色名称', { exact: true }).fill('zh-CN-XiaoyiNeural');
    await page.getByLabel('Embedding API 地址', { exact: true }).fill('https://embedding-fixture.invalid/v1');
    await page.getByLabel('Embedding 服务需要密钥', { exact: true }).check();
    await page.getByLabel(/^Embedding Key/).fill('embedding-ui-fixture-secret');
    await page.getByRole('button', { name: '测试向量接口', exact: true }).click();
    await page.getByText('已返回 1024 维向量，耗时 0.1 秒', { exact: true }).waitFor();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByText('配置版本 5', { exact: false }).waitFor();
    const componentSave = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'save').at(-1).payload);
    assert.equal(componentSave.settings.features.voice, true);
    assert.equal(componentSave.settings.features.semantic, true);
    assert.equal(componentSave.settings.features.sandbox, true);
    assert.equal(componentSave.settings.components.voice.name, 'zh-CN-XiaoyiNeural');
    assert.equal(Object.hasOwn(componentSave.settings.components, 'cluster'), false);
    assert.equal(Object.hasOwn(componentSave.settings.components, 'imessage'), false);
    assert.deepEqual(componentSave.component_keys, { embedding: 'embedding-ui-fixture-secret' });
    assert.equal(JSON.stringify(componentSave.settings).includes('embedding-ui-fixture-secret'), false);
    assert.equal(await page.getByLabel(/^Embedding Key/).inputValue(), '');
    assert.equal(await page.getByLabel(/^Embedding Key/).getAttribute('type'), 'password');
    await page.getByLabel('Embedding 模型', { exact: true }).fill('unsaved-embedding-model');
    await page.getByLabel('Embedding 模型', { exact: true }).focus();
    await page.evaluate(() => { window.embeddingInput = document.activeElement; });
    await page.waitForTimeout(4500);
    assert.equal(await page.getByLabel('Embedding 模型', { exact: true }).inputValue(), 'unsaved-embedding-model');
    assert.equal(await page.evaluate(() => window.embeddingInput === document.activeElement && window.embeddingInput.isConnected), true);
    await page.getByRole('button', { name: '测试向量接口', exact: true }).click();
    await page.getByText('已返回 1024 维向量，耗时 0.1 秒', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'embeddingTest').at(-1).payload.key), null);
    await page.getByLabel('Embedding API 地址', { exact: true }).fill('https://changed-embedding-fixture.invalid/v1');
    await page.getByRole('button', { name: '测试向量接口', exact: true }).click();
    await page.getByText('已返回 1024 维向量，耗时 0.1 秒', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'embeddingTest').at(-1).payload.key), '');
    await page.getByLabel(/^Embedding Key/).fill('');
    assert.equal(await page.getByRole('switch', { name: '外部集群', exact: true }).isDisabled(), true);
    assert.equal(await page.getByLabel('本实例数据额度（GiB）', { exact: true }).count(), 0);
    await app.evaluate(() => {
      globalThis.setupFixture.status.cluster_configuration = { schema: 1 };
      globalThis.setupFixture.status.capacity_configuration = { schema: 1 };
    });
    await page.getByRole('switch', { name: '外部集群', exact: true }).check({ timeout: 15000 });
    await page.getByLabel('本实例数据额度（GiB）', { exact: true }).fill('70');
    await page.getByLabel('磁盘预留空间（GiB）', { exact: true }).fill('3');
    await page.getByLabel('本实例数据额度（GiB）', { exact: true }).focus();
    await page.waitForTimeout(4500);
    assert.equal(await page.getByLabel('本实例数据额度（GiB）', { exact: true }).inputValue(), '70');
    assert.equal(await page.getByLabel('本实例数据额度（GiB）', { exact: true }).evaluate(element => document.activeElement === element), true);
    await page.getByLabel('集群网关地址', { exact: true }).fill('https://cluster-fixture.invalid');
    await page.getByLabel(/^集群 Token/).fill('cluster-ui-fixture-token-not-a-real-secret');
    await page.getByLabel('启用的 QQ 群号（每行一个）').fill('12345678\n23456789');
    await page.getByLabel('允许集群查询的群号', { exact: true }).fill('12345678\n');
    assert.equal(await page.getByLabel('允许集群查询的群号', { exact: true }).inputValue(), '12345678\n');
    await page.getByLabel('允许集群查询的群号', { exact: true }).pressSequentially('23456789');
    await page.getByLabel('允许查看集群日志的群号', { exact: true }).fill('12345678');
    await page.getByRole('button', { name: '检测集群连接', exact: true }).click();
    await page.getByText('网关已连接 · 3 项可用 / 1 项不可用 · 0.1 秒', { exact: true }).waitFor();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByText('配置版本 6', { exact: false }).waitFor();
    const clusterSave = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'save').at(-1).payload);
    assert.deepEqual(clusterSave.settings.components.capacity, { max_data_gib: 70, reserve_free_gib: 3 });
    assert.deepEqual(clusterSave.settings.components.cluster.allowed_groups, ['12345678', '23456789']);
    assert.deepEqual(clusterSave.settings.components.cluster.log_allowed_groups, ['12345678']);
    assert.equal(clusterSave.component_keys.cluster, 'cluster-ui-fixture-token-not-a-real-secret');
    assert.equal(JSON.stringify(clusterSave.settings).includes('cluster-ui-fixture-token'), false);
    assert.equal(await page.getByLabel(/^集群 Token/).inputValue(), '');
    assert.equal(await page.getByLabel(/^集群 Token/).getAttribute('type'), 'password');
    await page.getByRole('button', { name: '检测集群连接', exact: true }).click();
    await page.getByText('网关已连接 · 3 项可用 / 1 项不可用 · 0.1 秒', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'clusterTest').at(-1).payload.key), null);
    await app.evaluate(() => { globalThis.setupFixture.clusterReady = false; });
    await page.getByRole('button', { name: '检测集群连接', exact: true }).click();
    await page.getByText('网关已连接，但上游查询尚未就绪；不代表服务器状态正常', { exact: true }).waitFor();
    await page.getByLabel('允许集群查询的群号', { exact: true }).fill('12345678\n23456789\n34567890\n');
    await page.getByLabel('允许集群查询的群号', { exact: true }).focus();
    await page.waitForTimeout(4500);
    assert.equal(await page.getByLabel('允许集群查询的群号', { exact: true }).inputValue(), '12345678\n23456789\n34567890\n');
    assert.equal(await page.getByLabel('允许集群查询的群号', { exact: true }).evaluate(element => document.activeElement === element), true);
    await page.getByLabel('集群网关地址', { exact: true }).fill('https://changed-cluster-fixture.invalid');
    assert.equal(await page.getByText('网关已连接，但上游查询尚未就绪；不代表服务器状态正常', { exact: true }).count(), 0);
    await page.getByRole('button', { name: '检测集群连接', exact: true }).click();
    await page.getByText('请先填写集群接口地址和 Token', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'clusterTest').at(-1).payload.key), '');
    assert.equal(await page.getByRole('switch', { name: 'Matrix 消息互通', exact: true }).isDisabled(), true);
    await app.evaluate(() => { globalThis.setupFixture.status.matrix_configuration = { schema: 1 }; });
    await page.getByRole('switch', { name: 'Matrix 消息互通', exact: true }).check({ timeout: 15000 });
    await page.getByLabel('Matrix 服务地址', { exact: true }).fill('https://matrix-ui-fixture.invalid');
    await page.getByLabel('Matrix 用户 ID', { exact: true }).fill('@bot:example.test');
    await page.getByLabel(/^Matrix Access Token/).fill('matrix-ui-fixture-token-not-real');
    await page.getByRole('button', { name: '添加会话映射', exact: true }).click();
    await page.getByLabel('QQ 群号 1', { exact: true }).fill('12345678');
    await page.getByLabel('Matrix 房间 ID 1', { exact: true }).fill('!fixture:example.test');
    await page.getByRole('button', { name: '添加会话映射', exact: true }).click();
    await page.getByLabel('QQ 群号 2', { exact: true }).fill('23456789');
    await page.getByLabel('Matrix 房间 ID 2', { exact: true }).fill('!second:example.test');
    await page.getByRole('button', { name: '移除会话映射 2', exact: true }).click();
    assert.equal(await page.locator('.matrix-mapping').count(), 1);
    await page.getByRole('button', { name: '检测 Matrix', exact: true }).click();
    await page.getByText('账户与 1 个房间已核对 · 0.1 秒 · 未发送消息', { exact: true }).waitFor();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByText('配置版本 7', { exact: false }).waitFor();
    const matrixSave = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'save').at(-1).payload);
    assert.equal(matrixSave.settings.components.matrix.enabled, true);
    assert.equal(matrixSave.settings.components.matrix.routes.length, 1);
    assert.equal(matrixSave.settings.components.matrix.routes[0].room_id, '!fixture:example.test');
    assert.equal(matrixSave.component_keys.matrix, 'matrix-ui-fixture-token-not-real');
    assert.equal(JSON.stringify(matrixSave.settings).includes('matrix-ui-fixture-token'), false);
    assert.equal(await page.getByLabel(/^Matrix Access Token/).inputValue(), '');
    assert.equal(await page.getByLabel(/^Matrix Access Token/).getAttribute('type'), 'password');
    await page.getByRole('button', { name: '检测 Matrix', exact: true }).click();
    await page.getByText('账户与 1 个房间已核对 · 0.1 秒 · 未发送消息', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'matrixTest').at(-1).payload.key), null);
    await page.getByLabel('Matrix 房间 ID 1', { exact: true }).fill('!unsaved-room:example.test');
    await page.getByLabel('Matrix 房间 ID 1', { exact: true }).focus();
    await page.waitForTimeout(4500);
    assert.equal(await page.getByLabel('Matrix 房间 ID 1', { exact: true }).inputValue(), '!unsaved-room:example.test');
    assert.equal(await page.getByLabel('Matrix 房间 ID 1', { exact: true }).evaluate(element => document.activeElement === element), true);
    await page.getByLabel('Matrix 用户 ID', { exact: true }).fill('@other:example.test');
    assert.equal(await page.getByText('账户与 1 个房间已核对 · 0.1 秒 · 未发送消息', { exact: true }).count(), 0);
    await page.getByRole('button', { name: '检测 Matrix', exact: true }).click();
    await page.getByText('请先填写 Matrix Token', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'matrixTest').at(-1).payload.key), '');
    assert.equal(await page.getByRole('switch', { name: 'iMessage 消息互通', exact: true }).isDisabled(), true);
    await app.evaluate(() => { globalThis.setupFixture.status.imessage_configuration = { schema: 1 }; });
    await page.getByRole('switch', { name: 'iMessage 消息互通', exact: true }).check({ timeout: 15000 });
    await page.getByLabel('BlueBubbles 地址（你自己的 Mac）', { exact: true }).fill('https://bluebubbles-ui-fixture.invalid');
    await page.getByLabel('iMessage 自身标识', { exact: true }).fill('bot@example.test');
    await page.getByLabel(/^BlueBubbles 密码/).fill('imessage-ui-fixture-password-not-real');
    await page.getByRole('button', { name: '添加 iMessage 映射', exact: true }).click();
    await page.getByLabel('iMessage 对应 QQ 群号 1', { exact: true }).fill('12345678');
    await page.getByLabel('iMessage 会话 GUID 1', { exact: true }).fill('iMessage;+;chat-test');
    await page.getByRole('button', { name: '添加 iMessage 映射', exact: true }).click();
    await page.getByRole('button', { name: '移除 iMessage 映射 2', exact: true }).click();
    assert.equal(await page.locator('.imessage-mapping').count(), 1);
    await page.getByRole('button', { name: '检测 iMessage', exact: true }).click();
    await page.getByText('已核对 1 个会话的读取接口 · 0.1 秒 · 未验证发送', { exact: true }).waitFor();
    await page.getByRole('button', { name: '保存设置', exact: true }).click();
    await page.getByText('配置版本 8', { exact: false }).waitFor();
    const imessageSave = await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'save').at(-1).payload);
    assert.equal(imessageSave.settings.components.matrix.enabled, true);
    assert.equal(imessageSave.settings.components.imessage.routes[0].chat_guid, 'iMessage;+;chat-test');
    assert.equal(imessageSave.component_keys.imessage, 'imessage-ui-fixture-password-not-real');
    assert.equal(JSON.stringify(imessageSave.settings).includes('imessage-ui-fixture-password'), false);
    assert.equal(await page.getByLabel(/^BlueBubbles 密码/).inputValue(), '');
    assert.equal(await page.getByLabel(/^BlueBubbles 密码/).getAttribute('type'), 'password');
    await page.getByRole('button', { name: '检测 iMessage', exact: true }).click();
    await page.getByText('已核对 1 个会话的读取接口 · 0.1 秒 · 未验证发送', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'imessageTest').at(-1).payload.key), null);
    await page.getByLabel('iMessage 会话 GUID 1', { exact: true }).fill('iMessage;+;unsaved-chat');
    await page.getByLabel('iMessage 会话 GUID 1', { exact: true }).focus();
    await page.waitForTimeout(4500);
    assert.equal(await page.getByLabel('iMessage 会话 GUID 1', { exact: true }).inputValue(), 'iMessage;+;unsaved-chat');
    assert.equal(await page.getByLabel('iMessage 会话 GUID 1', { exact: true }).evaluate(element => document.activeElement === element), true);
    await page.getByLabel('BlueBubbles 地址（你自己的 Mac）', { exact: true }).fill('https://changed-bluebubbles.invalid');
    await page.getByRole('button', { name: '检测 iMessage', exact: true }).click();
    await page.getByText('请先填写 BlueBubbles 密码', { exact: true }).waitFor();
    assert.equal(await app.evaluate(() => globalThis.setupFixture.calls.filter(call => call.action === 'imessageTest').at(-1).payload.key), '');
    await page.waitForFunction(() => !document.querySelector('.imessage-options button')?.disabled);
    await page.locator('.imessage-options').screenshot({ path: path.join(directory, 'component-imessage-desktop.png') });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(directory, 'setup-settings-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('.feature-switches').screenshot({ path: path.join(directory, 'component-switches-mobile.png') });
    await page.locator('.component-options').filter({ has: page.getByRole('heading', { name: 'Embedding', exact: true }) }).screenshot({ path: path.join(directory, 'component-embedding-mobile.png') });
    await page.locator('.component-options').filter({ has: page.getByRole('heading', { name: 'Gaoji Cluster Control', exact: true }) }).screenshot({ path: path.join(directory, 'component-cluster-mobile.png') });
    await page.locator('.matrix-options').screenshot({ path: path.join(directory, 'component-matrix-mobile.png') });
    await page.locator('.imessage-options').screenshot({ path: path.join(directory, 'component-imessage-mobile.png') });
    await page.locator('.component-options').filter({ has: page.getByRole('heading', { name: '存储容量', exact: true }) }).screenshot({ path: path.join(directory, 'component-capacity-mobile.png') });
    await page.screenshot({ path: path.join(directory, 'setup-settings-mobile.png'), fullPage: true });
    await require('./verify-models-ui.cjs')(page, app, directory);
    assert.deepEqual(errors, []);
    console.log('Setup UI passed with IPC fixtures only: QR, keys, stable retry IDs, backup restore, component controls and key isolation, desktop/mobile. No server contacted.');
  } catch (error) {
    if (app) {
      const page = await app.firstWindow();
      await page.screenshot({ path: path.join(directory, 'setup-failure.png'), fullPage: true }).catch(() => {});
      console.error((await page.locator('body').innerText()).slice(0, 4000));
    }
    throw error;
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
