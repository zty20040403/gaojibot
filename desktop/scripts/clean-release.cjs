'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const semver = require('semver');

function cleanup(keep, apply = false) {
  if (!semver.valid(keep) || !/^\d+\.\d+\.\d+$/.test(keep)) throw Error('Invalid retained version');
  const root = path.resolve(__dirname, '../release');
  const current = JSON.parse(fs.readFileSync(path.join(root, keep, 'release.json'), 'utf8'));
  if (current.version !== keep) throw Error('Retained release identity mismatch');
  const ps = spawnSync('ps', ['-axo', 'command='], { encoding: 'utf8', maxBuffer: 8 * 1024 ** 2 });
  if (ps.status !== 0) throw Error('Cannot inspect running processes; no cleanup performed');
  const processes = ps.stdout;
  const remove = filename => {
    if (fs.lstatSync(filename).isSymbolicLink()) return;
    console.log(`${apply ? 'Removing' : 'Would remove'}: ${filename}`);
    if (apply) fs.rmSync(filename, { recursive: true, force: false });
  };
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^\d+\.\d+\.\d+$/.test(entry.name) || !semver.lt(entry.name, keep)) continue;
    const folder = path.join(root, entry.name);
    let manifest;
    try { manifest = JSON.parse(fs.readFileSync(path.join(folder, 'release.json'), 'utf8')); } catch { continue; }
    if (manifest.version !== entry.name || !Array.isArray(manifest.files)) continue;
    if (processes.includes(folder + path.sep) || processes.includes(`/Volumes/Gaoji ${entry.name}-`)) {
      console.log('Kept running release: ' + entry.name); continue;
    }
    // Retain tiny manifests/notes as evidence, but not old installers or App copies.
    const binaries = [`Gaoji-${entry.name}-arm64.dmg`, `Gaoji-${entry.name}-arm64-mac.zip`,
      `Gaoji-Setup-${entry.name}.exe`, `Gaoji-${entry.name}-win.zip`];
    const allowed = new Set([...binaries, ...binaries.map(name => name + '.blockmap'), 'mac-arm64', 'win-unpacked']);
    for (const item of fs.readdirSync(folder)) if (allowed.has(item)) remove(path.join(folder, item));
  }
  // Renderer screenshots are reproducible fixtures, not user data or QA source.
  const qa = path.join(root, 'qa');
  if (fs.existsSync(qa) && !fs.lstatSync(qa).isSymbolicLink() && !/tests\/verify-[^\s]+\.cjs/.test(processes)) {
    for (const entry of fs.readdirSync(qa, { withFileTypes: true })) {
      const filename = path.join(qa, entry.name);
      if (entry.isFile() && entry.name.endsWith('.png')) remove(filename);
      const match = /^workspace-(\d+\.\d+\.\d+)$/.exec(entry.name);
      if (!entry.isDirectory() || !match || !semver.valid(match[1]) || semver.gt(match[1], keep)) continue;
      const fixtures = fs.readdirSync(filename, { withFileTypes: true });
      if (fixtures.every(file => file.isFile() && (file.name.endsWith('.png') || file.name === 'diagnostics.json'))) remove(filename);
    }
  }
  const build = path.resolve(__dirname, '../../packaging/build');
  for (const name of ['linux-qa-ubuntu', 'linux-qa-ubuntu-restore-target', 'linux-qa-debian', 'linux-qa-debian-restore-target']) {
    const folder = path.join(build, name);
    if (!fs.existsSync(folder) || fs.lstatSync(folder).isSymbolicLink() || processes.includes(folder)) continue;
    let marker;
    try { marker = JSON.parse(fs.readFileSync(path.join(folder, 'vm.json'), 'utf8')); } catch { continue; }
    if (marker.schema !== 1 || !/^gaoji-local-qa-[a-f0-9]{32}$/.test(marker.id)) continue;
    remove(folder);
  }
}

if (require.main === module) {
  try {
    if (process.argv[2] !== '--keep' || !process.argv[3]) throw Error('Usage: --keep VERSION [--apply]');
    cleanup(process.argv[3], process.argv.includes('--apply'));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}

module.exports = { cleanup };
