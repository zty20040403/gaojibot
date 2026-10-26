'use strict';

const { createHash } = require('node:crypto');
const { UserError, connectionTarget, hostFingerprint } = require('./contracts.cjs');
const { PROBE_COMMAND, parseProbe, deploymentPlan } = require('./preflight.cjs');

function fingerprint(key) { return `SHA256:${createHash('sha256').update(key).digest('base64').replace(/=+$/, '')}`; }
function newClient() { const { Client } = require('ssh2'); return new Client(); }

function connectionError(error, changed = false) {
  if (changed) return new UserError('host_changed', '服务器指纹与之前不同，已停止连接；请核实是否更换或重装了服务器');
  if (error?.level === 'client-authentication' || error?.level === 'agent') {
    return new UserError('authentication_failed', 'SSH 登录被拒绝，请检查账户、密码或密钥');
  }
  if (error?.level === 'client-timeout') return new UserError('connection_timeout', '连接服务器超时，服务器可能正在启动，请稍后重试');
  return new UserError('connection_failed', 'SSH 连接中断或尚未就绪，请检查服务器地址、端口和网络后重试');
}

function discoverHost(target, factory = newClient) {
  const config = connectionTarget(target);
  return new Promise((resolve, reject) => {
    const client = factory();
    let seen, settled = false;
    const finish = error => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      if (error) reject(error);
      else resolve({ ...config, fingerprint: seen });
      client.destroy();
    };
    const timeout = setTimeout(() => finish(connectionError({ level: 'client-timeout' })), 15000);
    // ssh2 may emit both a protocol error and a socket error while closing.
    client.on('error', error => finish(seen ? null : connectionError(error)));
    client.once('close', () => finish(seen ? null : connectionError()));
    client.once('ready', () => finish(new UserError('invalid_handshake', '服务器身份验证流程异常')));
    try {
      client.connect({ ...config, readyTimeout: 12000, hostVerifier(key) { seen = fingerprint(key); return false; } });
    } catch (error) { finish(connectionError(error)); }
  });
}

function connect(target, authentication, expectedFingerprint, factory = newClient) {
  const config = connectionTarget(target);
  const trusted = hostFingerprint(expectedFingerprint);
  if (!authentication || typeof authentication !== 'object') throw new UserError('missing_auth', '请选择服务器登录方式');
  const auth = {};
  if (authentication.type === 'password') {
    if (typeof authentication.password !== 'string' || !authentication.password || authentication.password.length > 8192) throw new UserError('missing_auth', '请填写服务器密码');
    auth.password = authentication.password;
  } else if (authentication.type === 'private-key') {
    if (!Buffer.isBuffer(authentication.privateKey) || authentication.privateKey.length > 128 * 1024) throw new UserError('missing_auth', '请选择有效的私钥文件');
    auth.privateKey = authentication.privateKey;
    if (authentication.passphrase) auth.passphrase = authentication.passphrase;
  } else if (authentication.type === 'agent') {
    if (!authentication.agent || typeof authentication.agent !== 'string') throw new UserError('missing_auth', '没有可用的 SSH 密钥代理');
    auth.agent = authentication.agent;
  } else if (authentication.type === 'tailscale') {
    // Tailnet SSH can authorize the already authenticated network identity.
    // If the server does not support this, fail rather than trying blank passwords.
    auth.authHandler = ['none'];
  } else throw new UserError('missing_auth', '不支持的登录方式');
  return new Promise((resolve, reject) => {
    const client = factory();
    let changed = false, settled = false, closed = false;
    const fail = error => {
      if (!settled) { settled = true; reject(connectionError(error, changed)); }
      if (!closed) { closed = true; client.destroy(); }
    };
    client.once('ready', () => { if (!settled) { settled = true; resolve(client); } });
    // Keep the listener through teardown; later errors close active channels,
    // whose callers must recheck durable operation status before retrying.
    client.on('error', fail);
    client.once('close', () => { closed = true; if (!settled) fail(); });
    try {
      client.connect({ ...config, ...auth, readyTimeout: 20000, keepaliveInterval: 15000, keepaliveCountMax: 3,
        hostVerifier(key) { changed = fingerprint(key) !== trusted; return !changed; } });
    } catch (error) { fail(error); }
  });
}

function execute(client, command, { timeoutMs = 30000, maxBytes = 1024 * 1024, stdin } = {}) {
  return new Promise((resolve, reject) => {
    let done = false;
    let channel;
    const finish = (error, result) => {
      if (done) return;
      done = true; clearTimeout(timer);
      if (error) { channel?.close(); reject(error); } else resolve(result);
    };
    const timer = setTimeout(() => finish(new UserError('operation_timeout', '服务器响应超时，请重新查询状态')), timeoutMs);
    client.exec(command, (error, stream) => {
      if (error) { finish(new UserError('remote_exec_failed', '服务器没有接受操作')); return; }
      if (done) { stream.close(); return; }
      channel = stream;
      const stdout = [], stderr = [];
      let bytes = 0;
      const collect = list => chunk => {
        bytes += chunk.length;
        if (bytes > maxBytes) { finish(new UserError('response_too_large', '服务器返回内容过多，已停止读取')); return; }
        list.push(Buffer.from(chunk));
      };
      stream.on('data', collect(stdout));
      stream.stderr.on('data', collect(stderr));
      stream.once('error', () => finish(new UserError('connection_interrupted', '连接中断；操作是否完成需要重新核对')));
      stream.once('close', (code, signal) => finish(null, { code: code ?? null, signal: signal || null,
        stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8') }));
      stream.end(stdin);
    });
  });
}

async function inspectServer(client) {
  const result = await execute(client, PROBE_COMMAND, { maxBytes: 128 * 1024 });
  if (result.code !== 0) throw new UserError('probe_failed', '服务器检测失败，尚未修改系统');
  const probe = parseProbe(result.stdout);
  return { probe, plan: deploymentPlan(probe) };
}

module.exports = { fingerprint, discoverHost, connect, execute, inspectServer };
