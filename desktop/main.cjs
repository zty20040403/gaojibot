'use strict';

const { app, BrowserWindow, Menu, ipcMain, dialog, protocol, net, safeStorage, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { randomUUID } = require('node:crypto');
const { Connections } = require('./lib/connections.cjs');
const ssh = require('./lib/ssh.cjs');
const deploy = require('./lib/deploy.cjs');
const control = require('./lib/control.cjs');
const updates = require('./lib/updates.cjs');
const lifecycle = require('./lib/lifecycle.cjs');
const applications = require('./lib/applications.cjs');
const { ApplicationMonitor } = require('./lib/application-monitor.cjs');
const { Websites, identifier: websiteId, readSite, checkHttp } = require('./lib/websites.cjs');
const backupCrypto = require('./lib/backup-crypto.cjs');
const backupTransfer = require('./lib/backup-transfer.cjs');
const { UserError } = require('./lib/contracts.cjs');
const { LocalDeployment, LOCAL_ID, localClient } = require('./lib/local.cjs');
const { LocalUpdates } = require('./lib/local-updates.cjs');
const { AppUpdater } = require('./lib/app-updater.cjs');
const { updateTransport } = require('./lib/update-transport.cjs');
const { diagnostics } = require('./lib/diagnostics.cjs');

protocol.registerSchemesAsPrivileged([{ scheme: 'gaoji', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
let window;
let store;
let local;
let localUpdates;
const keySelections = new Map();
const clients = new Map();
const installations = new Set();
const consoles = new Map();
const backupSelections = new Map();
const transfers = new Map();
const writes = new Set();
const mutating = new Set(['connect', 'forget', 'local-start', 'local-docker', 'install', 'update',
  'update-resume', 'lifecycle-prepare', 'lifecycle-submit', 'backup-export', 'backup-import', 'application-create', 'application-rename', 'application-remove-draft', 'website-submit', 'website-resume', 'website-backup-export', 'website-backup-import']);
const instanceWrites = new Set(['save', 'operation', 'qqRefresh', 'storageCleanup', 'storagePolicy']);

const ownsLock = app.requestSingleInstanceLock();
if (!ownsLock) app.quit();
app.on('second-instance', () => { window?.show(); window?.focus(); });
app.on('before-quit', () => { for (const value of transfers.values()) value.controller.abort(); for (const client of clients.values()) client.end(); });
app.on('window-all-closed', () => app.quit());
app.on('activate', () => window?.show());

function ipc(name, handler) {
  ipcMain.handle(`gaoji:${name}`, async (event, ...args) => {
    if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame
        || !event.senderFrame.url.startsWith('gaoji://app/')) {
      return { ok: false, error: { code: 'untrusted_window', message: '此页面无权操作服务器' } };
    }
    const token = {};
    if (mutating.has(name) || name === 'instance' && instanceWrites.has(args[0]?.action)) writes.add(token);
    try { return { ok: true, data: await handler(...args) }; }
    catch (error) {
      return { ok: false, error: { code: error instanceof UserError ? error.code : 'operation_failed',
        message: error instanceof UserError ? error.message : '操作没有完成，请重试；现有服务器数据不会自动删除' } };
    } finally { writes.delete(token); }
  });
}

function authentication(input) {
  if (input.type === 'private-key') {
    const key = keySelections.get(input.keySelection);
    if (!key) throw new UserError('missing_key', '请重新选择私钥文件');
    return { type: input.type, privateKeyBase64: key.toString('base64'), passphrase: input.passphrase || '' };
  }
  if (input.type === 'password') return { type: input.type, password: input.password };
  if (input.type === 'agent') {
    const agent = process.platform === 'win32' ? '\\\\.\\pipe\\openssh-ssh-agent' : process.env.SSH_AUTH_SOCK;
    if (!agent) throw new UserError('missing_agent', '此电脑没有运行系统 SSH 密钥代理，请选择密码或密钥文件');
    return { type: input.type, agent };
  }
  if (input.type === 'tailscale') return { type: input.type };
  throw new UserError('missing_auth', '请选择登录方式');
}

function sshAuthentication(value) {
  return value.type === 'private-key' ? { ...value, privateKey: Buffer.from(value.privateKeyBase64, 'base64') } : value;
}

async function clientFor(id) {
  id = store.connectionId(id);
  if (clients.has(id)) return clients.get(id);
  const server = store.get(id);
  const client = await ssh.connect(server, sshAuthentication(store.credentials(id)), server.fingerprint);
  clients.set(id, client);
  client.once('close', () => { if (clients.get(id) === client) clients.delete(id); });
  return client;
}

function requireManagedService(server) {
  const value = server.lifecycle;
  if (value && (value.service_state !== 'installed' || value.dispatched && !value.settled)) {
    throw new UserError('service_maintenance', '此实例处于服务维护或保留数据状态，请在服务维护中核对或重新启用');
  }
}

function instanceTarget(id) {
  return id === LOCAL_ID ? { id, name: '本机高级', deployment: local.read() } : botTarget(id);
}

function botTarget(id) {
  const value = store.instance(id);
  if (value.appId && value.appId !== 'gaoji') throw new UserError('wrong_application', '此操作只适用于高级机器人实例');
  return value;
}

async function instanceConnection(server) {
  if (server.id === LOCAL_ID) return { client: localClient(), access: local.access(server.deployment) };
  const client = await clientFor(server.id);
  return { client, access: await control.accessFor(client, server.deployment.job) };
}

function rememberLifecycle(id, value) {
  const record = botTarget(id).lifecycle;
  if (!record || !value || value.id !== record.id) return value;
  // A status poll started before dispatch must not erase the durable dispatch flag.
  if (record.dispatched && value.status === 'preview') value = { ...value, status: 'unconfirmed', resumable: true };
  const next = { ...record, service_state: value.service_state,
    settled: value.status === 'completed' || value.status === 'failed' && !value.resumable };
  store.setLifecycle(id, next);
  return { ...value, dispatched: next.dispatched };
}

async function withTransfer(server, name, operation) {
  requireManagedService(server);
  if (installations.has(server.id) || server.id === LOCAL_ID && (local.running || local.starting)) throw new UserError('operation_busy', '此实例正在提交或传输，请等待完成');
  if (!server.deployment) throw new UserError('not_installed', '请先完成服务器安装');
  const state = { controller: new AbortController(), status: 'running', phase: 'preparing', bytes: 0, total: 0, name, message: '正在准备备份传输' };
  transfers.set(server.id, state); installations.add(server.id);
  try {
    const result = await operation(state.controller.signal, (phase, bytes, total) => {
      Object.assign(state, { phase, bytes, total, message: phase === 'verifying' ? '正在核对密码和备份完整性' : phase === 'uploading' ? '正在上传，尚未执行恢复' : '正在下载并加密' });
    });
    Object.assign(state, { status: 'completed', message: result.imported ? '备份已导入；尚未覆盖当前实例，请单独选择恢复' : '加密备份已保存到电脑' });
    return result;
  } catch (error) {
    Object.assign(state, { status: 'failed', message: error instanceof UserError ? error.message : '备份传输未完成，未执行恢复' });
    throw error;
  } finally { installations.delete(server.id); }
}

if (ownsLock) app.whenReady().then(async () => {
  const distribution = path.join(__dirname, 'dist');
  const deploymentDirectory = app.isPackaged ? path.join(process.resourcesPath, 'deployment') : path.resolve(__dirname, '../packaging/build/deployment');
  protocol.handle('gaoji', request => {
    const url = new URL(request.url);
    if (url.hostname !== 'app' || request.method !== 'GET') return new Response('Forbidden', { status: 403 });
    let pathname;
    try { pathname = decodeURIComponent(url.pathname); } catch { return new Response('Bad path', { status: 400 }); }
    const filename = path.resolve(distribution, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (filename !== distribution && !filename.startsWith(distribution + path.sep)) return new Response('Forbidden', { status: 403 });
    return net.fetch(pathToFileURL(filename).toString());
  });
  store = new Connections(path.join(app.getPath('userData'), 'connections'), safeStorage);
  const websites = new Websites(path.join(app.getPath('userData'), 'websites'), deploymentDirectory, store, clientFor);
  const applicationMonitor = new ApplicationMonitor(store, websites, clientFor);
  local = new LocalDeployment(path.join(app.getPath('userData'), 'local'), deploymentDirectory, safeStorage);
  localUpdates = new LocalUpdates(local);
  const appUpdater = new AppUpdater({ version: app.getVersion(), platform: process.platform, arch: process.arch,
    cache: path.join(app.getPath('userData'), 'app-updates'), fetch: updateTransport(net),
    native: process.platform === 'win32' ? require('electron-updater').autoUpdater : null,
    packaged: app.isPackaged, openPath: filename => shell.openPath(filename),
    canInstall: () => !writes.size && !installations.size && !local.running && !local.starting && !localUpdates.running && !localUpdates.starting });
  void appUpdater.cleanCache().catch(() => {});
  ipc('app-update-status', () => appUpdater.status());
  ipc('app-update-check', () => appUpdater.check());
  ipc('app-update-download', () => appUpdater.download());
  ipc('app-update-install', async () => {
    if (process.platform === 'win32') {
      const result = await dialog.showMessageBox(window, { type: 'question', title: '重启并更新 App',
        message: '确定重启高级并安装更新？', detail: '尚未保存的表单修改会丢失。服务器上的机器人不受影响。',
        buttons: ['取消', '重启并更新'], defaultId: 0, cancelId: 0, noLink: true });
      if (result.response !== 1) return appUpdater.status();
    }
    return appUpdater.install();
  });
  ipc('local-inspect', () => local.inspect());
  ipc('local-start', options => {
    if (installations.has(LOCAL_ID)) throw new UserError('operation_busy', '本机备份正在传输，请等待完成');
    return local.start(options);
  });
  ipc('local-status', () => local.status());
  ipc('local-docker', async action => {
    if (action === 'download') {
      await shell.openExternal(process.platform === 'win32' ? 'https://docs.docker.com/desktop/setup/install/windows-install/'
        : 'https://docs.docker.com/desktop/setup/install/mac-install/');
    } else if (action === 'open') {
      const filename = process.platform === 'win32'
        ? path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Docker', 'Docker', 'Docker Desktop.exe') : '/Applications/Docker.app';
      const error = await shell.openPath(filename);
      if (error) throw new UserError('docker_missing', 'Docker Desktop 尚未安装，请先下载安装');
    } else throw new UserError('invalid_operation', '不支持此操作');
    return true;
  });
  ipc('info', () => {
    let runtimeAvailable = false;
    try { deploy.bundle(deploymentDirectory); runtimeAvailable = true; } catch { /* Report missing release resources without exposing file paths. */ }
    return { version: app.getVersion(), platform: process.platform, secureStorage: store.canRemember(), runtimeAvailable };
  });
  ipc('servers', () => store.list());
  ipc('application-catalog', () => applications.catalog());
  ipc('applications', id => store.applications(id));
  ipc('application-status', id => applicationMonitor.status(id));
  ipc('application-rename', request => {
    if (!request || Object.keys(request).sort().join() !== 'id,name') throw new UserError('invalid_application', '无效实例名称');
    return store.renameApplication(request.id, request.name);
  });
  ipc('application-remove-draft', request => {
    if (!request || Object.keys(request).sort().join() !== 'confirm,id' || request.confirm !== true) throw new UserError('confirmation_required', '请确认移除未部署记录');
    if (installations.has(request.id) || websites.busy.has(request.id)) throw new UserError('operation_busy', '此实例正在提交任务，不能移除');
    return store.removeDraft(request.id);
  });
  ipc('application-create', async request => {
    if (!request || Object.keys(request).sort().join() !== 'appId,id,name') throw new UserError('invalid_application', '无效软件实例');
    const inspection = await ssh.inspectServer(await clientFor(request.id));
    if (!applications.supported(request.appId, inspection.probe)) throw new UserError('unsupported_platform', '此软件暂不支持该服务器系统');
    return store.createApplication(request.id, request.appId, request.name);
  });
  ipc('website-select', async () => {
    const result = await dialog.showOpenDialog(window, { title: '选择构建好的网页文件夹', properties: ['openDirectory'] });
    return result.canceled ? null : websites.select(result.filePaths[0]);
  });
  ipc('website-status', id => websites.status(id));
  ipc('website-submit', request => websites.submit(request));
  ipc('website-resume', request => websites.resume(request));
  ipc('website-backup-export', async request => {
    if (!request || Object.keys(request).some(key => !['id', 'backupId', 'password', 'sudoPassword'].includes(key))) throw new UserError('invalid_backup', '无效网站备份请求');
    websiteId(store.instance(request.id));
    const result = await dialog.showSaveDialog(window, { title: '导出加密的网站备份', defaultPath: 'website-backup.gaojibak', filters: [{ name: '高级加密备份', extensions: ['gaojibak'] }] });
    if (result.canceled || !result.filePath) return { cancelled: true };
    return websites.exportBackup(request.id, request.backupId, request.password, request.sudoPassword, result.filePath);
  });
  ipc('website-backup-import', async request => {
    if (!request || Object.keys(request).sort().join() !== 'password') throw new UserError('invalid_backup', '无效网站备份请求');
    const result = await dialog.showOpenDialog(window, { title: '导入加密的网站备份', properties: ['openFile'], filters: [{ name: '高级加密备份', extensions: ['gaojibak'] }] });
    return result.canceled ? null : websites.importBackup(result.filePaths[0], request.password);
  });
  ipc('website-open', async id => {
    const instance = store.instance(id);
    const client = await clientFor(id);
    const site = await readSite(client, websiteId(instance));
    if (site?.status !== 'running' || !site.current || !await checkHttp(client, site.current)) throw new UserError('site_unavailable', '网站尚未通过实际访问检查');
    consoles.get(id)?.close();
    const tunnel = await control.consoleTunnel(client, site.current.port);
    const url = new URL('/', tunnel.url).href;
    const origin = new URL(url).origin;
    const preview = new BrowserWindow({ width: 1180, height: 800, title: instance.name,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, partition: 'gaoji-site-' + websiteId(instance) } });
    preview.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    preview.webContents.on('will-navigate', (event, target) => { if (new URL(target).origin !== origin) event.preventDefault(); });
    preview.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    consoles.set(id, preview);
    preview.once('closed', () => { if (consoles.get(id) === preview) consoles.delete(id); tunnel.close(); });
    try { await preview.loadURL(url); } catch { preview.close(); throw new UserError('site_unavailable', '网站暂时不可用'); }
    return true;
  });
  ipc('diagnostics-export', async input => {
    const report = diagnostics(app.getVersion(), input);
    const result = await dialog.showSaveDialog(window, { title: '导出诊断报告',
      defaultPath: `gaoji-diagnostics-${new Date().toISOString().slice(0, 10)}.json`,
      filters: [{ name: 'JSON', extensions: ['json'] }] });
    if (result.canceled || !result.filePath) return { saved: false };
    await fs.promises.writeFile(result.filePath, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
    return { saved: true };
  });
  ipc('discover', target => ssh.discoverHost(target));
  ipc('select-key', async () => {
    const result = await dialog.showOpenDialog(window, { title: '选择 SSH 私钥', properties: ['openFile'] });
    if (result.canceled) return null;
    const filename = result.filePaths[0];
    if (fs.statSync(filename).size > 128 * 1024) throw new UserError('invalid_key', '选择的文件过大，不像 SSH 私钥');
    const selection = randomUUID();
    keySelections.clear();
    keySelections.set(selection, fs.readFileSync(filename));
    return { selection, name: path.basename(filename) };
  });
  ipc('connect', async request => {
    const credentials = authentication(request.authentication);
    const client = await ssh.connect(request, sshAuthentication(credentials), request.fingerprint);
    try {
      const result = await ssh.inspectServer(client);
      const server = store.save(request, credentials);
      clients.get(server.id)?.end();
      clients.set(server.id, client);
      client.once('close', () => { if (clients.get(server.id) === client) clients.delete(server.id); });
      return { server, ...result };
    } catch (error) { client.end(); throw error; }
  });
  ipc('inspect', async id => ssh.inspectServer(await clientFor(id)));
  ipc('install-status', async id => {
    const record = botTarget(id).deployment;
    return record ? deploy.status(await clientFor(id), record.job) : null;
  });
  ipc('install', async request => {
    const server = botTarget(request.id);
    requireManagedService(server);
    if (installations.has(server.id)) throw new UserError('install_busy', '此服务器正在提交安装，请勿重复点击');
    installations.add(server.id);
    try {
      const client = await clientFor(server.id);
      const onPrepared = value => store.setDeployment(server.id, value);
      return server.deployment ? await deploy.retry(client, deploymentDirectory, server.deployment, request.password, onPrepared, request.imageOptions)
        : await deploy.start(client, deploymentDirectory, { password: request.password, onPrepared, imageOptions: request.imageOptions });
    } finally { installations.delete(server.id); }
  });
  ipc('instance', async request => {
    if (request.id === LOCAL_ID) {
      if (request.action === 'console') throw new UserError('invalid_operation', '请使用打开控制台按钮');
      return local.request(request.action, request.payload || {});
    }
    const server = botTarget(request.id);
    requireManagedService(server);
    if (!server.deployment) throw new UserError('not_installed', '请先完成服务器安装');
    if (request.action === 'console' || !Object.hasOwn(control.ROUTES, request.action)) throw new UserError('invalid_operation', '不支持此操作');
    const client = await clientFor(server.id);
    return control.request(client, await control.accessFor(client, server.deployment.job), request.action, request.payload || {});
  });
  ipc('update-plan', async id => {
    if (id === LOCAL_ID) return localUpdates.plan();
    const server = botTarget(id);
    if (!server.deployment) throw new UserError('not_installed', '请先完成服务器安装');
    return updates.plan(await clientFor(id), deploymentDirectory, server.deployment);
  });
  ipc('update-status', async id => {
    if (id === LOCAL_ID) return localUpdates.status();
    const server = botTarget(id);
    return server.update ? updates.status(await clientFor(id), server.update) : null;
  });
  ipc('update', async request => {
    if (request?.id === LOCAL_ID) {
      if (installations.has(LOCAL_ID)) throw new UserError('operation_busy', '本机正在传输或修改，请稍后更新');
      return localUpdates.start(request);
    }
    const server = botTarget(request.id);
    requireManagedService(server);
    if (!server.deployment) throw new UserError('not_installed', '请先完成服务器安装');
    if (installations.has(server.id)) throw new UserError('install_busy', '此实例正在提交操作，请稍后再试');
    installations.add(server.id);
    try {
      const client = await clientFor(server.id);
      return await updates.start(client, deploymentDirectory, server, request, value => store.setUpdate(server.id, value));
    } finally { installations.delete(server.id); }
  });
  ipc('update-resume', async request => {
    if (!request || Object.keys(request).some(key => !['id', 'password'].includes(key))) throw new UserError('invalid_update', '无效更新操作');
    if (request.id === LOCAL_ID) return localUpdates.resume();
    const server = botTarget(request.id);
    requireManagedService(server);
    if (!server.update) throw new UserError('missing_update', '没有待继续的更新任务');
    if (installations.has(server.id)) throw new UserError('install_busy', '此实例正在提交操作，请稍后再试');
    installations.add(server.id);
    try {
      return await updates.resume(await clientFor(server.id), deploymentDirectory, server.update, request.password, value => store.setUpdate(server.id, value));
    } finally { installations.delete(server.id); }
  });
  ipc('lifecycle-status', async id => {
    const record = botTarget(id).lifecycle;
    return record ? rememberLifecycle(id, await lifecycle.status(await clientFor(id), record)) : null;
  });
  for (const action of ['prepare', 'submit']) ipc('lifecycle-' + action, async request => {
    if (!request || typeof request.id !== 'string') throw new UserError('invalid_lifecycle', '请选择服务器实例');
    const server = botTarget(request.id);
    if (!server.deployment) throw new UserError('not_installed', '请先完成服务器安装');
    if (installations.has(server.id)) throw new UserError('operation_busy', '此实例正在提交或传输，请等待完成');
    installations.add(server.id);
    try {
      const result = await lifecycle[action](await clientFor(server.id), server, request, value => store.setLifecycle(server.id, value));
      return rememberLifecycle(server.id, result);
    } finally { installations.delete(server.id); }
  });
  ipc('backup-select', async () => {
    const result = await dialog.showOpenDialog(window, { title: '选择加密备份', properties: ['openFile'], filters: [{ name: '高级加密备份', extensions: ['gaojibak'] }] });
    if (result.canceled) return null;
    const filename = result.filePaths[0];
    const stat = fs.lstatSync(filename);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > backupCrypto.MAX_BYTES + 8192) throw new UserError('invalid_backup', '选择的文件不是支持的加密备份');
    const selection = randomUUID(); backupSelections.clear(); backupSelections.set(selection, filename);
    return { selection, name: path.basename(filename), bytes: stat.size };
  });
  ipc('backup-export', async request => {
    if (!request || Object.keys(request).sort().join() !== 'backupId,id,password') throw new UserError('invalid_backup', '无效备份请求');
    const server = instanceTarget(request.id);
    deploy.jobId(request.backupId); backupCrypto.validatePassword(request.password);
    const result = await dialog.showSaveDialog(window, { title: '保存加密备份', defaultPath: 'gaoji-' + request.backupId.slice(0, 12) + '.gaojibak',
      filters: [{ name: '高级加密备份', extensions: ['gaojibak'] }] });
    if (result.canceled || !result.filePath) return { cancelled: true };
    return withTransfer(server, path.basename(result.filePath), async (signal, progress) => {
      const { client, access } = await instanceConnection(server);
      return backupTransfer.exportBackup(client, access, request.backupId, request.password, result.filePath,
        { signal, progress: (bytes, total) => progress('downloading', bytes, total) });
    });
  });
  ipc('backup-import', async request => {
    if (!request || Object.keys(request).sort().join() !== 'id,password,selection') throw new UserError('invalid_backup', '无效备份请求');
    const server = instanceTarget(request.id);
    const filename = backupSelections.get(request.selection);
    if (!filename) throw new UserError('missing_backup', '请重新选择加密备份文件');
    backupCrypto.validatePassword(request.password);
    return withTransfer(server, path.basename(filename), async (signal, progress) => {
      const { client, access } = await instanceConnection(server);
      const result = await backupTransfer.importBackup(client, access, filename, request.password, { signal, progress });
      backupSelections.delete(request.selection);
      return result;
    });
  });
  ipc('backup-transfer-status', id => {
    instanceTarget(id);
    const value = transfers.get(id);
    if (!value) return null;
    const { controller: _controller, ...result } = value;
    return result;
  });
  ipc('backup-transfer-cancel', id => { instanceTarget(id); const value = transfers.get(id); if (value?.status === 'running') value.controller.abort(); return true; });
  ipc('open-console', async id => {
    const server = instanceTarget(id);
    requireManagedService(server);
    if (consoles.has(id)) { consoles.get(id).show(); return true; }
    if (!server.deployment) throw new UserError('not_installed', '请先完成服务器安装');
    const { client, access } = await instanceConnection(server);
    const result = await control.request(client, access, 'console');
    const tunnel = await control.consoleTunnel(client, result.port);
    const origin = new URL(tunnel.url).origin;
    const consoleWindow = new BrowserWindow({ width: 1300, height: 850, title: server.name + ' · 控制台',
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true, partition: 'persist:gaoji-console-' + id } });
    consoleWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    consoleWindow.webContents.on('will-navigate', (event, url) => { if (new URL(url).origin !== origin) event.preventDefault(); });
    consoleWindow.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    consoles.set(id, consoleWindow);
    consoleWindow.once('closed', () => { consoles.delete(id); tunnel.close(); });
    try { await consoleWindow.loadURL(tunnel.url); }
    catch { consoleWindow.close(); throw new UserError('console_unavailable', '控制台尚未启动，请先启用机器人'); }
    return true;
  });
  ipc('forget', id => {
    for (const ref of [id, ...store.applications(id).map(item => item.id)]) {
      transfers.get(ref)?.controller.abort(); consoles.get(ref)?.close();
    }
    clients.get(id)?.end(); clients.delete(id); store.forget(id); return store.list();
  });

  window = new BrowserWindow({ width: 1180, height: 800, minWidth: 760, minHeight: 600, title: '高级 · 部署与管理',
    backgroundColor: '#f5f5f6', webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
      nodeIntegration: false, sandbox: true, webSecurity: true } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => { if (!url.startsWith('gaoji://app/')) event.preventDefault(); });
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  Menu.setApplicationMenu(Menu.buildFromTemplate([{ label: 'Gaoji', submenu: [
    { label: '显示高级', click: () => window.show() },
    { label: '检查 App 更新…', click: () => { window.show(); window.focus(); window.webContents.send('gaoji:show-app-update'); } },
    { type: 'separator' }, { role: 'quit', label: '退出客户端' },
  ] }, { role: 'editMenu', label: '编辑' }, { role: 'viewMenu', label: '视图' }]));
  await window.loadURL('gaoji://app/');
}).catch(() => { dialog.showErrorBox('高级启动失败', '请保留用户数据目录并重新安装客户端。'); app.quit(); });
