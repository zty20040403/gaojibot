'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { UserError, connectionTarget, hostFingerprint } = require('./contracts.cjs');
const { validateRecord: updateRecord } = require('./updates.cjs');
const { validateRecord: lifecycleRecord } = require('./lifecycle.cjs');
const { application, instanceName, siteRequest } = require('./applications.cjs');

function atomicJson(filename, value) {
  const temporary = path.join(path.dirname(filename), `.${randomUUID()}.tmp`);
  let descriptor;
  try {
    descriptor = fs.openSync(temporary, 'wx', 0o600);
    fs.writeFileSync(descriptor, JSON.stringify(value, null, 2) + '\n');
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor); descriptor = undefined;
    fs.renameSync(temporary, filename);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

class Connections {
  constructor(directory, encryption) {
    this.directory = directory;
    this.encryption = encryption;
    this.sessionSecrets = new Map();
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.filename = path.join(directory, 'servers.json');
    if (fs.existsSync(this.filename) && fs.lstatSync(this.filename).isSymbolicLink()) throw new UserError('invalid_storage', '服务器配置不能是符号链接');
    this.state = fs.existsSync(this.filename) ? JSON.parse(fs.readFileSync(this.filename, 'utf8')) : { version: 1, servers: [] };
    if (this.state.version !== 1 || !Array.isArray(this.state.servers)) throw new UserError('invalid_storage', '服务器配置版本不支持，请保留文件后联系维护者');
  }

  canRemember() {
    return Boolean(this.encryption.isEncryptionAvailable()) && this.encryption.getSelectedStorageBackend?.() !== 'basic_text';
  }

  list() {
    return this.state.servers.map(({ secret, ...server }) => ({ ...server,
      remembered: Boolean(secret), authenticated: Boolean(secret || this.sessionSecrets.has(server.id)) }));
  }

  get(id) {
    const server = this.state.servers.find(item => item.id === id);
    if (!server) throw new UserError('missing_server', '请先添加服务器');
    return { ...server, ...connectionTarget(server), fingerprint: hostFingerprint(server.fingerprint) };
  }

  applications(id) {
    const server = this.get(id);
    // Project the original record in place. Its job, controller and data paths
    // must not move when the desktop learns about multiple applications.
    const legacy = server.deployment ? [{ id: server.id, serverId: server.id, appId: 'gaoji',
      name: server.applicationName || server.name, legacy: true, deployment: server.deployment, update: server.update, lifecycle: server.lifecycle }] : [];
    return structuredClone([...legacy, ...(server.applications || [])]);
  }

  instance(id) {
    if (typeof id !== 'string' || !id.startsWith('app:')) return this.get(id);
    for (const record of this.state.servers) {
      const item = (record.applications || []).find(value => value.id === id);
      if (item) {
        const { deployment: _deployment, update: _update, lifecycle: _lifecycle, applications: _applications, secret: _secret, ...server } = this.get(record.id);
        return { ...server, ...structuredClone(item), serverId: record.id };
      }
    }
    throw new UserError('missing_application', '没有找到此软件实例');
  }

  connectionId(id) { return this.instance(id).serverId || id; }

  createApplication(id, appId, name) {
    this.get(id); application(appId);
    const value = { id: 'app:' + randomUUID(), serverId: id, appId, name: instanceName(name), createdAt: Date.now() };
    const next = this.state.servers.map(item => item.id === id ? { ...item, applications: [...(item.applications || []), value] } : item);
    this.persist(next);
    return structuredClone(value);
  }

  renameApplication(id, name) {
    const target = this.instance(id);
    const label = instanceName(name);
    if (!id.startsWith('app:')) {
      if (!target.deployment) throw new UserError('missing_application', '没有找到此软件实例');
      this.setInstanceField(id, 'applicationName', label);
    } else this.setInstanceField(id, 'name', label);
    return this.applications(target.serverId || id).find(item => item.id === id);
  }

  removeDraft(id) {
    const target = this.instance(id);
    if (!id.startsWith('app:') || target.deployment || target.update || target.lifecycle) {
      throw new UserError('application_has_task', '此实例已有任务记录，不能移除；请先在详情中核对服务器上的服务和数据');
    }
    this.persist(this.state.servers.map(server => server.id === target.serverId
      ? { ...server, applications: server.applications.filter(item => item.id !== id) } : server));
    return this.applications(target.serverId);
  }

  persist(servers) {
    atomicJson(this.filename, { version: 1, servers });
    this.state = { version: 1, servers };
  }

  setInstanceField(id, field, value) {
    const target = this.instance(id);
    this.persist(this.state.servers.map(server => {
      if (target.serverId && server.id === target.serverId) {
        return { ...server, applications: server.applications.map(item => item.id === id ? { ...item, [field]: value } : item) };
      }
      return server.id === id ? { ...server, [field]: value } : server;
    }));
  }

  credentials(id) {
    if (this.sessionSecrets.has(id)) return this.sessionSecrets.get(id);
    const server = this.get(id);
    if (!server.secret || !this.canRemember()) throw new UserError('missing_auth', '请重新填写此服务器的登录信息');
    try { return JSON.parse(this.encryption.decryptString(Buffer.from(server.secret, 'base64'))); }
    catch { throw new UserError('credential_locked', '系统凭据存储无法解锁，请重新输入登录信息'); }
  }

  save(input, credentials) {
    const target = connectionTarget(input);
    const fingerprint = hostFingerprint(input.fingerprint);
    const existing = input.id ? this.get(input.id) : null;
    if (existing && (existing.host !== target.host || existing.port !== target.port || existing.fingerprint !== fingerprint)) {
      throw new UserError('server_identity_changed', '服务器地址或身份改变时，请新增连接；不会把旧安装任务移到另一台服务器');
    }
    if (typeof input.name !== 'string' || !input.name.trim() || input.name.length > 80) throw new UserError('invalid_input', '请给服务器填写一个名称');
    const value = { id: existing?.id || randomUUID(), name: input.name.trim(), ...target, fingerprint,
      authType: credentials.type, createdAt: existing?.createdAt || Date.now(), updatedAt: Date.now() };
    if (existing?.deployment) value.deployment = existing.deployment;
    if (existing?.update) value.update = existing.update;
    if (existing?.lifecycle) value.lifecycle = existing.lifecycle;
    if (existing?.applications) value.applications = existing.applications;
    if (existing?.applicationName) value.applicationName = existing.applicationName;
    if (input.remember) {
      if (!this.canRemember()) throw new UserError('secure_storage_unavailable', '系统安全存储不可用，请关闭记住登录信息后重试');
      value.secret = this.encryption.encryptString(JSON.stringify(credentials)).toString('base64');
    }
    const next = this.state.servers.filter(item => item.id !== value.id).concat(value);
    atomicJson(this.filename, { version: 1, servers: next });
    this.state = { version: 1, servers: next };
    this.sessionSecrets.set(value.id, credentials);
    return this.list().find(item => item.id === value.id);
  }

  forget(id) {
    this.get(id);
    const next = this.state.servers.filter(item => item.id !== id);
    atomicJson(this.filename, { version: 1, servers: next });
    this.state = { version: 1, servers: next };
    this.sessionSecrets.delete(id);
  }

  setDeployment(id, value) {
    this.instance(id);
    if (!value || !/^[a-f0-9]{32}$/.test(value.job) || !/^[a-f0-9]{64}$/.test(value.checksum)
        || typeof value.version !== 'string' || value.version.length > 80 || !Number.isSafeInteger(value.startedAt)) {
      throw new UserError('invalid_job', '安装任务信息无效');
    }
    const deployment = { job: value.job, checksum: value.checksum, version: value.version, startedAt: value.startedAt };
    if (value.settled === true) deployment.settled = true;
    if (value.imageOptions) deployment.imageOptions = require('./image-policy.cjs').options(value.imageOptions);
    if (value.application) {
      const request = siteRequest(value.application);
      const instance = this.instance(id);
      if (instance.appId !== 'static-site' || instance.id !== 'app:' + [request.instance_id.slice(0, 8), request.instance_id.slice(8, 12), request.instance_id.slice(12, 16), request.instance_id.slice(16, 20), request.instance_id.slice(20)].join('-')) {
        throw new UserError('invalid_application', '网站任务不属于此实例');
      }
      deployment.application = request;
    }
    if (value.previousChecksum) {
      if (!/^[a-f0-9]{64}$/.test(value.previousChecksum)) throw new UserError('invalid_job', '原安装包信息无效');
      deployment.previousChecksum = value.previousChecksum;
    }
    this.setInstanceField(id, 'deployment', deployment);
  }

  setUpdate(id, value) {
    const server = this.instance(id);
    const update = updateRecord(value);
    if (update.job !== server.deployment?.job) throw new UserError('invalid_update', '版本任务不属于此服务器实例');
    this.setInstanceField(id, 'update', update);
  }

  setLifecycle(id, value) {
    const server = this.instance(id);
    const lifecycle = lifecycleRecord(value);
    if (lifecycle.job !== server.deployment?.job) throw new UserError('invalid_lifecycle', '维护任务不属于此服务器实例');
    if (JSON.stringify(server.lifecycle) === JSON.stringify(lifecycle)) return;
    this.setInstanceField(id, 'lifecycle', lifecycle);
  }
}

module.exports = { Connections, atomicJson };
