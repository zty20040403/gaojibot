'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const directory = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, 'release');
const { version } = require('../package.json');
const checked = ['mac-arm64/Gaoji.app', 'win-unpacked'].map(target => JSON.parse(execFileSync(process.execPath,
  [path.join(__dirname, 'verify-package.cjs'), path.join(directory, target)], { encoding: 'utf8' })));
assert.equal(checked[0].version, version);
assert.equal(checked[1].version, version);
assert.equal(checked[0].runtimeChecksum, checked[1].runtimeChecksum);

async function main() {
  const files = [];
  for (const name of [`Gaoji-${version}-arm64.dmg`, `Gaoji-${version}-arm64-mac.zip`, `Gaoji-Setup-${version}.exe`, `Gaoji-${version}-win.zip`]) {
    const filename = path.join(directory, name);
    const stat = fs.lstatSync(filename);
    assert.ok(stat.isFile() && !stat.isSymbolicLink() && stat.size > 1024 * 1024);
    const hash = crypto.createHash('sha256');
    for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
    files.push({ name, bytes: stat.size, sha256: hash.digest('hex') });
  }
  const manifest = { schema: 1, version, channel: 'development-preview', signed: false,
    runtimeVersion: checked[0].runtimeVersion, runtimeChecksum: checked[0].runtimeChecksum, files,
    verification: 'Package/PE structure and checksums only. See QA records for runtime evidence; this verifier does not prove Windows execution or production-server acceptance.' };
  fs.writeFileSync(path.join(directory, 'SHA256SUMS'), files.map(item => `${item.sha256}  ${item.name}\n`).join(''));
  fs.writeFileSync(path.join(directory, 'release.json'), JSON.stringify(manifest, null, 2) + '\n');
  const update = { schema: 1, appId: 'com.kenneth.gaoji', version, targets: {
    'darwin-arm64': files.find(file => file.name.endsWith('.dmg')),
    'win32-x64': files.find(file => file.name.endsWith('.exe')),
  } };
  fs.writeFileSync(path.join(directory, 'app-update.json'), JSON.stringify(update, null, 2) + '\n');
  console.log(JSON.stringify(manifest));
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
