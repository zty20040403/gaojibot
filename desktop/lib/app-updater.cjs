'use strict';

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const semver = require('semver');
const { UserError } = require('./contracts.cjs');

const REPOSITORY = 'zty20040403/gaoji-app';
const RELEASES = `https://api.github.com/repos/${REPOSITORY}/releases`;
const MAX_ASSET = 1024 ** 3;
const CACHE_AGE = 7 * 86400 * 1000;
const invalid = () => new UserError('invalid_app_update', '更新清单不完整或校验不通过，已停止更新');
function releaseVersion(release) {
  const match = /^app-v(\d+\.\d+\.\d+)$/.exec(release?.tag_name || '');
  return release && !release.draft && !release.prerelease && match && semver.valid(match[1]) ? match[1] : null;
}
function assetURL(tag, name) {
  return `https://github.com/${REPOSITORY}/releases/download/${tag}/${encodeURIComponent(name)}`;
}
function releaseAsset(release, name) {
  const assets = (release.assets || []).filter(item => item.name === name && item.state === 'uploaded');
  if (assets.length !== 1 || assets[0].browser_download_url !== assetURL(release.tag_name, name)) throw invalid();
  return assets[0];
}
function validateManifest(manifest, release, platform, arch) {
  const version = releaseVersion(release);
  if (manifest?.schema !== 1 || manifest.appId !== 'com.kenneth.gaoji' || manifest.version !== version) throw invalid();
  const target = manifest.targets?.[`${platform}-${arch}`];
  const name = platform === 'darwin' && arch === 'arm64' ? `Gaoji-${version}-arm64.dmg`
    : platform === 'win32' && arch === 'x64' ? `Gaoji-Setup-${version}.exe` : null;
  if (!name || !target) throw new UserError('unsupported_app_update', '暂未发布适合这台电脑的安装包');
  if (target.name !== name || !/^[a-f0-9]{64}$/.test(target.sha256)
      || !Number.isSafeInteger(target.bytes) || target.bytes < 1 || target.bytes > MAX_ASSET) throw invalid();
  const asset = releaseAsset(release, name);
  if (asset.size !== target.bytes) throw invalid();
  if (platform === 'win32') releaseAsset(release, 'latest.yml');
  return { ...target, url: asset.browser_download_url };
}

// Downloads have one fixed origin. Redirects may only reach GitHub's asset CDN.
async function githubFetch(fetch, url, signal, redirect = 0) {
  const parsed = new URL(url);
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port
      || !['api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'].includes(parsed.hostname)) throw invalid();
  let response;
  try {
    response = await fetch(url, { signal, credentials: 'omit', redirect: 'manual',
      headers: { 'User-Agent': 'Gaoji-App-Updater', Accept: 'application/octet-stream, application/json' } });
  } catch {
    throw new UserError('app_update_network', signal?.aborted
      ? '连接更新源超时，请稍后重试；当前版本仍可使用'
      : '无法连接 GitHub 更新源，请检查系统代理或网络后重试；当前版本仍可使用');
  }
  if ([301, 302, 303, 307, 308].includes(response.status)) {
    await response.body?.cancel();
    if (redirect >= 4 || !response.headers.get('location')) throw invalid();
    return githubFetch(fetch, new URL(response.headers.get('location'), url).href, signal, redirect + 1);
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new UserError('app_update_network', response.status === 403 || response.status === 429
      ? 'GitHub 暂时限制了检查频率，请稍后再试' : '无法读取 GitHub 更新，请检查网络后重试');
  }
  return response;
}
async function readJSON(fetch, url, signal) {
  const response = await githubFetch(fetch, url, signal);
  const chunks = [];
  let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.length;
    if (bytes > 2 * 1024 * 1024) throw invalid();
    chunks.push(Buffer.from(chunk));
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { throw invalid(); }
}
async function verifyFile(filename, asset) {
  const handle = await fsp.open(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const stat = await handle.stat();
    if (!stat.isFile() || stat.size !== asset.bytes) throw invalid();
    const hash = createHash('sha256');
    for await (const chunk of handle.createReadStream({ autoClose: false })) hash.update(chunk);
    if (hash.digest('hex') !== asset.sha256) throw invalid();
  } finally { await handle.close(); }
}

class AppUpdater {
  constructor({ version, platform, arch, cache, fetch, native, openPath, canInstall, packaged }) {
    Object.assign(this, { version, platform, arch, cache, fetch, native, openPath, canInstall, packaged });
    this.state = { phase: 'idle', currentVersion: version, targetVersion: '', checkedAt: '', notes: '',
      mode: platform === 'win32' ? 'native' : 'installer', bytes: 0, total: 0, percent: 0, message: '尚未检查更新' };
    this.operation = null;
    if (native) {
      Object.assign(native, { autoDownload: false, autoInstallOnAppQuit: false, allowDowngrade: false,
        allowPrerelease: false, disableDifferentialDownload: true, logger: null });
      native.on('error', () => { if (!this.operation) this.fail(new Error()); });
      native.on('download-progress', progress => {
        if (this.state.phase === 'downloading') this.set({ bytes: progress.transferred, total: progress.total,
          percent: Math.min(100, Math.max(0, progress.percent || 0)) });
      });
    }
  }
  status() { return { ...this.state }; }
  set(value) { Object.assign(this.state, value); }
  fail(error) { this.set({ phase: 'error', message: error instanceof UserError ? error.message
    : error.code === 'ENOSPC' ? '磁盘空间不足，无法保存安装包；当前版本仍可使用'
    : '更新处理未完成，请重试；当前版本和连接配置均保留' }); return this.status(); }
  run(operation) {
    if (this.operation) return this.operation;
    this.operation = operation().catch(error => this.fail(error)).finally(() => { this.operation = null; });
    return this.operation;
  }
  async cleanCache() {
    await fsp.mkdir(this.cache, { recursive: true, mode: 0o700 });
    if ((await fsp.lstat(this.cache)).isSymbolicLink()) throw invalid();
    for (const entry of await fsp.readdir(this.cache, { withFileTypes: true })) {
      const match = /^Gaoji-(\d+\.\d+\.\d+)-arm64\.dmg$/.exec(entry.name);
      const partial = /^download-[a-f0-9-]+\.part$/.test(entry.name);
      if (!entry.isFile() || (!match && !partial)) continue;
      const filename = path.join(this.cache, entry.name);
      if (filename === this.downloaded) continue;
      const stat = await fsp.lstat(filename);
      if (partial || Date.now() - stat.mtimeMs > CACHE_AGE || match && semver.valid(match[1]) && semver.lte(match[1], this.version)) {
        await fsp.unlink(filename);
      }
    }
  }
  check() {
    return this.run(async () => {
      this.set({ phase: 'checking', message: '正在检查 App 更新', bytes: 0, total: 0, percent: 0 });
      this.release = this.asset = this.downloaded = null;
      this.set({ targetVersion: '', notes: '', checkedAt: '' });
      await this.cleanCache();
      const signal = AbortSignal.timeout(30000);
      let releases = [];
      // Only explicit App release tags belong to the desktop update channel.
      for (let page = 1; page <= 10; page++) {
        const values = await readJSON(this.fetch, `${RELEASES}?per_page=100&page=${page}`, signal);
        if (!Array.isArray(values)) throw invalid();
        releases.push(...values.filter(releaseVersion));
        if (values.length < 100) break;
        if (page === 10) throw new UserError('release_scan_limit', '发布记录过多，暂时无法确认 App 最新版本');
      }
      const release = releases.sort((a, b) => semver.rcompare(releaseVersion(a), releaseVersion(b)))[0];
      this.set({ checkedAt: new Date().toISOString() });
      if (!release) { this.set({ phase: 'no_release', message: '尚未发布 App 更新' }); return this.status(); }
      const version = releaseVersion(release);
      this.set({ targetVersion: version });
      if (semver.lte(version, this.version)) {
        this.set({ phase: 'up_to_date', message: version === this.version ? '当前已是最新发布版' : '当前版本比在线发布版更新，无需降级' });
        return this.status();
      }
      const manifest = await readJSON(this.fetch, releaseAsset(release, 'app-update.json').browser_download_url, signal);
      this.asset = validateManifest(manifest, release, this.platform, this.arch);
      this.release = release;
      this.set({ phase: 'available', notes: typeof release.body === 'string' ? release.body.slice(0, 6000) : '',
        total: this.asset.bytes, message: '发现新版本' });
      return this.status();
    });
  }
  download() {
    return this.run(async () => {
      if (!this.asset || !this.release || !['available', 'error'].includes(this.state.phase)) throw new UserError('check_app_update', '请先检查 App 更新');
      this.set({ phase: 'downloading', bytes: 0, percent: 0, message: '正在下载并校验安装包' });
      if (this.platform === 'win32') {
        if (!this.packaged || !this.native) throw new UserError('unpackaged_update', '请在已安装的 Windows App 中执行更新');
        this.native.setFeedURL({ provider: 'generic', url: assetURL(this.release.tag_name, ''), channel: 'latest', useMultipleRangeRequest: false });
        const result = await this.native.checkForUpdates();
        if (result?.updateInfo?.version !== this.state.targetVersion) throw invalid();
        const files = result.updateInfo.files;
        if (!Array.isArray(files) || files.length !== 1 || decodeURIComponent(files[0].url) !== this.asset.name
            || files[0].size !== this.asset.bytes) throw invalid();
        const downloaded = await this.native.downloadUpdate(result.cancellationToken);
        if (downloaded.length !== 1) throw invalid();
        await verifyFile(downloaded[0], this.asset);
        this.downloaded = downloaded[0];
      } else {
        await this.cleanCache();
        const filename = path.join(this.cache, this.asset.name);
        try { await verifyFile(filename, this.asset); this.downloaded = filename; } catch { /* Missing or stale downloads must be fetched again. */ }
        if (!this.downloaded) {
          const partial = path.join(this.cache, `download-${randomUUID()}.part`);
          const signal = AbortSignal.timeout(30 * 60 * 1000);
          let file;
          try {
            const response = await githubFetch(this.fetch, this.asset.url, signal);
            file = await fsp.open(partial, 'wx', 0o600);
            let bytes = 0;
            for await (const chunk of response.body) {
              bytes += chunk.length;
              if (bytes > this.asset.bytes) throw invalid();
              const buffer = Buffer.from(chunk);
              for (let offset = 0; offset < buffer.length;) offset += (await file.write(buffer, offset, buffer.length - offset)).bytesWritten;
              this.set({ bytes, percent: Math.min(99, bytes / this.asset.bytes * 100) });
            }
            await file.close(); file = null;
            await verifyFile(partial, this.asset);
            await fsp.rename(partial, filename);
            this.downloaded = filename;
          } finally { await file?.close(); await fsp.rm(partial, { force: true }); }
        }
      }
      this.set({ phase: 'ready', percent: 100, bytes: this.asset.bytes, message: '下载完成，SHA-256 校验通过' });
      return this.status();
    });
  }
  install() {
    return this.run(async () => {
      if (this.state.phase !== 'ready' || !this.downloaded || !this.asset) throw new UserError('update_not_ready', '请先下载并校验更新');
      if (!this.canInstall()) throw new UserError('app_busy', '正在安装、保存或传输数据，请等待完成再安装 App 更新');
      try { await verifyFile(this.downloaded, this.asset); }
      catch { this.downloaded = null; throw invalid(); }
      if (!this.canInstall()) throw new UserError('app_busy', '当前操作尚未结束，请稍后安装更新');
      if (this.platform === 'win32') {
        this.native.quitAndInstall(false, true);
      } else {
        const error = await this.openPath(this.downloaded);
        if (error) throw new UserError('installer_open_failed', '无法打开安装包，请重新下载后再试');
        this.set({ message: '安装包已打开：退出旧 App，将 Gaoji 拖入“应用程序”并选择“替换”' });
      }
      return this.status();
    });
  }
}
module.exports = { AppUpdater, validateManifest, releaseVersion, assetURL, REPOSITORY };
