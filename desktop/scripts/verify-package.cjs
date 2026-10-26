'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { bundle } = require('../lib/deploy.cjs');

async function main() {
  const directory = path.resolve(process.argv[2]);
  const mac = directory.endsWith('.app');
  const resources = path.join(directory, mac ? 'Contents/Resources' : 'resources');
  const { listPackage, extractFile } = await import('@electron/asar');
  const archive = path.join(resources, 'app.asar');
  const files = listPackage(archive).map(name => name.replaceAll('\\', '/'));
  for (const name of ['/main.cjs', '/preload.cjs', '/dist/index.html', '/dist/brand.png', '/lib/control.cjs', '/lib/updates.cjs',
    '/lib/backup-crypto.cjs', '/lib/backup-transfer.cjs', '/lib/lifecycle.cjs', '/lib/local.cjs', '/lib/app-updater.cjs', '/lib/update-transport.cjs',
    '/node_modules/electron-updater/package.json', '/node_modules/ssh2/package.json', '/node_modules/tar/package.json',
    '/lib/applications.json', '/lib/applications.cjs', '/lib/websites.cjs', '/lib/website-content.cjs', '/lib/website-backups.cjs', '/lib/application-monitor.cjs']) {
    assert.ok(files.includes(name), 'Missing packaged file: ' + name);
  }
  for (const name of files) {
    assert.doesNotMatch(name, /(^|\/)(\.env[^/]*|\.git|tests|qa)(\/|$)/);
    assert.doesNotMatch(name, /node_modules\/cpu-features(\/|$)|\.node$/);
    assert.doesNotMatch(name, /h310-install|\.sqlite3$|secrets\.json$/);
  }
  const metadata = JSON.parse(extractFile(archive, 'package.json').toString());
  const runtime = bundle(path.join(resources, 'deployment'));
  const runtimeIndex = JSON.parse(execFileSync('tar', ['-xOf', path.join(resources, 'deployment/gaoji-runtime.tar.gz'), './manifest.json'], { encoding: 'utf8' }));
  const sourceRoot = path.resolve(__dirname, '../..');
  assert.deepEqual(extractFile(archive, 'dist/brand.png'), fs.readFileSync(path.join(sourceRoot, 'desktop/public/brand.png')),
    'Packaged icon differs from the selected artwork');
  for (const name of fs.readdirSync(path.join(sourceRoot, 'desktop/public/themes'))) {
    assert.deepEqual(extractFile(archive, 'dist/themes/' + name), fs.readFileSync(path.join(sourceRoot, 'desktop/public/themes', name)),
      'Packaged theme drawing differs from source: ' + name);
  }
  const dist = path.join(sourceRoot, 'desktop/dist');
  for (const name of ['index.html', ...fs.readdirSync(path.join(dist, 'assets')).map(file => 'assets/' + file)]) {
    assert.deepEqual(extractFile(archive, 'dist/' + name), fs.readFileSync(path.join(dist, name)),
      'Packaged interface differs from the current build: ' + name);
  }
  assert.equal(runtimeIndex.files.find(item => item.path === 'Dockerfile')?.sha256,
    createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, 'packaging/server/Dockerfile'))).digest('hex'),
    'Packaged build recipe differs from current source');
  const buildSources = JSON.parse(fs.readFileSync(path.join(sourceRoot, 'packaging/server/build-sources.lock.json'), 'utf8'));
  for (const source of buildSources.files) {
    const filename = 'vendor/' + source.name;
    assert.equal(runtimeIndex.files.find(item => item.path === filename)?.sha256, source.sha256, 'Missing pinned build source');
    const contents = execFileSync('tar', ['-xOf', path.join(resources, 'deployment/gaoji-runtime.tar.gz'), './' + filename], { maxBuffer: 25 * 1024 ** 2 });
    assert.equal(contents.length, source.bytes);
    assert.equal(createHash('sha256').update(contents).digest('hex'), source.sha256, 'Bundled build source is corrupt');
  }
  for (const name of ['ssh.cjs', 'control.cjs', 'local.cjs', 'backup-transfer.cjs', 'backup-crypto.cjs', 'app-updater.cjs', 'applications.json', 'applications.cjs', 'application-monitor.cjs', 'websites.cjs', 'website-content.cjs', 'website-backups.cjs', 'connections.cjs', 'deploy.cjs']) {
    assert.deepEqual(extractFile(archive, 'lib/' + name), fs.readFileSync(path.join(sourceRoot, 'desktop/lib', name)),
      'Packaged transport differs from current source: ' + name);
  }
  for (const name of ['static_site.py', 'content.py', 'retention.py', 'backups.py']) {
    const filename = 'src/deployment_platform/' + name;
    assert.equal(runtimeIndex.files.find(item => item.path === filename)?.sha256,
      createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, filename))).digest('hex'),
      'Packaged application adapter differs from source: ' + name);
  }
  assert.equal(runtimeIndex.files.find(item => item.path === 'applications.json')?.sha256,
    createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, 'desktop/lib/applications.json'))).digest('hex'));
  for (const name of ['bootstrap.sh', 'installer.py']) {
    assert.equal(runtime.files[name].sha256,
      createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, 'packaging/server', name))).digest('hex'),
      'Packaged installer differs from current source: ' + name);
  }
  const checkPath = 'src/local_runtime/runtime_check.py';
  assert.equal(runtimeIndex.files.find(item => item.path === checkPath)?.sha256,
    createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, checkPath))).digest('hex'),
    'Packaged image acceptance differs from current source');
  for (const name of ['readiness.py', 'desktop.py', 'capacity.py', 'storage.py', 'maintenance.py', 'qq_handover.py', 'backup_transfer.py', 'sandbox_service.py', 'sandbox_image.py', 'video_assets.py', 'components.py', 'cluster_credentials.py', 'matrix_config.py', 'imessage_config.py', 'entrypoint.py', 'instances.py', 'manager.py', 'runtime.py', 'setup_clients.py',
    'services.py', 'releases.py', 'updates.py', 'lifecycle.py', 'lifecycle_state.py', 'lifecycle_worker.py', 'purge.py', 'purge_inventory.py', 'files.py', 'update_dispatch.py', 'update_worker.py']) {
    const filename = 'src/local_runtime/' + name;
    assert.equal(runtimeIndex.files.find(item => item.path === filename)?.sha256,
      createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, filename))).digest('hex'),
      'Packaged lifecycle module differs from current source: ' + name);
  }
  for (const filename of ['src/storage_admission.py', 'src/sandbox_runtime/server.py',
    'src/plugins/ai_chat/tool_executor.py', 'src/plugins/ai_chat/media_library.py',
    'src/plugins/ai_chat/vision_worker.py', 'src/plugins/ai_chat/video_analysis.py']) {
    assert.equal(runtimeIndex.files.find(item => item.path === filename)?.sha256,
      createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, filename))).digest('hex'),
      'Packaged capacity admission differs from current source: ' + filename);
  }
  for (const name of ['bridges.py', 'onebot_delivery.py', 'config.py', 'runtime.py', '__init__.py']) {
    const filename = 'src/plugins/ai_chat/' + name;
    assert.equal(runtimeIndex.files.find(item => item.path === filename)?.sha256,
      createHash('sha256').update(fs.readFileSync(path.join(sourceRoot, filename))).digest('hex'),
      'Packaged bridge delivery differs from current source: ' + name);
  }
  if (!mac) {
    const executable = fs.readFileSync(path.join(directory, 'Gaoji.exe'));
    assert.equal(executable.subarray(0, 2).toString(), 'MZ');
    const pe = executable.readUInt32LE(0x3c);
    assert.equal(executable.subarray(pe, pe + 4).toString('hex'), '50450000');
    assert.equal(executable.readUInt16LE(pe + 4), 0x8664, 'Windows executable must be x64');
  }
  console.log(JSON.stringify({ application: directory, version: metadata.version, runtimeVersion: runtime.version,
    runtimeChecksum: runtime.files['gaoji-runtime.tar.gz'].sha256, files: files.length, runtimeFiles: runtimeIndex.files.length,
    evidence: mac ? 'archive integrity only; run Electron smoke separately' : 'x64 PE and archive integrity only; not a Windows runtime test' }));
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
