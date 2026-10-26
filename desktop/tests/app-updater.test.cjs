'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { createHash } = require('node:crypto');
const { EventEmitter } = require('node:events');
const { AppUpdater, releaseVersion, validateManifest, assetURL } = require('../lib/app-updater.cjs');

const bytes = Buffer.from('test-only-installer');
const sha256 = createHash('sha256').update(bytes).digest('hex');
test('desktop update assets use the dedicated distribution repository', () => {
  assert.equal(assetURL('app-v0.1.25', 'app-update.json'),
    'https://github.com/zty20040403/gaoji-app/releases/download/app-v0.1.25/app-update.json');
});
function fixture(platform = 'darwin') {
  const name = platform === 'darwin' ? 'Gaoji-0.1.23-arm64.dmg' : 'Gaoji-Setup-0.1.23.exe';
  const target = { name, bytes: bytes.length, sha256 };
  const manifest = { schema: 1, version: '0.1.23', appId: 'com.kenneth.gaoji',
    targets: { [platform === 'darwin' ? 'darwin-arm64' : 'win32-x64']: target } };
  const release = { tag_name: 'app-v0.1.23', draft: false, prerelease: false, body: 'fixture notes',
    assets: [name, 'app-update.json', ...(platform === 'win32' ? ['latest.yml'] : [])].map(name => ({
      name, state: 'uploaded', size: bytes.length, browser_download_url: assetURL('app-v0.1.23', name),
    })) };
  return { target, manifest, release };
}
async function setup(t, options = {}) {
  const cache = await fs.mkdtemp(path.join(os.tmpdir(), 'gaoji-app-update-'));
  t.after(() => fs.rm(cache, { recursive: true, force: true }));
  const data = fixture(options.platform);
  let calls = 0;
  const updater = new AppUpdater({ version: '0.1.22', platform: 'darwin', arch: 'arm64', cache, packaged: true,
    canInstall: () => true, openPath: async () => '', fetch: async url => {
      calls++;
      if (url.includes('api.github.com')) return Response.json([ { tag_name: 'v0.20.0' }, data.release ]);
      if (url.endsWith('app-update.json')) return Response.json(data.manifest);
      return new Response(bytes);
    }, ...options });
  return { updater, cache, data, calls: () => calls };
}
test('App tags are separate from bot versions; unsafe/mismatched manifests rejected', () => {
  assert.equal(releaseVersion({ tag_name: 'v99.0.0' }), null);
  assert.equal(releaseVersion({ tag_name: 'app-v0.1.23', draft: true }), null);
  assert.equal(releaseVersion(null), null);
  const { manifest, release } = fixture();
  assert.equal(validateManifest(manifest, release, 'darwin', 'arm64').sha256, sha256);
  assert.throws(() => validateManifest({ ...manifest, appId: 'another.app' }, release, 'darwin', 'arm64'));
  release.assets[0].browser_download_url = 'http://localhost/installer.dmg';
  assert.throws(() => validateManifest(manifest, release, 'darwin', 'arm64'));
});
test('check is opt-in, selects app version, verified download opens installer only on request', async t => {
  const opened = [];
  const { updater, calls } = await setup(t, { openPath: async filename => { opened.push(filename); return ''; } });
  assert.equal(calls(), 0);
  assert.equal((await updater.check()).phase, 'available');
  assert.equal((await updater.download()).phase, 'ready');
  assert.equal(opened.length, 0);
  assert.match((await updater.install()).message, /安装包已打开/);
  assert.equal(opened.length, 1);
  await fs.writeFile(updater.downloaded, 'tampered');
  assert.equal((await updater.install()).phase, 'error');
  assert.equal(opened.length, 1);
});
test('empty release list, network failure and current version remain distinct', async t => {
  const { updater } = await setup(t, { fetch: async () => Response.json([]) });
  assert.equal((await updater.check()).phase, 'no_release');
  updater.fetch = async () => { throw new Error('secret-token-in-network-error'); };
  const failed = await updater.check();
  assert.equal(failed.phase, 'error');
  assert.doesNotMatch(JSON.stringify(failed), /secret-token/);
  updater.fetch = async () => Response.json([{ tag_name: 'app-v0.1.21' }]);
  assert.match((await updater.check()).message, /无需降级/);
});
test('wrong hash/length cannot be installed; partials removed and unrelated cache files retained', async t => {
  const { updater, cache } = await setup(t);
  await updater.check();
  await fs.writeFile(path.join(cache, 'user-file.txt'), 'keep');
  updater.fetch = async () => new Response(Buffer.alloc(bytes.length));
  assert.equal((await updater.download()).phase, 'error');
  assert.deepEqual(await fs.readdir(cache), ['user-file.txt']);
  await fs.writeFile(path.join(cache, 'Gaoji-0.1.20-arm64.dmg'), bytes);
  await updater.cleanCache();
  assert.deepEqual(await fs.readdir(cache), ['user-file.txt']);
});
test('an install in progress blocks app replacement and requests are coalesced', async t => {
  let finish;
  const { updater } = await setup(t, { canInstall: () => false });
  await updater.check(); await updater.download();
  assert.match((await updater.install()).message, /请等待完成/);
  updater.fetch = () => new Promise(resolve => { finish = () => resolve(Response.json([])); });
  const first = updater.check();
  assert.equal(updater.check(), first);
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  finish(); await first;
});
test('Windows uses pinned app release, validates downloaded bytes and only explicitly restarts', async t => {
  const native = new EventEmitter();
  const { updater, cache, data } = await setup(t, { platform: 'win32', arch: 'x64', native });
  let installed = false;
  native.setFeedURL = value => assert.match(value.url, /app-v0\.1\.23\/$/);
  native.checkForUpdates = async () => ({ updateInfo: { version: '0.1.23', files: [{ url: encodeURIComponent(data.target.name), size: bytes.length }] } });
  native.downloadUpdate = async () => { const filename = path.join(cache, 'installer.exe'); await fs.writeFile(filename, bytes); return [filename]; };
  native.quitAndInstall = () => { installed = true; };
  assert.equal(native.autoDownload, false);
  assert.equal(native.autoInstallOnAppQuit, false);
  await updater.check();
  assert.equal((await updater.download()).phase, 'ready');
  assert.equal(installed, false);
  await updater.install();
  assert.equal(installed, true);
});
test('asset redirects cannot download from arbitrary or insecure hosts', async t => {
  const { updater } = await setup(t);
  await updater.check();
  updater.fetch = async () => new Response(null, { status: 302, headers: { Location: 'http://127.0.0.1/payload' } });
  assert.equal((await updater.download()).phase, 'error');
  assert.equal(updater.downloaded, null);
});
