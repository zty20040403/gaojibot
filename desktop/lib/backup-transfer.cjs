'use strict';

const http = require('node:http');
const { createHash } = require('node:crypto');
const control = require('./control.cjs');
const crypto = require('./backup-crypto.cjs');
const { jobId } = require('./deploy.cjs');
const { UserError } = require('./contracts.cjs');

async function transport(client, access, route, headers, signal) {
  signal?.throwIfAborted();
  if (!['/backup/export', '/backup/import', '/backup/import/status'].includes(route) || !control.validPort(access.port)) throw new UserError('invalid_backup', '备份传输目标无效');
  const socket = await control.forward(client, access.port);
  const agent = new http.Agent({ keepAlive: false });
  agent.createConnection = () => socket;
  const req = http.request({ hostname: '127.0.0.1', port: access.port, path: route, method: 'POST', agent,
    headers: { ...headers, Authorization: 'Bearer ' + access.token } });
  const response = new Promise((resolve, reject) => { req.once('response', resolve); req.once('error', reject); });
  response.catch(() => {});
  const abort = () => req.destroy(new Error('Backup transfer cancelled'));
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) abort();
  // SSH forwardOut returns a Duplex channel, not a net.Socket. ClientRequest's
  // setTimeout calls socket.setTimeout and crashes on that real transport.
  let idleTimer;
  const touch = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => req.destroy(new Error('Backup transfer stalled')), 60000);
  };
  touch();
  req.once('response', touch);
  const timer = setTimeout(() => req.destroy(new Error('Backup transfer deadline')), 60 * 60 * 1000);
  return { req, response, touch, close: () => { clearTimeout(timer); clearTimeout(idleTimer); req.removeListener('response', touch); signal?.removeEventListener('abort', abort); req.destroy(); agent.destroy(); socket.destroy(); } };
}
async function* activity(source, touch) {
  for await (const chunk of source) { touch(); yield chunk; }
}
async function jsonResponse(response, touch = () => {}) {
  let size = 0; const chunks = [];
  for await (const chunk of response) {
    touch();
    size += chunk.length;
    if (size > 65536) throw new UserError('invalid_backup_response', '备份响应过大');
    chunks.push(chunk);
  }
  let value;
  try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new UserError('invalid_backup_response', '备份接口没有返回有效响应'); }
  if (response.statusCode !== 200) throw new UserError(response.statusCode === 404 ? 'backup_unsupported' : 'backup_failed',
    typeof value.error === 'string' ? value.error.slice(0, 500) : '备份传输尚未完成');
  return value.data;
}
function failure(error, signal) {
  if (signal?.aborted) return new UserError('backup_cancelled', '备份传输已取消；没有执行恢复');
  return error instanceof UserError ? error : new UserError('backup_connection_lost', '备份传输中断，请重新查看备份列表后重试；没有执行恢复');
}
function writableOrAnswered(req, answered) {
  if (answered()) return Promise.resolve();
  if (req.destroyed) return Promise.reject(new Error('Backup upload closed'));
  return new Promise((resolve, reject) => {
    const finish = error => {
      req.removeListener('drain', drained); req.removeListener('response', drained);
      req.removeListener('error', finish); req.removeListener('close', closed);
      if (error) reject(error); else resolve();
    };
    const drained = () => finish();
    const closed = () => finish(answered() ? null : new Error('Backup upload closed'));
    req.once('drain', drained); req.once('response', drained);
    req.once('error', finish); req.once('close', closed);
  });
}
async function upload(source, req, answered, touch) {
  // A deduplicated import can reply before consuming the request body. Do not
  // pipeline() into ClientRequest: its early-close cleanup destroys the receipt.
  try {
    for await (const bytes of source) {
      if (answered()) break;
      touch();
      if (!req.write(bytes)) await writableOrAnswered(req, answered);
    }
    if (!answered()) req.end();
  } catch (error) {
    if (!answered()) req.destroy(error);
    throw error;
  }
}
function validateReceipt(value, access, info) {
  const expected = createHash('sha256').update(access.instance_id + ':' + info.sha256).digest('hex').slice(0, 32);
  if (!value || typeof value.imported !== 'boolean' || value.id !== expected) throw new UserError('invalid_backup_response', '导入回执与当前备份不匹配，请查看列表核对');
  return value;
}
async function importReceipt(client, access, info, signal) {
  const body = JSON.stringify({ metadata: info });
  const connection = await transport(client, access, '/backup/import/status', { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }, signal);
  try {
    connection.req.end(body);
    return validateReceipt(await jsonResponse(await connection.response, connection.touch), access, info);
  } finally { connection.close(); }
}
async function exportBackup(client, access, identifier, password, filename, options = {}) {
  jobId(identifier); crypto.validatePassword(password);
  let connection;
  try {
    const body = JSON.stringify({ id: identifier });
    connection = await transport(client, access, '/backup/export', { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }, options.signal);
    connection.req.end(body);
    const response = await connection.response;
    if (response.statusCode !== 200) return await jsonResponse(response, connection.touch);
    let info;
    try {
      const encoded = response.headers['x-gaoji-backup'];
      if (typeof encoded !== 'string' || encoded.length > 8192) throw new Error();
      info = crypto.metadata(JSON.parse(Buffer.from(encoded, 'base64').toString('utf8')));
      if (info.app_id) throw new Error('Wrong application backup');
      if (info.id !== identifier || String(info.bytes) !== response.headers['content-length'] || response.headers['content-type'] !== 'application/octet-stream') throw new Error();
    } catch { throw new UserError('invalid_backup_response', '服务器返回的备份与所选文件不符'); }
    await crypto.encrypt(activity(response, connection.touch), info, password, filename, options);
    return { exported: true, bytes: info.bytes };
  } catch (error) { throw failure(error, options.signal); }
  finally { connection?.close(); }
}
async function importBackup(client, access, filename, password, { signal, progress = () => {} } = {}) {
  let archive, connection;
  try {
    archive = await crypto.openEncrypted(filename, password, { signal, progress: (bytes, total) => progress('verifying', bytes, total) });
    if (archive.metadata.app_id) throw new UserError('wrong_backup', '这是网站备份，请在静态网站中导入，未上传数据');
    const current = await control.request(client, access, 'versions');
    if (current.checksum !== archive.metadata.runtime_checksum) throw new UserError('backup_version_mismatch', '备份与目标实例的运行版本不一致，请先使用匹配版本；未上传数据');
    if (!Number.isSafeInteger(current.revision) || current.revision < 1) throw new UserError('invalid_backup_response', '目标实例配置版本无效');
    const instance = current.instance_id ?? access.instance_id;
    if (!/^[a-f0-9]{32}$/.test(instance || '') || access.instance_id && instance !== access.instance_id) {
      throw new UserError('invalid_backup_response', '目标实例身份无法核对，未上传数据');
    }
    access = { ...access, instance_id: instance };
    const info = archive.metadata;
    const previous = await importReceipt(client, access, info, signal);
    if (previous.imported) return previous;
    connection = await transport(client, access, '/backup/import', { 'Content-Type': 'application/octet-stream',
      'Content-Length': info.bytes, 'X-Gaoji-Backup': Buffer.from(JSON.stringify(info)).toString('base64'),
      'X-Gaoji-Revision': String(current.revision) }, signal);
    let answered = false;
    connection.req.once('response', () => { answered = true; });
    const receiver = connection.response.then(response => jsonResponse(response, connection.touch));
    const sender = upload(archive.stream((bytes, total) => progress('uploading', bytes, total)), connection.req, () => answered, connection.touch);
    const [_sent, received] = await Promise.allSettled([sender, receiver]);
    if (received.status === 'rejected') {
      // HTTP may close with an unread upload after a concurrent import, or lose
      // the final acknowledgement. Query the durable receipt; never resend here.
      connection.close(); connection = null;
      if (!signal?.aborted) {
        try { const confirmed = await importReceipt(client, access, info, signal); if (confirmed.imported) return confirmed; }
        catch { /* Preserve the original transfer failure if receipt lookup fails. */ }
      }
      throw received.reason;
    }
    if (!received.value || received.value.imported !== true) throw new UserError('invalid_backup_response', '服务器未确认备份导入，请查看列表核对');
    return validateReceipt(received.value, access, info);
  } catch (error) { throw failure(error, signal); }
  finally { connection?.close(); await archive?.close(); }
}

module.exports = { exportBackup, importBackup };
