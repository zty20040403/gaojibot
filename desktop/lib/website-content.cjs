'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const tar = require('tar');
const { UserError } = require('./contracts.cjs');

const MAX_BYTES = 128 * 1024 ** 2;
const MAX_FILE = 32 * 1024 ** 2;
const MAX_FILES = 5000;
const denied = new Set(['node_modules', '.git', '.env', '.ssh', '.DS_Store']);

function filename(name) {
  if (!name || name.length > 240 || /[\x00-\x1f\x7f\\:]/.test(name) || name.startsWith('.') || denied.has(name)
      || /\.(?:pem|key|p12|pfx)$/i.test(name)) {
    throw new UserError('invalid_website', '网站目录包含隐藏文件、密钥或无效文件名，请选择构建后的网页目录');
  }
}

async function snapshot(source, cache) {
  const origin = fs.lstatSync(source);
  if (!origin.isDirectory() || origin.isSymbolicLink()) throw new UserError('invalid_website', '请选择实际的网页文件夹');
  fs.mkdirSync(cache, { recursive: true, mode: 0o700 });
  const temporary = fs.mkdtempSync(path.join(cache, 'site-'));
  const content = path.join(temporary, 'content');
  fs.mkdirSync(content, { mode: 0o700 });
  const files = [];
  let bytes = 0;
  function copy(directory, relative = '', depth = 0) {
    if (depth > 20) throw new UserError('invalid_website', '网站目录层级超过上限');
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.name === '.DS_Store' && entry.isFile()) continue;
      filename(entry.name);
      const input = path.join(directory, entry.name);
      const name = relative ? `${relative}/${entry.name}` : entry.name;
      const output = path.join(content, name);
      const stat = fs.lstatSync(input);
      if (stat.isDirectory() && !stat.isSymbolicLink()) {
        fs.mkdirSync(output, { mode: 0o755 }); copy(input, name, depth + 1); continue;
      }
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_FILE || files.length >= MAX_FILES) {
        throw new UserError('invalid_website', '网站包含链接、特殊文件、过大文件或文件数量超过上限');
      }
      bytes += stat.size;
      if (bytes > MAX_BYTES) throw new UserError('invalid_website', '网站总大小不能超过 128 MiB');
      const fd = fs.openSync(input, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
      try {
        const opened = fs.fstatSync(fd);
        if (opened.ino !== stat.ino || opened.dev !== stat.dev || opened.size !== stat.size) throw new UserError('website_changed', '网站文件正在变化，请停止构建后重新选择');
        const data = Buffer.alloc(stat.size);
        let offset = 0;
        while (offset < data.length) {
          const read = fs.readSync(fd, data, offset, data.length - offset, offset);
          if (!read) throw new UserError('website_changed', '网站文件正在变化，请重新选择');
          offset += read;
        }
        const after = fs.fstatSync(fd);
        if (after.size !== stat.size || after.mtimeMs !== stat.mtimeMs) throw new UserError('website_changed', '网站文件正在变化，请重新选择');
        fs.writeFileSync(output, data, { flag: 'wx', mode: 0o644 });
        files.push(name);
      } finally { fs.closeSync(fd); }
    }
  }
  try {
    copy(source);
    if (!files.includes('index.html')) throw new UserError('invalid_website', '所选文件夹根目录需要有 index.html');
    if (fs.statSync(path.join(content, 'index.html')).size === 0) throw new UserError('invalid_website', 'index.html 不能为空');
    if (new Set(files.map(name => name.toLocaleLowerCase('en-US'))).size !== files.length) throw new UserError('invalid_website', '网站包含仅大小写不同的文件名');
    const archive = path.join(temporary, 'website.tar.gz');
    await tar.c({ file: archive, cwd: content, gzip: true, portable: true, noMtime: true, follow: false }, files.sort());
    const data = fs.readFileSync(archive);
    const checksum = createHash('sha256').update(data).digest('hex');
    fs.rmSync(content, { recursive: true });
    return { archive, checksum, bytes, archiveBytes: data.length, files: files.length, name: path.basename(source), temporary };
  } catch (error) { fs.rmSync(temporary, { recursive: true, force: true }); throw error; }
}

module.exports = { snapshot, MAX_BYTES, MAX_FILES, MAX_FILE };
