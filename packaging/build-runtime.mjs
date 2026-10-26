import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { prepareBuildSources } from './build-sources.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const build = path.join(root, 'packaging/build');
const destination = path.join(build, 'runtime');
const manifest = [];
const denied = new Set(['.env', '.git', '__pycache__', '.DS_Store', 'node_modules', 'assets', 'admin_ui_dist']);
const extensions = new Set(['.py', '.swift', '.mako', '.md', '.svg']);

function copyFile(relative, target = relative) {
  const source = path.join(root, relative);
  if (!fs.lstatSync(source).isFile()) throw new Error(`Refusing non-regular source: ${relative}`);
  const bytes = fs.readFileSync(source);
  if (bytes.length > 25 * 1024 ** 2) throw new Error(`Unexpected large source: ${relative}`);
  const filename = path.join(destination, target);
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  fs.writeFileSync(filename, bytes);
  manifest.push({ path: target, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') });
}
function copyTree(relative) {
  for (const entry of fs.readdirSync(path.join(root, relative), { withFileTypes: true })) {
    if (entry.isSymbolicLink()) throw new Error(`Refusing symlink in release input: ${relative}/${entry.name}`);
    if (denied.has(entry.name) || entry.name.startsWith('.')) continue;
    const child = `${relative}/${entry.name}`;
    if (entry.isDirectory()) copyTree(child);
    else if (entry.isFile() && extensions.has(path.extname(entry.name))) copyFile(child);
  }
}
if (!fs.existsSync(path.join(root, 'admin-ui/dist/app.js'))) throw new Error('Build the admin UI first');
// Fetch and verify once on the packaging machine, before replacing any build output.
const buildSources = prepareBuildSources(root);
fs.mkdirSync(build, { recursive: true });
fs.rmSync(destination, { recursive: true, force: true });
fs.mkdirSync(destination);
for (const name of ['src', 'migrations', 'skills']) copyTree(name);
for (const name of ['pyproject.toml', 'uv.lock', 'README.md', 'alembic.ini', 'LICENSE', 'THIRD_PARTY_NOTICES.md']) copyFile(name);
for (const name of ['app.js', 'app.css']) copyFile(`admin-ui/dist/${name}`, `src/plugins/ai_chat/admin_ui_dist/${name}`);
copyFile('packaging/server/Dockerfile', 'Dockerfile');
copyFile('packaging/server/Desktop.Dockerfile', 'Desktop.Dockerfile');
copyFile('packaging/server/images.lock.json', 'images.lock.json');
copyFile('desktop/lib/applications.json', 'applications.json');
for (const source of buildSources) copyFile(source.source, source.target);
for (const name of ['default.nix', 'build.sh', 'builder.lock.json']) copyFile(`packaging/sandbox/${name}`, `sandbox/${name}`);
copyFile('nix/sandbox-image.nix', 'sandbox/image.nix');
manifest.sort((a, b) => a.path.localeCompare(b.path));
const version = /^version\s*=\s*"([^"]+)"/m.exec(fs.readFileSync(path.join(root, 'pyproject.toml'), 'utf8'))?.[1];
if (!version) throw new Error('Missing project version');
fs.writeFileSync(path.join(destination, 'manifest.json'), JSON.stringify({ schema: 1, version, files: manifest }, null, 2) + '\n');
const archive = path.join(build, 'gaoji-runtime.tar.gz');
execFileSync('tar', ['-czf', archive, '-C', destination, '.'], { env: { ...process.env, COPYFILE_DISABLE: '1' } });
const digest = createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
fs.writeFileSync(path.join(build, 'gaoji-runtime.sha256'), `${digest}  gaoji-runtime.tar.gz\n`);
const deployment = path.join(build, 'deployment');
fs.mkdirSync(deployment, { recursive: true });
const deploymentFiles = {};
for (const name of ['bootstrap.sh', 'installer.py', 'environment.nix', 'gaoji-runtime.tar.gz']) {
  const source = name === 'gaoji-runtime.tar.gz' ? archive : path.join(root, 'packaging/server', name);
  const bytes = fs.readFileSync(source);
  fs.writeFileSync(path.join(deployment, name), bytes);
  deploymentFiles[name] = { bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
}
fs.writeFileSync(path.join(deployment, 'deployment.json'), JSON.stringify({ schema: 1, version, files: deploymentFiles,
  runtime_images: { schema: 1, checksum: digest, images: {} } }, null, 2) + '\n');
console.log(JSON.stringify({ version, files: manifest.length, archive, bytes: fs.statSync(archive).size, sha256: digest }));
