import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';

function verified(filename, source) {
  try {
    const stat = fs.lstatSync(filename);
    return stat.isFile() && !stat.isSymbolicLink() && stat.size === source.bytes
      && createHash('sha256').update(fs.readFileSync(filename)).digest('hex') === source.sha256;
  } catch { return false; }
}

export function prepareBuildSources(root, download = downloadSource) {
  const lock = JSON.parse(fs.readFileSync(path.join(root, 'packaging/server/build-sources.lock.json'), 'utf8'));
  if (lock.schema !== 1 || !Array.isArray(lock.files) || lock.files.length < 1 || lock.files.length > 10) throw new Error('Invalid build source lock');
  const cache = path.join(root, 'packaging/build/source-cache');
  fs.mkdirSync(cache, { recursive: true });
  if (fs.lstatSync(cache).isSymbolicLink()) throw new Error('Source cache must not be a symlink');
  const names = new Set();
  return lock.files.map(source => {
    const url = new URL(source.url);
    if (!/^[a-zA-Z0-9._-]+\.tar\.gz$/.test(source.name) || names.has(source.name)
      || !/^[a-f0-9]{64}$/.test(source.sha256) || !Number.isSafeInteger(source.bytes) || source.bytes < 1 || source.bytes > 25 * 1024 ** 2
      || url.origin !== 'https://codeload.github.com' || url.username || url.password || url.search || url.hash) throw new Error('Untrusted build source');
    names.add(source.name);
    const filename = path.join(cache, source.name);
    if (!verified(filename, source)) {
      const partial = path.join(cache, `.${randomUUID()}.part`);
      try {
        console.log(`Preparing verified build source: ${source.name}`);
        download(source, partial);
        if (!verified(partial, source)) throw new Error(`Build source checksum mismatch: ${source.name}`);
        fs.renameSync(partial, filename);
      } finally { fs.rmSync(partial, { force: true }); }
    }
    return { source: path.relative(root, filename), target: `vendor/${source.name}` };
  });
}

function downloadSource(source, filename) {
  execFileSync(process.platform === 'darwin' ? '/usr/bin/curl' : 'curl', [
    '--fail', '--silent', '--show-error', '--location', '--proto', '=https', '--proto-redir', '=https',
    '--connect-timeout', '30', '--max-time', '1200', '--retry', '2', '--retry-max-time', '1500',
    '--max-filesize', String(source.bytes), '--output', filename, source.url,
  ], { stdio: ['ignore', 'inherit', 'inherit'], timeout: 1800000 });
}
