'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function profile(distribution = '', slot = '') {
  return spawnSync(process.execPath, ['-e', 'console.log(require("./scripts/local-vm.cjs").directory)'], {
    cwd: path.resolve(__dirname, '..'), encoding: 'utf8',
    env: { ...process.env, GAOJI_QA_DISTRIBUTION: distribution, GAOJI_QA_SLOT: slot },
  });
}

test('existing primary QA directory stays unchanged', () => {
  const result = profile();
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), path.resolve(__dirname, '../../packaging/build/linux-qa-ubuntu'));
});

test('fresh restore target and distribution records cannot collide', () => {
  const directories = new Set();
  for (const distribution of ['ubuntu', 'debian']) {
    for (const slot of ['primary', 'restore-target']) {
      const result = profile(distribution, slot);
      assert.equal(result.status, 0, result.stderr);
      directories.add(result.stdout.trim());
    }
  }
  assert.equal(directories.size, 4);
});

test('remote host names and path traversal are not QA targets', () => {
  for (const [distribution, slot] of [['h310', 'primary'], ['ubuntu', 'h310'],
    ['../ubuntu', 'primary'], ['ubuntu', '../primary']]) {
    const result = profile(distribution, slot);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Choose a fixed/);
    assert.equal(result.stdout, '');
  }
});
