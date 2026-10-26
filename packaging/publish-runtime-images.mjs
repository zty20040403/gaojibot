// Run only after explicit approval to publish this allowlisted runtime bundle.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
const require = createRequire(import.meta.url);
const { bundle } = require('../desktop/lib/deploy.cjs');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const value = name => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : ''; };
const registry = value('--registry'), host = value('--docker-host');
if (!args.includes('--publish') || !/^ghcr\.io\/[a-z0-9][a-z0-9-]*\/[a-z0-9][a-z0-9._-]*$/.test(registry) || !host) {
  throw new Error('Required: --publish --registry ghcr.io/OWNER/NAME --docker-host EXPLICIT_DOCKER_ENDPOINT');
}
const directory = path.join(root, 'packaging/build/deployment');
const manifest = bundle(directory), checksum = manifest.files['gaoji-runtime.tar.gz'].sha256;
const images = {};
const docker = value('--docker') || 'docker';
for (const arch of ['amd64', 'arm64']) {
  images['linux/' + arch] = {};
  for (const [kind, file] of [['runtime', 'Dockerfile'], ['controller', 'Desktop.Dockerfile']]) {
    const name = `${registry}-${kind}`, tag = `${name}:${checksum}-${arch}`;
    const metadataFile = path.join(root, 'packaging/build', `${kind}-${arch}-image.json`);
    execFileSync(docker, ['--host', host, 'buildx', 'build', '--platform', 'linux/' + arch,
      '--label', `gaoji.desktop.runtime-digest=${checksum}`, '--provenance=false', '--tag', tag,
      '--file', file, '--push', '--metadata-file', metadataFile, '-'], {
      input: fs.readFileSync(path.join(directory, 'gaoji-runtime.tar.gz')), stdio: ['pipe', 'inherit', 'inherit'], timeout: 7200000,
    });
    const metadata = JSON.parse(fs.readFileSync(metadataFile, 'utf8'));
    const digest = metadata['containerimage.digest'];
    if (!/^sha256:[a-f0-9]{64}$/.test(digest)) throw new Error('Registry did not return a pinned digest');
    images['linux/' + arch][kind] = name + '@' + digest;
  }
}
const updated = { ...manifest, runtime_images: { schema: 1, checksum, images } };
fs.writeFileSync(path.join(directory, 'deployment.json'), JSON.stringify(updated, null, 2) + '\n');
bundle(directory);
console.log('Published pinned amd64/arm64 images. Package the App now WITHOUT rebuilding the runtime archive.');
