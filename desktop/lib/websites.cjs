'use strict';

const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { randomUUID, createHash } = require('node:crypto');
const { UserError } = require('./contracts.cjs');
const deploy = require('./deploy.cjs');
const control = require('./control.cjs');
const ssh = require('./ssh.cjs');
const content = require('./website-content.cjs');
const { siteRequest } = require('./applications.cjs');
const backups = require('./website-backups.cjs');

function identifier(instance) {
  if (instance.appId !== 'static-site' || !/^app:[a-f0-9-]{36}$/.test(instance.id)) throw new UserError('wrong_application', '请选择静态网站实例');
  return deploy.jobId(instance.id.slice(4).replaceAll('-', ''));
}

async function readSite(client, id) {
  const result = await ssh.execute(client, `if test -e /var/lib/gaoji-app/status/applications/${deploy.jobId(id)}.json; then cat /var/lib/gaoji-app/status/applications/${id}.json; else printf 'null'; fi`, { maxBytes: 64 * 1024 });
  if (result.code !== 0) throw new UserError('site_unavailable', '网站状态暂时不可读取，不能确定是否已完成');
  let value;
  try { value = JSON.parse(result.stdout); } catch { throw new UserError('invalid_status', '网站状态暂时不可读取'); }
  if (value === null) return null;
  if (value.schema !== 1 || value.id !== id || value.app_id !== 'static-site' || !Number.isSafeInteger(value.revision)
      || !['not_installed', 'running', 'stopped', 'retained', 'removed'].includes(value.status)
      || value.current && (!control.validPort(value.current.port) || !/^[a-f0-9]{64}$/.test(value.current.checksum))) throw new UserError('invalid_status', '网站状态校验失败');
  return value;
}

async function checkHttp(client, current) {
  const stream = await control.forward(client, current.port);
  return new Promise((resolve, reject) => {
    const agent = new http.Agent({ keepAlive: false });
    agent.createConnection = () => stream;
    const request = http.get({ hostname: '127.0.0.1', port: current.port, path: '/index.html', agent, headers: { 'Accept-Encoding': 'identity' } }, response => {
      const digest = createHash('sha256'); let bytes = 0;
      response.on('data', data => {
        bytes += data.length;
        if (bytes > content.MAX_FILE) request.destroy(new Error('Oversized response'));
        else digest.update(data);
      });
      response.once('error', reject);
      response.once('end', () => resolve(response.statusCode === 200 && digest.digest('hex') === current.index_sha256));
    });
    const timer = setTimeout(() => request.destroy(new Error('Website request timed out')), 5000);
    request.once('error', reject);
    request.once('close', () => { clearTimeout(timer); agent.destroy(); stream.destroy(); });
  });
}

class Websites {
  constructor(directory, runtime, store, clientFor) {
    this.directory = directory; this.runtime = runtime; this.store = store; this.clientFor = clientFor;
    this.selections = new Map(); this.busy = new Set(); this.pendingImports = 0;
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.cleanup();
  }

  cleanup() {
    // Import authenticates and stages asynchronously; a simultaneous status poll
    // must not collect its archive before the selection is registered.
    if (this.pendingImports) return;
    const keep = new Set([...this.selections.values()].map(item => item.checksum));
    for (const server of this.store.list()) for (const item of this.store.applications(server.id)) {
      if (!item.deployment?.settled && item.deployment?.application?.content_sha256) keep.add(item.deployment.application.content_sha256);
    }
    let names;
    try { names = fs.readdirSync(this.directory); } catch { return; }
    for (const name of names) {
      if (/^\.site-import-[a-f0-9-]{36}$/.test(name)) {
        const partial = path.join(this.directory, name);
        try {
          const stat = fs.lstatSync(partial);
          if (stat.isFile() && stat.size <= content.MAX_BYTES + 8 * 1024 ** 2) fs.unlinkSync(partial);
        } catch { /* Leave unknown or inaccessible entries untouched. */ }
        continue;
      }
      if (!/^[a-f0-9]{64}\.tar\.gz$/.test(name) || keep.has(name.slice(0, 64))) continue;
      const file = path.join(this.directory, name);
      try {
        const stat = fs.lstatSync(file);
        if (stat.isFile() && stat.size <= content.MAX_BYTES + 8 * 1024 ** 2
            && createHash('sha256').update(fs.readFileSync(file)).digest('hex') === name.slice(0, 64)) fs.unlinkSync(file);
      } catch { /* Cache cleanup must not hide the durable task result. */ }
    }
  }

  async select(folder) {
    const value = await content.snapshot(folder, this.directory);
    const target = path.join(this.directory, value.checksum + '.tar.gz');
    try {
      if (fs.existsSync(target)) {
        if (fs.lstatSync(target).isSymbolicLink() || createHash('sha256').update(fs.readFileSync(target)).digest('hex') !== value.checksum) throw new UserError('invalid_website', '本地网站缓存校验失败');
      } else fs.renameSync(value.archive, target);
      const selection = randomUUID();
      const result = { selection, checksum: value.checksum, name: value.name, files: value.files, bytes: value.bytes };
      this.selections.set(selection, result);
      return result;
    } finally { fs.rmSync(value.temporary, { recursive: true, force: true }); }
  }

  async status(id) {
    const instance = this.store.instance(id);
    const remoteId = identifier(instance);
    const client = await this.clientFor(id);
    const site = await readSite(client, remoteId);
    const task = instance.deployment ? await deploy.status(client, instance.deployment.job) : null;
    const current = this.store.instance(id).deployment;
    // A new submission may finish while this older status request is in flight.
    if (task?.status === 'completed' && current?.job === task.job && !current.settled) {
      this.store.setDeployment(id, { ...current, settled: true });
      this.cleanup();
    }
    let health = site?.status === 'running' ? 'unknown' : 'stopped';
    if (site?.status === 'running' && site.current) {
      try { health = await checkHttp(client, site.current) ? 'ready' : 'unhealthy'; }
      catch { health = 'unknown'; }
    }
    return { site, task, health };
  }

  async importBackup(filename, password) {
    this.pendingImports++;
    try {
      const selected = await backups.importBackup(filename, password, this.directory);
      this.selections.set(selected.selection, selected);
      return selected;
    } finally { this.pendingImports--; this.cleanup(); }
  }

  async exportBackup(id, backupId, password, sudoPassword, filename) {
    const instance = this.store.instance(id);
    const remoteId = identifier(instance);
    if (this.busy.has(id)) throw new UserError('operation_busy', '此网站正在处理任务');
    this.busy.add(id);
    try {
      const client = await this.clientFor(id);
      const site = await readSite(client, remoteId);
      const info = site?.backups?.find(item => item.id === backupId);
      if (!info) throw new UserError('backup_missing', '此网站没有该备份，请刷新');
      const { probe } = await ssh.inspectServer(client);
      return await backups.exportBackup(client, remoteId, info, probe.privilege, sudoPassword, password, filename);
    } finally { this.busy.delete(id); }
  }

  async submit(request) {
    const allowed = ['id', 'operation', 'revision', 'selection', 'backupId', 'confirm', 'eraseInstanceId', 'password'];
    if (!request || Object.keys(request).some(key => !allowed.includes(key)) || request.confirm !== true) throw new UserError('invalid_application', '请先确认网站操作范围');
    const instance = this.store.instance(request.id);
    const remoteId = identifier(instance);
    if (this.busy.has(instance.id)) throw new UserError('operation_busy', '正在提交此网站任务');
    if (request.operation === 'purge' && request.eraseInstanceId !== remoteId) throw new UserError('confirmation_required', '彻底删除需要填写此网站实例编号');
    const selected = this.selections.get(request.selection);
    const application = siteRequest({ schema: 1, app_id: 'static-site', instance_id: remoteId, name: instance.name,
      operation: request.operation, expected_revision: request.revision, content_sha256: selected?.checksum || '',
      ...(['restore', 'delete-backup'].includes(request.operation) ? { backup_id: request.backupId } : {}) });
    this.busy.add(instance.id);
    try {
      const client = await this.clientFor(instance.id);
      if (instance.deployment) {
        const previous = await deploy.status(client, instance.deployment.job);
        const site = await readSite(client, remoteId);
        const rejected = previous.rejected === true && previous.instance_id === remoteId && previous.app_id === 'static-site';
        if (previous.status !== 'completed' && !(previous.status === 'failed' && (rejected || site?.operation?.status === 'failed' && site.operation.safe_to_replace))) throw new UserError('task_pending', '请先查询或继续原任务，不会重复创建实例');
      }
      return await deploy.start(client, this.runtime, { application, password: request.password,
        contentFile: selected ? path.join(this.directory, selected.checksum + '.tar.gz') : undefined,
        onPrepared: record => {
          this.store.setDeployment(instance.id, record);
          if (selected) this.selections.delete(request.selection);
          this.cleanup();
        } });
    } finally { this.busy.delete(instance.id); }
  }

  async resume(request) {
    if (!request || Object.keys(request).some(key => !['id', 'password'].includes(key))) throw new UserError('invalid_application', '无效恢复请求');
    const instance = this.store.instance(request.id); identifier(instance);
    const record = instance.deployment;
    if (!record?.application) throw new UserError('missing_job', '没有待继续的网站任务');
    if (this.busy.has(instance.id)) throw new UserError('operation_busy', '此网站正在提交任务');
    this.busy.add(instance.id);
    try {
      const client = await this.clientFor(instance.id);
      const current = await deploy.status(client, record.job);
      if (current.rejected) throw new UserError('task_rejected', '原操作未执行，请刷新网站状态后重新选择操作');
      if (!['failed', 'unconfirmed'].includes(current.status)) return current;
      if (current.status === 'failed') {
        const inspection = await ssh.inspectServer(client);
        const job = deploy.jobId(record.job);
        const result = await deploy.privileged(client, `set -eu\nstate=$(systemctl show gaoji-app-install-${job}.service --property=ActiveState --value)\ncase "$state" in failed|inactive) ;; *) exit 1;; esac\nsystemctl start --no-block gaoji-app-install-${job}.service`, inspection.probe.privilege, request.password);
        if (result.code !== 0) throw new UserError('retry_failed', '未能继续原任务，请重新查询状态');
        return { ...current, status: 'queued', message: '正在继续原网站任务' };
      }
      const manifest = deploy.bundle(this.runtime);
      if (manifest.files['gaoji-runtime.tar.gz'].sha256 !== record.checksum) throw new UserError('bundle_changed', '原任务使用另一运行包，请先核对服务器任务');
      return await deploy.start(client, this.runtime, { job: record.job, password: request.password, application: record.application,
        contentFile: record.application.content_sha256 ? path.join(this.directory, record.application.content_sha256 + '.tar.gz') : undefined,
        onPrepared: next => this.store.setDeployment(instance.id, next) });
    } finally { this.busy.delete(instance.id); }
  }
}

module.exports = { Websites, identifier, readSite, checkHttp };
