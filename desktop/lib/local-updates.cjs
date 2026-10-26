'use strict';

const { randomUUID } = require('node:crypto');
const deploy = require('./deploy.cjs');
const { validateRecord } = require('./updates.cjs');
const { UserError } = require('./contracts.cjs');

class LocalUpdates {
  constructor(local) { this.local = local; this.running = null; }
  record() {
    const record = this.local.read();
    if (!record) throw new UserError('not_installed', '请先安装本机环境');
    return record;
  }
  persist(update, progress) {
    const record = this.record();
    if (record.update && record.update.id !== update.id) throw new UserError('update_conflict', '本机更新任务已变化');
    this.local.write({ ...record, update, updateProgress: progress });
  }
  async ready() {
    const inspection = await this.local.inspect();
    if (!inspection.ready) throw new UserError('local_unavailable', inspection.blockers.join('；'));
    const record = this.record();
    if (!await this.local.owned('volume', `gaoji-desktop-${record.job}-data`, record)
        || !await this.local.owned('network', `gaoji-desktop-${record.job}`, record)) {
      throw new UserError('local_data_missing', '原本机数据卷或网络不存在，未创建替代实例');
    }
    return record;
  }
  async plan() {
    const record = this.record();
    const current = await this.local.request('versions');
    if (!/^[a-f0-9]{64}$/.test(current.checksum) || !Number.isSafeInteger(current.revision) || !Array.isArray(current.rollbacks)) {
      throw new UserError('invalid_update', '本机版本信息无效');
    }
    const bundle = deploy.bundle(this.local.bundleDirectory);
    const available = { version: bundle.version, checksum: bundle.files['gaoji-runtime.tar.gz'].sha256 };
    // A successful worker may have finished while Electron was closed.
    if (record.update) await this.status();
    return { current, available, rollbacks: current.rollbacks, canUpdate: current.checksum !== available.checksum };
  }
  async worker(record, update) {
    const name = `gaoji-desktop-${record.job}-update-${update.id}`;
    const row = await this.local.owned('container', name, record);
    if (!row) return null;
    const mounts = row.Mounts || [];
    const volume = mounts.find(m => m.Destination === '/var/lib/gaoji-app');
    const socket = mounts.find(m => m.Destination === '/var/run/docker.sock');
    if (row.Config.Image !== `gaoji-desktop-controller:${update.runnerChecksum}`
        || JSON.stringify(row.Config.Cmd) !== JSON.stringify(['update', update.id])
        || row.Config.Labels?.['gaoji.desktop.update'] !== update.id
        || volume?.Name !== `gaoji-desktop-${record.job}-data` || volume?.Type !== 'volume' || !volume?.RW
        || mounts.length !== 2 || socket?.Type !== 'bind' || socket.Source !== '/var/run/docker.sock' || !socket.RW
        || row.HostConfig?.Privileged || row.HostConfig?.NetworkMode !== `gaoji-desktop-${record.job}`) {
      throw new UserError('update_ownership', '本机更新执行器与任务记录不符，未操作');
    }
    const image = JSON.parse(await this.local.checked(['image', 'inspect', row.Image]))[0];
    if (image.Config.Labels?.['gaoji.desktop.runtime-digest'] !== update.runnerChecksum) throw new UserError('update_ownership', '更新镜像身份不符');
    return row;
  }
  async status() {
    const record = this.record();
    if (!record.update) return null;
    const update = validateRecord(record.update);
    if (this.running) return record.updateProgress;
    const worker = await this.worker(record, update);
    if (!worker) return { ...record.updateProgress, id: update.id, job: update.job,
      target_checksum: update.target_checksum, status: 'unconfirmed', finished: false, resumable: true,
      stage: 'prepare', message: '本机更新准备尚未完成，可继续原任务；当前机器人未被替换' };
    const raw = await this.local.checked(['logs', '--tail', '1', worker.Id]);
    if (!raw.trim()) return { id: update.id, status: worker.State.Running ? 'queued' : 'interrupted', stage: 'dispatch',
      message: worker.State.Running ? '后台已接收原更新任务' : '更新执行器已停止，请继续原任务', finished: false };
    let value;
    try { value = JSON.parse(raw.trim()); } catch { throw new UserError('update_status', '更新进度暂不可读，请稍后查询'); }
    if (value.id !== update.id || value.job !== record.job || value.target_checksum !== update.target_checksum
        || !['queued', 'running', 'failed', 'completed'].includes(value.status)) throw new UserError('update_status', '更新回执与任务不符');
    if (value.finished === true || value.status === 'completed') {
      if (!/^[a-f0-9]{64}$/.test(value.current_checksum) || !/^[a-f0-9]{64}$/.test(value.controller_checksum)) throw new UserError('update_status', '更新回执缺少有效版本');
      const latest = this.record();
      if (latest.update?.id === update.id) this.local.write({ ...latest, checksum: value.current_checksum,
        controllerChecksum: value.controller_checksum, updateProgress: value, runtimeVerified: true });
    }
    if (!worker.State.Running && !value.finished && value.status !== 'completed') return { ...value, status: 'interrupted',
      resumable: true, message: '后台执行器已停止，继续时沿用原任务和恢复记录' };
    return value;
  }
  async start(options) {
    if (this.starting) throw new UserError('update_busy', '正在提交原更新请求，请勿重复点击');
    this.starting = true;
    try { return await this.prepareStart(options); }
    finally { this.starting = false; }
  }
  async prepareStart(options) {
    if (!options || options.confirm !== true || !['update', 'rollback'].includes(options.kind)
        || Object.keys(options).some(k => !['id','kind','confirm','revision','expectedChecksum','rollbackId','password'].includes(k))) {
      throw new UserError('invalid_update', '请确认版本操作');
    }
    if (this.running || this.local.running || this.local.starting) throw new UserError('update_busy', '本机实例正在处理操作');
    const record = await this.ready();
    const previous = await this.status();
    if (previous && previous.finished !== true && previous.status !== 'completed') throw new UserError('update_busy', '请先继续原更新任务');
    const plan = await this.plan();
    if (plan.current.checksum !== options.expectedChecksum || plan.current.revision !== options.revision
        || plan.current.maintenance_pending || ['queued', 'running'].includes(plan.current.operation?.status)) {
      throw new UserError('update_conflict', '版本、配置或维护状态已变化，请重新查询');
    }
    const point = options.kind === 'rollback' ? plan.rollbacks.find(p => p.id === options.rollbackId) : null;
    if (options.kind === 'rollback' ? !point : !plan.canUpdate) throw new UserError('invalid_update', '此版本不可用于更新');
    if (record.update) {
      const old = await this.worker(record, validateRecord(record.update));
      if (old) { await this.local.checked(['stop', old.Id]); await this.local.checked(['rm', old.Id]); }
    }
    const update = validateRecord({ id: randomUUID().replaceAll('-', ''), job: record.job, kind: options.kind,
      expected_checksum: plan.current.checksum, target_checksum: point?.target_checksum || plan.available.checksum,
      revision: plan.current.revision, runnerChecksum: plan.available.checksum,
      ...(point ? { rollback_id: point.id, confirm_data_replacement: true } : {}) });
    this.local.write({ ...this.record(), update, updateProgress: null });
    return this.launch(update);
  }
  async resume() {
    if (this.starting) throw new UserError('update_busy', '正在提交原更新请求，请勿重复点击');
    this.starting = true;
    try { return await this.prepareResume(); }
    finally { this.starting = false; }
  }
  async prepareResume() {
    if (this.running) return this.record().updateProgress;
    const record = await this.ready();
    if (!record.update) throw new UserError('missing_update', '没有待继续的本机更新');
    const update = validateRecord(record.update);
    const current = await this.status();
    if (current.finished === true || ['completed','queued','running'].includes(current.status)) return current;
    const worker = await this.worker(record, update);
    if (worker) {
      await this.local.checked([worker.State.Running ? 'restart' : 'start', worker.Id]);
      return { ...current, status: 'queued', message: '已请求后台继续原任务' };
    }
    return this.launch(update);
  }
  launch(update) {
    const progress = (stage, message) => this.persist(update, { id: update.id, job: update.job,
      target_checksum: update.target_checksum, status: 'running', stage, message, finished: false, updated_at: Math.floor(Date.now()/1000) });
    progress('prepare', '正在准备更新组件，当前机器人继续运行');
    this.running = this.prepare(update, progress).catch(error => {
      this.persist(update, { id: update.id, status: 'failed', stage: 'prepare', finished: false, resumable: true,
        message: error instanceof UserError ? error.message : '本机更新尚未完成，请继续原任务；数据保留' });
    }).finally(() => { this.running = null; });
    return this.record().updateProgress;
  }
  async prepare(update, progress) {
    const record = await this.ready();
    const manifest = deploy.bundle(this.local.bundleDirectory);
    if (manifest.files['gaoji-runtime.tar.gz'].sha256 !== update.runnerChecksum) throw new UserError('update_bundle', '原更新使用的 App 运行包已变化，请使用原安装包继续');
    await this.local.prepareImages({ ...record, checksum: update.runnerChecksum, runtimeVerified: false }, progress);
    const mounts = ['--mount', `type=volume,source=gaoji-desktop-${record.job}-data,target=/var/lib/gaoji-app`,
      '--mount', 'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock'];
    const image = `gaoji-desktop-controller:${update.runnerChecksum}`;
    const { runnerChecksum, ...request } = update;
    await this.local.checked(['run','--rm','-i','--network','none',...mounts,image,'prepare-update'], {
      input: JSON.stringify({ request, runner_checksum: runnerChecksum, port: record.port }) });
    progress('dispatch', '正在把任务交给独立后台，关闭 App 不会取消更新');
    let worker = await this.worker(record, update);
    if (!worker) {
      await this.local.checked(['create','--name',`gaoji-desktop-${record.job}-update-${update.id}`,
        '--label',`gaoji.desktop.job=${record.job}`,'--label',`gaoji.desktop.update=${update.id}`,
        '--restart','unless-stopped','--network',`gaoji-desktop-${record.job}`,'--memory','512m','--cpus','1',
        '--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2',...mounts,image,'update',update.id]);
      worker = await this.worker(record, update);
    }
    if (!worker.State.Running) await this.local.checked(['start', worker.Id]);
  }
}

module.exports = { LocalUpdates };
