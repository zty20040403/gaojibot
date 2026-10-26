'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const tar = require('tar');
const { Connections } = require('../lib/connections.cjs');
const { catalog, siteRequest, supported } = require('../lib/applications.cjs');
const { snapshot } = require('../lib/website-content.cjs');
const { launchScript } = require('../lib/deploy.cjs');
const { Websites } = require('../lib/websites.cjs');
const { PassThrough } = require('node:stream');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-applications-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const store = new Connections(path.join(directory, 'connections'), { isEncryptionAvailable: () => false });
  const server = store.save({ name: 'Existing', host: '192.0.2.10', port: 22, username: 'qa', fingerprint: 'SHA256:' + 'a'.repeat(43) }, { type: 'password', password: 'fixture' });
  return { directory, store, server };
}
const record = (job = 'a') => ({ job: job.repeat(32), checksum: 'b'.repeat(64), version: 'test', startedAt: 1 });
const request = { schema: 1, app_id: 'static-site', instance_id: 'c'.repeat(32), name: 'Website', operation: 'install', expected_revision: 0, content_sha256: 'd'.repeat(64) };

test('legacy identity survives multi-application creation, mutation and reconnection', t => {
  const { directory, store, server } = fixture(t);
  store.setDeployment(server.id, record());
  const first = store.createApplication(server.id, 'gaoji', 'Second bot');
  const second = store.createApplication(server.id, 'static-site', 'Website');
  store.setDeployment(first.id, record('c'));
  assert.equal(store.applications(server.id).length, 3);
  assert.equal(store.instance(first.id).serverId, server.id);
  assert.equal(store.instance(second.id).deployment, undefined);
  assert.equal(store.get(server.id).deployment.job, 'a'.repeat(32));
  store.save({ ...server, name: 'Renamed host' }, { type: 'password', password: 'updated' });
  const fresh = new Connections(path.join(directory, 'connections'), { isEncryptionAvailable: () => false });
  assert.equal(fresh.instance(first.id).deployment.job, 'c'.repeat(32));
  assert.equal(fresh.applications(server.id)[0].id, server.id);
  assert.equal(fresh.connectionId(second.id), server.id);
  assert.equal(JSON.stringify(fresh.applications(server.id)).includes('updated'), false);
});

test('site task cannot be attached to another instance or bot', t => {
  const { store, server } = fixture(t);
  const site = store.createApplication(server.id, 'static-site', 'Site');
  const bot = store.createApplication(server.id, 'gaoji', 'Bot');
  const value = { ...record(), application: { ...request, instance_id: site.id.slice(4).replaceAll('-', '') } };
  store.setDeployment(site.id, value);
  assert.equal(store.instance(site.id).deployment.application.instance_id, value.application.instance_id);
  assert.throws(() => store.setDeployment(bot.id, value), /不属于/);
  assert.throws(() => store.setDeployment(site.id, { ...value, application: request }), /不属于/);
  store.forget(server.id);
  assert.throws(() => store.instance(site.id), /没有找到/);
});

test('rename changes only the local app label, including legacy instances and reconnects', t => {
  const { store, server } = fixture(t);
  store.setDeployment(server.id, record());
  const site = store.createApplication(server.id, 'static-site', 'Site');
  store.renameApplication(server.id, 'My bot');
  assert.equal(store.renameApplication(site.id, 'Portfolio').name, 'Portfolio');
  assert.equal(store.get(server.id).name, 'Existing');
  assert.equal(store.get(server.id).deployment.job, record().job);
  store.save(server, { type: 'password', password: 'new-session' });
  assert.deepEqual(store.applications(server.id).map(item => item.name), ['My bot', 'Portfolio']);
  assert.throws(() => store.renameApplication(site.id, '  '));
});

test('draft removal cannot hide an existing or unconfirmed deployment', t => {
  const { store, server } = fixture(t);
  const first = store.createApplication(server.id, 'gaoji', 'Bot');
  const draft = store.createApplication(server.id, 'static-site', 'Draft');
  store.setDeployment(first.id, record());
  assert.throws(() => store.removeDraft(first.id), /已有任务/);
  assert.throws(() => store.removeDraft(server.id), /已有任务/);
  assert.deepEqual(store.removeDraft(draft.id).map(item => item.id), [first.id]);
  assert.equal(store.credentials(server.id).password, 'fixture');
  assert.equal(store.instance(first.id).deployment.job, record().job);
  assert.throws(() => store.instance(draft.id), /没有找到/);
});

test('catalog permits built-in adapters only and site requests contain no commands', () => {
  assert.deepEqual(catalog().map(item => item.id), ['gaoji', 'static-site']);
  assert.equal(supported('static-site', { os: 'ubuntu', version: '24.04', arch: 'x86_64' }), true);
  assert.equal(supported('static-site', { os: 'nixos', version: '26.05', arch: 'x86_64' }), false);
  assert.throws(() => siteRequest({ ...request, command: 'reboot' }));
  assert.throws(() => siteRequest({ ...request, operation: 'purge' }));
  assert.throws(() => siteRequest({ ...request, expected_revision: -1 }));
  const manifest = { files: Object.fromEntries(['bootstrap.sh', 'installer.py', 'environment.nix', 'gaoji-runtime.tar.gz'].map(name => [name, { sha256: 'e'.repeat(64) }])) };
  const script = launchScript('a'.repeat(32), '/tmp/gaoji-upload.1234567890', manifest, 1000, undefined, undefined, request);
  assert.ok(script.includes('website.tar.gz'));
  assert.ok(script.includes('application.json'));
  assert.ok(script.includes('"operation":"install"'));
  if (process.platform !== 'win32') require('node:child_process').execFileSync('/bin/sh', ['-n'], { input: script });
});

test('website snapshot is immutable and omits absolute source paths', async t => {
  const { directory } = fixture(t);
  const source = path.join(directory, 'site'); fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, 'index.html'), '<h1>Hello</h1>');
  const value = await snapshot(source, path.join(directory, 'cache'));
  fs.writeFileSync(path.join(source, 'index.html'), '<h1>Changed</h1>');
  const destination = path.join(directory, 'unpack'); fs.mkdirSync(destination);
  await tar.x({ file: value.archive, cwd: destination });
  assert.equal(fs.readFileSync(path.join(destination, 'index.html'), 'utf8'), '<h1>Hello</h1>');
  assert.equal(value.files, 1);
  assert.match(value.checksum, /^[a-f0-9]{64}$/);
});

test('website rejects hidden secrets, links and missing entrypoint and cleans temporary output', async t => {
  const { directory } = fixture(t);
  const source = path.join(directory, 'site'); fs.mkdirSync(source);
  const cache = path.join(directory, 'cache');
  await assert.rejects(snapshot(source, cache), /index.html/);
  fs.writeFileSync(path.join(source, 'index.html'), 'hello');
  fs.writeFileSync(path.join(source, '.env'), 'SECRET');
  await assert.rejects(snapshot(source, cache), /隐藏/);
  fs.unlinkSync(path.join(source, '.env'));
  if (process.platform !== 'win32') {
    fs.symlinkSync(path.join(source, 'index.html'), path.join(source, 'link.html'));
    await assert.rejects(snapshot(source, cache), /链接/);
  }
  assert.deepEqual(fs.readdirSync(cache), []);
});

test('cache cleanup preserves selected and pending archives, then removes settled copies', async t => {
  const { directory, store, server } = fixture(t);
  const cache = path.join(directory, 'cache');
  const source = path.join(directory, 'source'); fs.mkdirSync(source);
  fs.writeFileSync(path.join(source, 'index.html'), 'test');
  const site = store.createApplication(server.id, 'static-site', 'Site');
  const websites = new Websites(cache, 'unused', store, () => { throw Error('No SSH in this test'); });
  const selected = await websites.select(source);
  const archive = path.join(cache, selected.checksum + '.tar.gz');
  websites.cleanup(); assert.ok(fs.existsSync(archive));
  const deployment = { ...record(), application: { ...request, instance_id: site.id.slice(4).replaceAll('-', ''), content_sha256: selected.checksum } };
  store.setDeployment(site.id, deployment);
  const restarted = new Websites(cache, 'unused', store, null);
  assert.ok(fs.existsSync(archive));
  store.setDeployment(site.id, { ...deployment, settled: true });
  restarted.cleanup(); assert.equal(fs.existsSync(archive), false);
});

test('an old completed poll cannot overwrite a newer submitted job or discard its upload', async t => {
  const { directory, store, server } = fixture(t);
  const cache = path.join(directory, 'cache');
  const site = store.createApplication(server.id, 'static-site', 'Site');
  const old = record('a');
  const next = record('c');
  store.setDeployment(site.id, old);
  let completeRead;
  let calls = 0;
  const client = { exec(_command, callback) {
    const stream = new PassThrough(); stream.stderr = new PassThrough(); stream.close = () => {};
    stream.end = () => {
      const respond = output => { stream.emit('data', Buffer.from(output)); stream.emit('close', 0); };
      if (++calls === 1) queueMicrotask(() => respond('null'));
      else completeRead = () => respond(JSON.stringify({ schema: 1, job: old.job, status: 'completed', completed: [] }));
    };
    callback(null, stream);
  } };
  const websites = new Websites(cache, '', store, async () => client);
  let cleaned = false;
  websites.cleanup = () => { cleaned = true; };
  const pending = websites.status(site.id);
  while (!completeRead) await new Promise(resolve => setImmediate(resolve));
  store.setDeployment(site.id, next);
  completeRead();
  await pending;
  assert.equal(store.instance(site.id).deployment.job, next.job);
  assert.equal(store.instance(site.id).deployment.settled, undefined);
  assert.equal(cleaned, false);
});
