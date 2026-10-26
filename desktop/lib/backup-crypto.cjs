'use strict';

// V1 envelope: magic, JSON-header length, salt, IV, authenticated metadata,
// AES-256-GCM ciphertext, 16-byte tag. No plaintext temporary file is created.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { promisify } = require('node:util');
const { UserError } = require('./contracts.cjs');
const scrypt = promisify(crypto.scrypt);
const MAGIC = Buffer.from('GAOJIBK1');
const PREFIX = 40;
const BLOCK = 1024 * 1024;
const MAX_BYTES = 32 * 1024 ** 3;
const KDF = Object.freeze({ N: 32768, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });

function metadata(value) {
  if (value?.app_id === 'static-site') {
    const fields = ['schema', 'app_id', 'id', 'source_instance_id', 'bytes', 'sha256', 'created_at', 'files'];
    if (Object.keys(value).sort().join() !== fields.sort().join() || value.schema !== 1
        || !/^[a-f0-9]{32}$/.test(value.id) || !/^[a-f0-9]{32}$/.test(value.source_instance_id)
        || !/^[a-f0-9]{64}$/.test(value.sha256)
        || ['bytes', 'created_at', 'files'].some(key => !Number.isSafeInteger(value[key]) || value[key] < 0)
        || value.bytes < 1 || value.bytes > 136 * 1024 ** 2 || value.files < 1 || value.files > 5000) {
      throw new UserError('invalid_backup', '网站备份格式或大小不支持');
    }
    return { ...value };
  }
  const keys = ['schema', 'id', 'source_instance_id', 'runtime_checksum', 'bytes', 'sha256', 'database_bytes', 'created_at', 'revision', 'files'];
  if (!value || Object.keys(value).sort().join() !== keys.sort().join() || value.schema !== 1
      || !/^[a-f0-9]{32}$/.test(value.id) || !/^[a-f0-9]{32}$/.test(value.source_instance_id)
      || !/^[a-f0-9]{64}$/.test(value.runtime_checksum) || !/^[a-f0-9]{64}$/.test(value.sha256)
      || ['bytes', 'database_bytes', 'created_at', 'revision', 'files'].some(key => !Number.isSafeInteger(value[key]) || value[key] < 0)
      || value.bytes < 1024 || value.bytes > MAX_BYTES || value.database_bytes > MAX_BYTES * 16
      || value.revision < 1 || value.files < 2 || value.files > 200000) {
    throw new UserError('invalid_backup', '备份格式或大小不支持，未执行恢复');
  }
  return { ...value };
}
function validatePassword(value) {
  if (typeof value !== 'string' || value.length < 12 || Buffer.byteLength(value, 'utf8') > 1024 || value.includes('\0')) {
    throw new UserError('backup_password', '备份密码至少 12 个字符，最多 1024 字节，请使用独立的长密码');
  }
}
function check(signal) { signal?.throwIfAborted(); }
async function derive(password, salt) {
  validatePassword(password);
  const input = Buffer.from(password, 'utf8');
  try { return await scrypt(input, salt, 32, KDF); }
  finally { input.fill(0); }
}
async function writeAll(file, bytes) {
  for (let offset = 0; offset < bytes.length;) {
    const { bytesWritten } = await file.write(bytes, offset, bytes.length - offset);
    if (!bytesWritten) throw new Error('Incomplete backup write');
    offset += bytesWritten;
  }
}
async function readAt(file, bytes, position) {
  const result = Buffer.alloc(bytes);
  for (let offset = 0; offset < bytes;) {
    const { bytesRead } = await file.read(result, offset, bytes - offset, position + offset);
    if (!bytesRead) throw new UserError('invalid_backup', '备份文件被截断');
    offset += bytesRead;
  }
  return result;
}
function unchanged(before, after) {
  return ['size', 'mtimeMs', 'ctimeMs', 'ino', 'dev'].every(key => before[key] === after[key]);
}

async function encrypt(source, details, password, destination, { signal, progress = () => {} } = {}) {
  const info = metadata(details);
  validatePassword(password);
  const salt = crypto.randomBytes(16), iv = crypto.randomBytes(12);
  const json = Buffer.from(JSON.stringify(info));
  const prefix = Buffer.alloc(PREFIX);
  MAGIC.copy(prefix); prefix.writeUInt32BE(json.length, 8); salt.copy(prefix, 12); iv.copy(prefix, 28);
  const header = Buffer.concat([prefix, json]);
  const partial = path.join(path.dirname(destination), '.gaoji-export-' + crypto.randomUUID() + '.partial');
  let output, key;
  try {
    check(signal);
    const existing = await fs.promises.lstat(destination).catch(error => { if (error.code !== 'ENOENT') throw error; return null; });
    if (existing && (!existing.isFile() || existing.isSymbolicLink())) throw new UserError('invalid_destination', '目标不是普通文件，请选择其他位置');
    const space = await fs.promises.statfs(path.dirname(destination));
    if (space.bavail * space.bsize < info.bytes + header.length + 16) throw new UserError('disk_full', '电脑可用空间不足，原文件未修改');
    key = await derive(password, salt);
    check(signal);
    const cipher = crypto.createCipheriv('aes-256-gcm', key, iv, { authTagLength: 16 });
    cipher.setAAD(header);
    output = await fs.promises.open(partial, 'wx', 0o600);
    await writeAll(output, header);
    const digest = crypto.createHash('sha256');
    let total = 0;
    for await (const bytes of source) {
      check(signal); total += bytes.length;
      if (total > info.bytes) throw new UserError('invalid_backup', '服务器备份大小发生变化');
      digest.update(bytes);
      await writeAll(output, cipher.update(bytes));
      progress(total, info.bytes);
    }
    if (total !== info.bytes || digest.digest('hex') !== info.sha256) throw new UserError('invalid_backup', '备份传输不完整或校验失败，未生成可用备份');
    await writeAll(output, cipher.final());
    await writeAll(output, cipher.getAuthTag());
    await output.sync(); await output.close(); output = null;
    check(signal);
    // The destination comes only from the native Save dialog, including its
    // overwrite confirmation. Until this rename the previous file stays intact.
    await fs.promises.rename(partial, destination);
    return info;
  } finally {
    key?.fill(0);
    try { await output?.close(); }
    finally { await fs.promises.rm(partial, { force: true }); }
  }
}

async function openEncrypted(filename, password, { signal, progress = () => {} } = {}) {
  let file, key;
  try {
    validatePassword(password); check(signal);
    file = await fs.promises.open(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    const before = await file.stat();
    if (!before.isFile() || before.size < PREFIX + 16 || before.size > MAX_BYTES + 8192) throw new UserError('invalid_backup', '选择的文件不是受支持的加密备份');
    const prefix = await readAt(file, PREFIX, 0);
    const length = prefix.readUInt32BE(8);
    if (!prefix.subarray(0, 8).equals(MAGIC) || length < 2 || length > 4096) throw new UserError('invalid_backup', '备份文件头无效');
    const json = await readAt(file, length, PREFIX);
    let info;
    try { info = metadata(JSON.parse(json.toString('utf8'))); }
    catch { throw new UserError('invalid_backup', '备份文件头无效'); }
    const header = Buffer.concat([prefix, json]);
    if (before.size !== header.length + info.bytes + 16) throw new UserError('invalid_backup', '备份大小不符或文件被截断');
    key = await derive(password, prefix.subarray(12, 28));
    const tag = await readAt(file, 16, before.size - 16);
    async function* plaintext(report) {
      check(signal);
      if (!unchanged(before, await file.stat())) throw new UserError('invalid_backup', '备份在读取期间被更改');
      const decipher = crypto.createDecipheriv('aes-256-gcm', key, prefix.subarray(28, 40), { authTagLength: 16 });
      decipher.setAAD(header); decipher.setAuthTag(tag);
      const digest = crypto.createHash('sha256');
      for (let offset = 0; offset < info.bytes;) {
        check(signal);
        const bytes = await readAt(file, Math.min(BLOCK, info.bytes - offset), header.length + offset);
        const plain = decipher.update(bytes);
        digest.update(plain); offset += bytes.length;
        report(offset, info.bytes);
        yield plain;
      }
      try { decipher.final(); }
      catch { throw new UserError('backup_password_or_damage', '备份密码错误或文件已损坏，未导入'); }
      if (digest.digest('hex') !== info.sha256 || !unchanged(before, await file.stat())) throw new UserError('invalid_backup', '备份校验失败或读取期间被更改');
    }
    // Authenticate the entire file before any plaintext can leave this computer.
    for await (const _chunk of plaintext(progress)) { /* Validation pass, no plaintext disk output. */ }
    let closed = false;
    return { metadata: info, stream: (report = () => {}) => plaintext(report), close: async () => {
      if (!closed) { closed = true; key.fill(0); await file.close(); }
    } };
  } catch (error) {
    key?.fill(0); await file?.close(); throw error;
  }
}

module.exports = { MAX_BYTES, metadata, validatePassword, encrypt, openEncrypted };
