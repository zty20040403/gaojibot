'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');

test('source is hash-verified, cached and included without deployment-time GitHub download', async t => {
  const { prepareBuildSources } = await import('../../packaging/build-sources.mjs');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-build-source-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const data = Buffer.from('fixture-source-archive');
  const source = { name: 'whisper.cpp-v1.9.4.tar.gz', url: 'https://codeload.github.com/test/source',
    bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') };
  fs.mkdirSync(path.join(root, 'packaging/server'), { recursive: true });
  fs.writeFileSync(path.join(root, 'packaging/server/build-sources.lock.json'), JSON.stringify({ schema: 1, files: [source] }));
  let downloads = 0;
  const download = (_, filename) => { downloads++; fs.writeFileSync(filename, data); };
  const [item] = prepareBuildSources(root, download);
  assert.equal(item.target, 'vendor/' + source.name);
  prepareBuildSources(root, download);
  assert.equal(downloads, 1);
  fs.writeFileSync(path.join(root, item.source), 'corrupt');
  assert.throws(() => prepareBuildSources(root, (_, filename) => fs.writeFileSync(filename, 'wrong')), /checksum mismatch/);
  assert.ok(fs.readdirSync(path.join(root, 'packaging/build/source-cache')).every(name => !name.endsWith('.part')));
  prepareBuildSources(root, download);
  assert.equal(downloads, 2);
  const dockerfile = fs.readFileSync(path.resolve(__dirname, '../../packaging/server/Dockerfile'), 'utf8');
  assert.match(dockerfile, /COPY vendor\/whisper\.cpp-v1\.9\.4\.tar\.gz/);
  assert.match(dockerfile, /sha256sum --check/);
  assert.doesNotMatch(dockerfile, /codeload\.github\.com|--max-time 180/);
});
