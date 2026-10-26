'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

async function main() {
  const tag = process.env.RELEASE_TAG;
  const expected = process.env.MANIFEST_SHA256;
  if (!/^app-v\d+\.\d+\.\d+$/.test(tag || '') || !/^[a-f0-9]{64}$/.test(expected || '')) throw Error('Invalid release identity');
  const repository = 'zty20040403/gaoji-app';
  const headers = process.env.GH_TOKEN ? { Authorization: `Bearer ${process.env.GH_TOKEN}` } : {};
  const allowDraft = process.env.ALLOW_DRAFT === 'true';
  const response = await fetch(`https://api.github.com/repos/${repository}${allowDraft ? '/releases?per_page=100' : `/releases/tags/${tag}`}`, {
    headers, signal: AbortSignal.timeout(30000) });
  if (!response.ok) throw Error(`Release lookup HTTP ${response.status}`);
  const payload = await response.json();
  const release = allowDraft ? payload.find(item => item.tag_name === tag) : payload;
  if (!release || release.draft && !allowDraft || release.tag_name !== tag) throw Error('Unexpected release');
  function asset(name) {
    const found = release.assets.find(value => value.name === name && value.state === 'uploaded');
    const url = `https://github.com/${repository}/releases/download/${tag}/${encodeURIComponent(name)}`;
    if (!found || !Number.isSafeInteger(found.id)
        || found.url !== `https://api.github.com/repos/${repository}/releases/assets/${found.id}`
        || !release.draft && found.browser_download_url !== url) throw Error('Unexpected download target');
    return found;
  }
  async function downloadAsset(remote, timeout = 300000) {
    if (!release.draft) return fetch(remote.browser_download_url, { signal: AbortSignal.timeout(timeout) });
    if (!Number.isSafeInteger(remote.id)) throw Error('Invalid asset identity');
    return fetch(`https://api.github.com/repos/${repository}/releases/assets/${remote.id}`, {
      headers: { ...headers, Accept: 'application/octet-stream' }, signal: AbortSignal.timeout(timeout) });
  }
  const manifestAsset = asset('release.json');
  const manifestResponse = await downloadAsset(manifestAsset, 60000);
  if (!manifestResponse.ok) throw Error('Manifest download failed');
  const raw = Buffer.from(await manifestResponse.arrayBuffer());
  if (raw.length > 1024 ** 2 || createHash('sha256').update(raw).digest('hex') !== expected) throw Error('Manifest hash mismatch');
  const manifest = JSON.parse(raw);
  const version = tag.slice(5);
  if (manifest.version !== version) throw Error('Manifest version mismatch');
  const directory = path.resolve('release-input');
  fs.mkdirSync(directory, { recursive: true });
  const verified = [];
  for (const name of [`Gaoji-Setup-${version}.exe`, `Gaoji-${version}-win.zip`]) {
    const file = manifest.files.find(item => item.name === name);
    const remote = asset(name);
    if (!file || !Number.isSafeInteger(file.bytes) || file.bytes > 1024 ** 3 || file.bytes < 1
        || remote.size !== file.bytes || remote.digest !== `sha256:${file.sha256}`) throw Error('Installer metadata mismatch');
    const download = await downloadAsset(remote);
    if (!download.ok) throw Error(`Download HTTP ${download.status}`);
    const filename = path.join(directory, name);
    const descriptor = fs.openSync(filename, 'wx');
    const hash = createHash('sha256');
    let bytes = 0;
    try {
      for await (const chunk of download.body) {
        bytes += chunk.length;
        if (bytes > file.bytes) throw Error('Download too large');
        hash.update(chunk);
        const buffer = Buffer.from(chunk);
        let offset = 0;
        while (offset < buffer.length) offset += fs.writeSync(descriptor, buffer, offset, buffer.length - offset);
      }
    } finally { fs.closeSync(descriptor); }
    if (bytes !== file.bytes || hash.digest('hex') !== file.sha256) throw Error('Installer checksum mismatch');
    verified.push(file);
    console.log(`Verified download: ${name}`);
  }
  fs.writeFileSync(path.join(directory, 'verified.json'), JSON.stringify({ version, release: release.html_url, manifestSHA256: expected, files: verified }, null, 2));
  fs.writeFileSync(path.join(directory, 'release.json'), raw);
  fs.appendFileSync(process.env.GITHUB_ENV, `GAOJI_RELEASE_VERSION=${version}\nGAOJI_INSTALLER=${path.join(directory, `Gaoji-Setup-${version}.exe`)}\nGAOJI_PORTABLE_ZIP=${path.join(directory, `Gaoji-${version}-win.zip`)}\n`);
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
