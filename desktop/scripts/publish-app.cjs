'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const semver = require('semver');
const { REPOSITORY } = require('../lib/app-updater.cjs');
const { version } = require('../package.json');

async function main() {
  const prepare = process.argv[2] === '--prepare-draft';
  if ((!prepare && process.argv[2] !== '--publish') || !process.argv[3]) throw new Error('Usage: node scripts/publish-app.cjs --publish|--prepare-draft RELEASE_DIRECTORY');
  const directory = path.resolve(process.argv[3]);
  const notesPath = path.join(directory, 'release-notes.md');
  const notesStat = fs.lstatSync(notesPath);
  if (!notesStat.isFile() || notesStat.isSymbolicLink() || notesStat.size > 32768) throw new Error('Release notes are invalid');
  const notes = fs.readFileSync(notesPath, 'utf8').trim();
  if (!notes || !notes.includes(version)) throw new Error('Release notes must describe the current version');
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'release.json'), 'utf8'));
  const update = JSON.parse(fs.readFileSync(path.join(directory, 'app-update.json'), 'utf8'));
  if (manifest.version !== version || update.version !== version || update.appId !== 'com.kenneth.gaoji') throw new Error('Release version mismatch');
  const allowed = [`Gaoji-${version}-arm64.dmg`, `Gaoji-${version}-arm64-mac.zip`, `Gaoji-Setup-${version}.exe`, `Gaoji-${version}-win.zip`,
    'latest.yml', 'app-update.json', 'release.json', 'SHA256SUMS'];
  const files = [];
  for (const name of allowed) {
    const filename = path.join(directory, name);
    const stat = fs.lstatSync(filename);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Invalid release asset');
    const hash = createHash('sha256');
    for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
    const sha256 = hash.digest('hex');
    const entry = manifest.files.find(item => item.name === name);
    if (entry && (entry.bytes !== stat.size || entry.sha256 !== sha256)) throw new Error('Release checksum mismatch');
    files.push({ name, filename, bytes: stat.size, sha256 });
  }
  // Use the configured Git credential helper in memory; never log or persist credentials.
  const credential = spawnSync('git', ['credential', 'fill'], { input: 'protocol=https\nhost=github.com\n\n', encoding: 'utf8',
    timeout: 30000, maxBuffer: 65536, env: { ...process.env, GIT_TERMINAL_PROMPT: '0' } });
  if (credential.status !== 0) throw new Error('GitHub login is required; no release was published');
  const token = credential.stdout.split('\n').find(line => line.startsWith('password='))?.slice(9);
  if (!token) throw new Error('GitHub credential unavailable');
  async function api(route, method = 'GET', body) {
    const response = await fetch(`https://api.github.com/repos/${REPOSITORY}${route}`, { method,
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28' },
      body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(45000), redirect: 'error' });
    if (!response.ok) throw new Error(`GitHub API ${method} failed (HTTP ${response.status})`);
    return response.status === 204 ? null : response.json();
  }
  const releases = await api('/releases?per_page=100');
  const newer = releases.find(item => !item.draft && !item.prerelease &&
    semver.valid(item.tag_name.replace(/^app-v/, '')) && semver.gt(item.tag_name.replace(/^app-v/, ''), version));
  if (newer) throw new Error('Release version is older than an existing public version');
  const tag = `app-v${version}`;
  let release = releases.find(value => value.tag_name === tag);
  if (release && !release.draft) throw new Error('This App release is already public; published artifacts will not be overwritten');
  if (!release) {
    const repo = await api('');
    const commit = await api(`/commits/${encodeURIComponent(repo.default_branch)}`);
    release = await api('/releases', 'POST', { tag_name: tag, target_commitish: commit.sha, name: `Gaoji App ${version}`, draft: true, prerelease: false,
      body: notes });
  } else if (release.body !== notes) {
    release = await api(`/releases/${release.id}`, 'PATCH', { body: notes });
  }
  for (const file of files.filter(file => !prepare || !manifest.files.some(item => item.name === file.name)).sort((a, b) => a.bytes - b.bytes)) {
    const existing = (await api(`/releases/${release.id}/assets?per_page=100`)).find(item => item.name === file.name);
    if (existing) {
      if (existing.state === 'uploaded' && existing.size === file.bytes && existing.digest === `sha256:${file.sha256}`) {
        console.log('Already verified: ' + file.name); continue;
      }
      if (existing.state === 'starter' && !existing.digest && (existing.size === 0 || existing.size === file.bytes)
          && (Date.now() - Date.parse(existing.updated_at) > 120000 || process.argv.includes('--resume-incomplete'))) {
        await api(`/releases/assets/${existing.id}`, 'DELETE');
      } else throw new Error('Draft has an unverified/conflicting asset; refusing to replace it');
    }
    console.log('Uploading: ' + file.name);
    const result = await new Promise((resolve, reject) => {
      const curl = 'curl';
      const upload = spawn(curl, ['--silent', '--show-error', '--fail', '--http2', '--config', '-', '--request', 'POST',
        '--header', 'Content-Type: application/octet-stream', '--header', `Content-Length: ${file.bytes}`,
        '--connect-timeout', '20', '--max-time', '1200', '--upload-file', file.filename,
        '--write-out', '%{stderr}\nHTTP %{http_code}; uploaded %{size_upload} bytes in %{time_total}s\n',
        `https://uploads.github.com/repos/${REPOSITORY}/releases/${release.id}/assets?name=${encodeURIComponent(file.name)}`],
      { stdio: ['pipe', 'pipe', 'pipe'] });
      const chunks = []; let size = 0;
      let diagnostic = '';
      upload.stderr.on('data', chunk => { diagnostic = (diagnostic + chunk.toString()).slice(-1000); });
      upload.stdout.on('data', chunk => { size += chunk.length; if (size > 1024 * 1024) upload.kill(); else chunks.push(chunk); });
      upload.on('error', () => reject(new Error('Asset upload could not start; draft kept unpublished')));
      upload.stdin.on('error', () => {});
      upload.stdin.end('header = ' + JSON.stringify(`Authorization: Bearer ${token}`) + '\n');
      upload.on('close', code => {
        if (code !== 0) {
          const detail = diagnostic.replaceAll(token, '[redacted]').replace(/https?:\/\/\S+/g, '[URL]').replace(/[\r\n]+/g, ' ').slice(0, 400);
          return reject(new Error(`Asset upload failed (curl ${code}): ${detail}; draft kept unpublished`));
        }
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
        catch { reject(new Error('Asset upload response invalid; draft kept unpublished')); }
      });
    });
    if (result.name !== file.name || result.state !== 'uploaded' || result.size !== file.bytes || result.digest !== `sha256:${file.sha256}`) throw new Error('Uploaded asset integrity not confirmed; draft kept unpublished');
    console.log('Verified: ' + file.name);
  }
  if (prepare) {
    console.log('Verified draft metadata; installers not uploaded and release remains unpublished.');
    return;
  }
  const finalAssets = await api(`/releases/${release.id}/assets?per_page=100`);
  if (finalAssets.length !== files.length || !files.every(file => finalAssets.some(asset =>
    asset.name === file.name && asset.state === 'uploaded' && asset.size === file.bytes && asset.digest === `sha256:${file.sha256}`))) {
    throw new Error('Uploaded asset inventory differs from the release allowlist; draft kept unpublished');
  }
  const publicRelease = await api(`/releases/${release.id}`, 'PATCH', { tag_name: tag, draft: false, make_latest: 'true' });
  if (publicRelease.draft || publicRelease.tag_name !== tag) throw new Error('Release publication or version tag unconfirmed');
  console.log('Published: ' + publicRelease.html_url);
  const cleanup = spawnSync(process.execPath, [path.join(__dirname, 'clean-release.cjs'), '--keep', version, '--apply'], { stdio: 'inherit' });
  if (cleanup.status !== 0) console.warn('Release is public; local cleanup needs attention. No running App was deliberately removed.');
}
main().catch(error => {
  // Network errors may contain signed redirect URLs; only print controlled messages.
  const safe = /^(Usage:|Release |Invalid release|GitHub |This App|Draft |Asset upload|Uploaded asset)/.test(error.message);
  const code = String(error.cause?.code || error.code || '').replace(/[^A-Z0-9_]/g, '').slice(0, 80);
  console.error(safe ? error.message : `Publication did not complete (${code || 'network or transport error'}). Existing releases and source were not changed.`);
  process.exitCode = 1;
});
