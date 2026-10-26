'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { PassThrough } = require('node:stream');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const { bundle, launchScript, privileged, status, retry } = require('../lib/deploy.cjs');
const files = ['bootstrap.sh', 'installer.py', 'environment.nix', 'gaoji-runtime.tar.gz'];
const job = 'a'.repeat(32);

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-bundle-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const manifest = { schema: 1, version: 'test', files: {} };
  for (const name of files) {
    const data = Buffer.from('test ' + name);
    fs.writeFileSync(path.join(directory, name), data);
    manifest.files[name] = { bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') };
  }
  fs.writeFileSync(path.join(directory, 'deployment.json'), JSON.stringify(manifest));
  return { directory, manifest };
}
function clientReturning({ output = '', code = 0 } = {}) {
  const calls = [];
  return { calls, exec(command, callback) {
    const stream = new PassThrough(); stream.stderr = new PassThrough(); stream.close = () => {};
    const call = { command, stdin: '' }; calls.push(call);
    stream.end = value => {
      call.stdin = String(value || '');
      queueMicrotask(() => { if (output) stream.emit('data', Buffer.from(output)); stream.emit('close', code); });
    };
    callback(null, stream);
  } };
}
test('release inputs are hash-checked and tampering fails before upload', t => {
  const { directory, manifest } = fixture(t);
  assert.deepEqual(bundle(directory), manifest);
  fs.appendFileSync(path.join(directory, 'installer.py'), 'modified');
  assert.throws(() => bundle(directory), /校验未通过/);
});
test('launch script uses persistent namespaced service and is valid shell', t => {
  const { manifest } = fixture(t);
  const script = launchScript(job, '/tmp/gaoji-upload.1234567890', manifest);
  if (process.platform !== 'win32') execFileSync('/bin/sh', ['-n'], { input: script });
  assert.ok(script.includes(`gaoji-app-install-${job}.service`));
  assert.ok(script.includes('sha256sum --check --status'));
  assert.ok(script.includes('if test -e "$unit"; then'));
  assert.equal(/nixos-rebuild|docker (system|image) prune|systemctl restart/.test(script), false);
  assert.throws(() => launchScript('$(reboot)', '/tmp/gaoji-upload.1234567890', manifest));
  assert.throws(() => launchScript(job, '/home/user', manifest));
});
test('sudo password travels only on stdin, and unknown privilege cannot execute', async () => {
  const client = clientReturning();
  await privileged(client, 'printf test\ntrue', 'password-required', 'secret-only-in-stdin');
  assert.equal(client.calls[0].command.includes('secret-only-in-stdin'), false);
  assert.equal(client.calls[0].stdin, 'secret-only-in-stdin\n');
  if (process.platform !== 'win32') execFileSync('/bin/sh', ['-n'], { input: client.calls[0].command });
  await assert.rejects(() => privileged(client, 'true', 'unavailable', 'value'));
  assert.equal(client.calls.length, 1);
});
test('unconfirmed dispatch does not pretend failure or create another task', async () => {
  const client = clientReturning({ code: 1 });
  const value = await status(client, job);
  assert.equal(value.status, 'unconfirmed');
  assert.equal(client.calls.length, 1);
  assert.equal(client.calls[0].command.startsWith('cat '), true);
});
test('mismatched remote job status is refused', async () => {
  const client = clientReturning({ output: JSON.stringify({ schema: 1, job: 'b'.repeat(32), completed: [], status: 'running' }) });
  await assert.rejects(() => status(client, job), /无效/);
});

test('an inactive worker is interrupted only with evidence that it started', async () => {
  for (const [timestamp, expected] of [['0', 'unconfirmed'], ['12345', 'failed']]) {
    const responses = [JSON.stringify({ schema: 1, job, status: 'running', completed: [] }), 'inactive', timestamp];
    const client = { exec(command, callback) { clientReturning({ output: responses.shift() }).exec(command, callback); } };
    const result = await status(client, job);
    assert.equal(result.status, expected);
    assert.equal(responses.length, 0);
  }
});

test('resuming worker does not expose its previous failed receipt as a new failure', async () => {
  for (const [unit, expected] of [['activating', 'queued'], ['failed', 'failed'], ['inactive', 'failed']]) {
    const responses = [JSON.stringify({ schema: 1, job, status: 'failed', completed: [], message: 'previous failure' }), unit];
    const client = { exec(command, callback) { clientReturning({ output: responses.shift() }).exec(command, callback); } };
    assert.equal((await status(client, job)).status, expected);
  }
});

test('a completed package repair can retry database setup without replacing the package', async t => {
  const { directory, manifest } = fixture(t);
  const checksum = manifest.files['gaoji-runtime.tar.gz'].sha256;
  const responses = [
    JSON.stringify({ schema: 1, job, checksum, status: 'failed', stage: 'database', completed: ['prepare', 'engine', 'runtime'] }),
    'failed',
    '\n__GAOJI_OS__\nID=nixos\nVERSION_ID="26.05"\nPRETTY_NAME="Test"\n__GAOJI_ARCH__\nx86_64\n__GAOJI_UID__\n0\n__GAOJI_DISK__\n/dev/test 99999999 0 99999999 0% /\n__GAOJI_SYSTEMD__\nyes\n__GAOJI_DOCKER__\n\n__GAOJI_NIX__\n/run/current-system/sw/bin/nix\n__GAOJI_SUDO__\nroot\n__GAOJI_DONE__\n',
    '',
  ];
  const commands = [];
  const client = { exec(command, callback) {
    commands.push(command);
    clientReturning({ output: responses.shift() }).exec(command, callback);
  } };
  let saved;
  const state = await retry(client, directory, { job, checksum, previousChecksum: 'b'.repeat(64), version: 'test', startedAt: 1 }, undefined, record => { saved = record; });
  assert.equal(state.status, 'queued');
  assert.equal(commands.length, 4);
  assert.ok(commands[3].endsWith(`systemctl start --no-block gaoji-app-install-${job}.service`));
  assert.ok(commands[3].includes('install-options.json'));
  assert.equal(saved.previousChecksum, undefined);
  assert.equal(saved.checksum, checksum);
});

test('a different runtime cannot replace an instance with an existing database', async t => {
  const { directory } = fixture(t);
  const client = clientReturning({ output: JSON.stringify({ schema: 1, job, checksum: 'a'.repeat(64), status: 'failed', stage: 'database', completed: [] }) });
  await assert.rejects(retry(client, directory, { job, checksum: 'a'.repeat(64) }, undefined, () => assert.fail('no repair permitted')), { code: 'update_required' });
  assert.equal(client.calls.length, 2);
});
