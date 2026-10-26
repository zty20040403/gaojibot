'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { createHash } = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const { pipeline } = require('node:stream/promises');
const { REPOSITORY } = require('../lib/app-updater.cjs');
const { version } = require('../package.json');

async function main() {
  if (process.argv[2] !== '--stage' || !process.argv[3]) throw Error('Usage: --stage RELEASE_DIRECTORY');
  const directory = path.resolve(process.argv[3]);
  const raw = fs.readFileSync(path.join(directory, 'release.json'));
  const manifest = JSON.parse(raw);
  const names = [`Gaoji-${version}-arm64.dmg`, `Gaoji-${version}-arm64-mac.zip`, `Gaoji-Setup-${version}.exe`, `Gaoji-${version}-win.zip`];
  if (REPOSITORY !== 'zty20040403/gaoji-app' || manifest.version !== version || manifest.files.length !== 4 || new Set(manifest.files.map(file => file.name)).size !== 4) throw Error('Invalid manifest');
  for (const file of manifest.files) {
    if (!names.includes(file.name)) throw Error('Unexpected release file');
    const filename = path.join(directory, file.name);
    const stat = fs.lstatSync(filename);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== file.bytes) throw Error('Invalid installer');
    const hash = createHash('sha256');
    for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
    if (hash.digest('hex') !== file.sha256) throw Error('Installer hash mismatch');
  }
  const credential = spawnSync('git', ['credential', 'fill'], { input: 'protocol=https\nhost=github.com\n\n', encoding: 'utf8', timeout: 30000,
    env: { ...process.env, GIT_TERMINAL_PROMPT: '0' } });
  const token = credential.stdout?.split('\n').find(line => line.startsWith('password='))?.slice(9);
  if (credential.status !== 0 || !token) throw Error('GitHub credential unavailable');
  async function api(route, method = 'GET', body, optional = false) {
    const response = await fetch(`https://api.github.com/repos/${REPOSITORY}${route}`, { method,
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(60000), redirect: 'error' });
    if (optional && response.status === 404) return null;
    if (!response.ok) throw Error(`GitHub ${method}: HTTP ${response.status}`);
    return response.status === 204 ? null : response.json();
  }
  const tag = `app-v${version}`;
  const release = (await api('/releases?per_page=100')).find(item => item.tag_name === tag);
  if (!release?.draft) throw Error('An existing unpublished draft is required');
  const workflow = fs.readFileSync(path.join(__dirname, 'assemble-release.yml'));
  const route = '/contents/.github/workflows/assemble-release.yml';
  const old = await api(route, 'GET', undefined, true);
  if (!old || !Buffer.from(old.content, 'base64').equals(workflow)) throw Error('GitHub assembly workflow differs; review and authorize workflow changes separately');
  const assets = await api(`/releases/${release.id}/assets?per_page=100`);
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-release-stage-'));
  try {
    const chunkSize = 8 * 1024 ** 2;
    for (const file of manifest.files) {
      for (let index = 0; index < Math.ceil(file.bytes / chunkSize); index++) {
        const name = `${file.name}.part-${String(index).padStart(3, '0')}`;
        const filename = path.join(temp, name);
        await pipeline(fs.createReadStream(path.join(directory, file.name), { start: index * chunkSize, end: Math.min(file.bytes, (index + 1) * chunkSize) - 1 }), fs.createWriteStream(filename, { mode: 0o600 }));
        const bytes = fs.statSync(filename).size;
        const hash = createHash('sha256');
        for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
        const digest = 'sha256:' + hash.digest('hex');
        const existing = assets.find(item => item.name === name);
        if (existing?.state === 'uploaded' && existing.size === bytes && existing.digest === digest) {
          fs.unlinkSync(filename); console.log(`Already verified: ${name}`); continue;
        }
        if (existing?.state === 'starter') await api(`/releases/assets/${existing.id}`, 'DELETE');
        else if (existing) throw Error('Conflicting staged asset');
        console.log(`Uploading: ${name}`);
        const result = await new Promise((resolve, reject) => {
          const child = spawn('curl', ['--silent', '--show-error', '--fail', '--http1.1', '--limit-rate', '2M', '--config', '-', '--request', 'POST',
            '--header', 'Content-Type: application/octet-stream', '--header', `Content-Length: ${bytes}`, '--connect-timeout', '20', '--max-time', '180',
            '--write-out', '%{stderr}\nHTTP %{http_code}; uploaded %{size_upload} bytes in %{time_total}s\n',
            '--upload-file', filename, `https://uploads.github.com/repos/${REPOSITORY}/releases/${release.id}/assets?name=${encodeURIComponent(name)}`]);
          const chunks = []; let length = 0;
          child.stdout.on('data', chunk => { length += chunk.length; if (length > 1024 ** 2) child.kill(); else chunks.push(chunk); });
          let diagnostic = '';
          child.stderr.on('data', chunk => { diagnostic = (diagnostic + chunk.toString()).slice(-1000); });
          child.on('error', () => reject(Error('Upload could not start')));
          child.stdin.on('error', () => {});
          child.stdin.end('header = ' + JSON.stringify(`Authorization: Bearer ${token}`) + '\n');
          child.on('close', code => {
            if (code) {
              const detail = diagnostic.replaceAll(token, '[redacted]').replace(/https?:\/\/\S+/g, '[URL]').replace(/[\r\n]+/g, ' ').slice(0, 400);
              return reject(Error(`Part upload failed: curl ${code}: ${detail}`));
            }
            try { resolve(JSON.parse(Buffer.concat(chunks))); } catch { reject(Error('Invalid upload response')); }
          });
        });
        if (result.state !== 'uploaded' || result.size !== bytes || result.digest !== digest) throw Error('Part hash mismatch');
        fs.unlinkSync(filename);
      }
    }
    const repo = await api('');
    await api('/actions/workflows/assemble-release.yml/dispatches', 'POST', { ref: repo.default_branch,
      inputs: { tag, manifest_sha256: createHash('sha256').update(raw).digest('hex') } });
    console.log('Staged and verified; GitHub assembly dispatched. Release remains a draft.');
  } finally { fs.rmSync(temp, { recursive: true, force: true }); }
}
main().catch(error => { console.error(/^(GitHub |Usage:|Invalid |Unexpected |Installer |An existing |Conflicting |Upload |Part )/.test(error.message) ? error.message : 'Staging did not complete; draft remains unpublished'); process.exitCode = 1; });
