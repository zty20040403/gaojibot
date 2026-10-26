'use strict';

const { UserError } = require('./contracts.cjs');
const catalog = require('./applications.json');

function application(id) {
  const value = catalog.applications.find(item => item.id === id);
  if (!value) throw new UserError('unknown_application', '请选择客户端内置的软件');
  return structuredClone(value);
}

function instanceName(value) {
  if (typeof value !== 'string' || !value.trim() || value.length > 80 || /[\x00-\x1f\x7f]/.test(value)) {
    throw new UserError('invalid_application', '请填写 1 到 80 字的实例名称');
  }
  return value.trim();
}

function supported(id, probe) {
  return application(id).systems.some(item => {
    const [os, version, arch] = item.split(':');
    return os === probe.os && (version === '*' || version === probe.version) && arch === probe.arch;
  });
}

function siteRequest(value) {
  const withBackup = ['restore', 'delete-backup'].includes(value?.operation);
  const keys = ['app_id', 'content_sha256', 'expected_revision', 'instance_id', 'name', 'operation', 'schema', ...(withBackup ? ['backup_id'] : [])];
  if (!value || Object.keys(value).sort().join() !== keys.sort().join()
      || value.schema !== 1 || value.app_id !== 'static-site' || !/^[a-f0-9]{32}$/.test(value.instance_id || '')
      || !['install', 'update', 'start', 'stop', 'rollback', 'uninstall', 'purge', 'backup', 'restore', 'delete-backup'].includes(value.operation)
      || withBackup && !/^[a-f0-9]{32}$/.test(value.backup_id || '')
      || !Number.isSafeInteger(value.expected_revision) || value.expected_revision < 0
      || typeof value.content_sha256 !== 'string' || value.content_sha256 && !/^[a-f0-9]{64}$/.test(value.content_sha256)
      || Boolean(value.content_sha256) !== ['install', 'update'].includes(value.operation)) {
    throw new UserError('invalid_application', '网站部署参数无效');
  }
  instanceName(value.name);
  return structuredClone(value);
}

module.exports = { application, instanceName, supported, siteRequest, catalog: () => structuredClone(catalog.applications) };
