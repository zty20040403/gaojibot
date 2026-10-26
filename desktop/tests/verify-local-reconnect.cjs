'use strict';

// Real local Docker lifecycle, synthetic HTTP controller, no QQ/database/model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { randomBytes } = require('node:crypto');
const { LocalDeployment } = require('../lib/local.cjs');

async function main() {
  const image = process.env.GAOJI_TEST_DESKTOP_CONTROLLER;
  assert.match(image || '', /^gaoji-desktop-controller:[a-f0-9]{64}$/);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-reconnect-docker-'));
  const local = new LocalDeployment(directory, path.join(directory, 'deliberately-missing-bundle'), {
    isEncryptionAvailable: () => true, decryptString: value => value.toString(),
  });
  const job = randomBytes(16).toString('hex'), token = randomBytes(32).toString('base64url');
  const names = { volume: `gaoji-desktop-${job}-data`, network: `gaoji-desktop-${job}`, container: `gaoji-desktop-${job}-controller` };
  const record = { schema: 1, job, checksum: image.split(':')[1], credential: Buffer.from(token).toString('base64') };
  const label = `gaoji.desktop.job=${job}`;
  try {
    const info = JSON.parse(await local.checked(['info', '--format', '{{json .}}']));
    assert.match(info.OperatingSystem, /docker desktop/i);
    const metadata = JSON.parse(await local.checked(['image', 'inspect', image]))[0];
    assert.equal(metadata.Config.Labels['gaoji.desktop.runtime-digest'], record.checksum);
    const listener = net.createServer();
    await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
    record.port = listener.address().port;
    await new Promise(resolve => listener.close(resolve));
    record.botPort = record.port === 54321 ? 54322 : 54321;
    record.engineId = info.ID;
    local.write(record);
    await local.checked(['volume', 'create', '--label', label, names.volume]);
    await local.checked(['network', 'create', '--label', label, names.network]);
    await local.checked(['create', '--name', names.container, '--label', label, '--network', names.network,
      '--publish', `127.0.0.1:${record.port}:8090`, '--memory', '128m', '--cpus', '0.5', '--pids-limit', '64',
      '--env', `QA_TOKEN=${token}`, '--env', `QA_CHECKSUM=${record.checksum}`,
      '--mount', `type=volume,source=${names.volume},target=/var/lib/gaoji-app`,
      '--mount', `type=bind,source=${path.join(__dirname, 'qa-local-reconnect.py')},target=/qa-server.py,readonly`,
      '--entrypoint', '/qa-server.py', image, 'serve']);
    assert.equal((await local.start()).stage, 'reconnect');
    const ready = async () => {
      for (let count = 0; count < 30; count++) {
        try { return await local.request('status'); } catch { await new Promise(resolve => setTimeout(resolve, 200)); }
      }
      const state = await local.owned('container', names.container, record);
      const log = await local.docker(['logs', '--tail', '10', state.Id]);
      throw Error('QA controller did not become ready: ' + state.State.Status + ' ' + log.diagnostic);
    };
    const first = await ready();
    const before = await local.owned('container', names.container, record);
    assert.equal((await local.start()).status, 'awaiting_setup');
    const unchanged = await local.owned('container', names.container, record);
    assert.equal(unchanged.State.StartedAt, before.State.StartedAt);
    await local.checked(['stop', before.Id]);
    assert.equal((await local.start()).stage, 'reconnect');
    assert.equal((await ready()).qa_marker, first.qa_marker);
    assert.equal((await local.owned('container', names.container, record)).Id, before.Id);
    console.log('Real Docker reconnect passed: initial start, no restart on live connect, same-ID stopped resume, unchanged volume marker; synthetic HTTP only.');
  } finally {
    const container = await local.owned('container', names.container, record);
    if (container) await local.checked(['rm', '-f', container.Id]);
    for (const kind of ['network', 'volume']) if (await local.owned(kind, names[kind], record)) await local.checked([kind, 'rm', names[kind]]);
    fs.rmSync(directory, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
