'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');

async function main() {
  const tag = process.env.RELEASE_TAG;
  const expected = process.env.MANIFEST_SHA256;
  if (!/^app-v\d+\.\d+\.\d+$/.test(tag || '') || !/^[a-f0-9]{64}$/.test(expected || '')) throw Error('Invalid release identity');
  const directory = path.resolve('release-input');
  const raw = fs.readFileSync(path.join(directory, 'release.json'));
  if (raw.length > 1024 ** 2 || createHash('sha256').update(raw).digest('hex') !== expected) throw Error('Manifest checksum mismatch');
  const manifest = JSON.parse(raw);
  const version = tag.slice(5);
  if (manifest.version !== version || !Array.isArray(manifest.files)) throw Error('Manifest version mismatch');
  for (const name of [`Gaoji-Setup-${version}.exe`, `Gaoji-${version}-win.zip`]) {
    const entries = manifest.files.filter(item => item.name === name);
    if (entries.length !== 1) throw Error('Ambiguous installer identity');
    const file = entries[0];
    const filename = path.join(directory, name);
    const stat = fs.lstatSync(filename);
    if (!Number.isSafeInteger(file.bytes) || file.bytes < 1 || file.bytes > 1024 ** 3
        || !/^[a-f0-9]{64}$/.test(file.sha256) || !stat.isFile() || stat.isSymbolicLink() || stat.size !== file.bytes) throw Error('Invalid staged installer');
    const hash = createHash('sha256');
    for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
    if (hash.digest('hex') !== file.sha256) throw Error('Staged installer checksum mismatch');
    console.log(`Reverified on Windows: ${name}`);
  }
  fs.appendFileSync(process.env.GITHUB_ENV, `GAOJI_RELEASE_VERSION=${version}\nGAOJI_INSTALLER=${path.join(directory, `Gaoji-Setup-${version}.exe`)}\nGAOJI_PORTABLE_ZIP=${path.join(directory, `Gaoji-${version}-win.zip`)}\n`);
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
