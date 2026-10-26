'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const { options, catalog, script } = require('../lib/image-policy.cjs');
const checksum = 'b'.repeat(64);
test('image catalog binds immutable images to the exact archive and architecture', () => {
  const manifest = { files: { 'gaoji-runtime.tar.gz': { sha256: checksum } } };
  assert.deepEqual(catalog(manifest).images, {});
  manifest.runtime_images = { schema: 1, checksum, images: { 'linux/amd64': {
    runtime: 'ghcr.io/example/runtime@sha256:' + 'a'.repeat(64), controller: 'ghcr.io/example/controller@sha256:' + 'c'.repeat(64) } } };
  assert.equal(Object.keys(catalog(manifest).images).length, 1);
  assert.ok(script(manifest, { source: 'tuna' }).includes('install-options.json'));
  manifest.runtime_images.checksum = 'd'.repeat(64);
  assert.throws(() => catalog(manifest));
  manifest.runtime_images.checksum = checksum;
  manifest.runtime_images.images['linux/amd64'].runtime = 'ghcr.io/example/runtime:latest';
  assert.throws(() => catalog(manifest));
});
test('download settings are a closed set, not shell or arbitrary mirror URLs', () => {
  assert.deepEqual(options(), { mode: 'auto', source: 'official' });
  for (const value of [{ mode: 'magic' }, { source: 'official; reboot' }, { source: 'https://elsewhere.invalid' }, []]) assert.throws(() => options(value));
});
