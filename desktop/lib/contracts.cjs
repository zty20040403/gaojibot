'use strict';

const { isIP } = require('node:net');

class UserError extends Error {
  constructor(code, message) { super(message); this.name = 'UserError'; this.code = code; }
}

function text(value, field, maximum = 255) {
  if (typeof value !== 'string' || !value.trim() || value.length > maximum || /[\x00-\x1f\x7f]/.test(value)) {
    throw new UserError('invalid_input', `${field}格式不正确`);
  }
  return value.trim();
}

function connectionTarget(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new UserError('invalid_input', '请填写服务器信息');
  const host = text(input.host, '服务器地址');
  if (!isIP(host) && !/^(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$/.test(host)) {
    throw new UserError('invalid_host', '填写 IP 或域名，不要添加 http://、路径或命令');
  }
  if (host.split('.').some(label => label.length > 63 || label.startsWith('-') || label.endsWith('-')) && !isIP(host)) {
    throw new UserError('invalid_host', '服务器域名格式不正确');
  }
  const port = input.port === undefined ? 22 : input.port;
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new UserError('invalid_port', 'SSH 端口必须在 1～65535 之间');
  const username = text(input.username, '用户名', 64);
  if (!/^[a-zA-Z_][a-zA-Z0-9_.-]*\$?$/.test(username)) throw new UserError('invalid_username', '服务器用户名格式不正确');
  return { host, port, username };
}

function hostFingerprint(value) {
  if (typeof value !== 'string' || !/^SHA256:[A-Za-z0-9+/]{43}$/.test(value)) {
    throw new UserError('untrusted_host', '请先确认服务器指纹');
  }
  return value;
}

function shellLiteral(value) {
  if (typeof value !== 'string' || value.includes('\0') || value.includes('\n') || value.includes('\r')) {
    throw new UserError('invalid_argument', '无效的部署参数');
  }
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

function instanceId(value) {
  if (typeof value !== 'string' || !/^[a-f0-9]{32}$/.test(value)) throw new UserError('invalid_instance', '实例编号无效');
  return value;
}

module.exports = { UserError, connectionTarget, hostFingerprint, instanceId, shellLiteral };
