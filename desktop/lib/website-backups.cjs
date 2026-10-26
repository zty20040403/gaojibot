'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');
const crypto = require('./backup-crypto.cjs');
const { jobId, privilegedCommand } = require('./deploy.cjs');
const { UserError } = require('./contracts.cjs');

async function* remoteArchive(client, instance, backup, privilege, password) {
  jobId(instance); jobId(backup);
  const folder = `/var/lib/gaoji-app/applications/${instance}`;
  const filename = `${folder}/backups/${backup}.tar.gz`;
  const command = `set -eu\ntest ! -L ${folder}\ntest ! -L ${folder}/backups\ntest ! -L ${filename}\ntest -f ${filename}\nexec cat -- ${filename}`;
  const request = privilegedCommand(command, privilege, password);
  let channel, timer;
  const failure = () => new UserError('backup_transfer_failed', '网站备份读取中断或权限不足，请重试；没有生成可用的导出文件');
  try {
    channel = await new Promise((resolve, reject) => {
      let expired = false;
      timer = setTimeout(() => { expired = true; reject(failure()); }, 30000);
      client.exec(request.command, (error, stream) => {
        if (expired) { stream?.close(); return; }
        clearTimeout(timer);
        if (error) reject(failure()); else resolve(stream);
      });
    });
    const closed = new Promise((resolve, reject) => {
      channel.once('close', code => code === 0 ? resolve() : reject(failure()));
      channel.once('error', () => reject(failure()));
    });
    closed.catch(() => {});
    let stderrBytes = 0;
    channel.stderr.on('data', bytes => { stderrBytes += bytes.length; if (stderrBytes > 8192) channel.destroy(failure()); });
    // Bound the whole transfer, including a sudo prompt or stalled SSH channel.
    timer = setTimeout(() => channel.destroy(failure()), 10 * 60 * 1000);
    channel.end(request.stdin);
    for await (const bytes of channel) yield bytes;
    await closed;
  } finally { clearTimeout(timer); channel?.close(); }
}

async function exportBackup(client, instance, details, privilege, sudoPassword, password, filename) {
  const info = crypto.metadata(details);
  if (info.app_id !== 'static-site' || info.source_instance_id !== instance) throw new UserError('wrong_backup', '备份不属于此网站');
  await crypto.encrypt(remoteArchive(client, instance, info.id, privilege, sudoPassword), info, password, filename);
  return { exported: true, bytes: info.bytes };
}

async function importBackup(filename, password, directory) {
  const archive = await crypto.openEncrypted(filename, password);
  const partial = path.join(directory, '.site-import-' + randomUUID());
  try {
    const info = archive.metadata;
    if (info.app_id !== 'static-site') throw new UserError('wrong_backup', '这是机器人备份，请在机器人备份页面导入');
    const space = await fs.promises.statfs(directory);
    if (space.bavail * space.bsize < info.bytes + 8 * 1024 ** 2) throw new UserError('disk_full', '电脑没有足够空间暂存网站备份');
    // Authentication completes before plaintext is staged. The ordinary website
    // extractor validates paths and file limits again before any server promotion.
    await pipeline(Readable.from(archive.stream()), fs.createWriteStream(partial, { flags: 'wx', mode: 0o600 }));
    const target = path.join(directory, info.sha256 + '.tar.gz');
    if (fs.existsSync(target)) {
      if (fs.lstatSync(target).isSymbolicLink() || createHash('sha256').update(fs.readFileSync(target)).digest('hex') !== info.sha256) throw new UserError('invalid_backup', '网站缓存校验失败');
    } else await fs.promises.rename(partial, target);
    return { selection: randomUUID(), checksum: info.sha256, name: '导入的网站备份', files: info.files, bytes: info.bytes };
  } finally { await archive.close(); await fs.promises.rm(partial, { force: true }); }
}

module.exports = { remoteArchive, exportBackup, importBackup };
