'use strict';
// Development acceptance only. Uses the real Electron preload and no server credentials.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function checkPackagedSSH(app) {
  const { Server } = require('ssh2');
  const { generateKeyPairSync, randomBytes, createHash } = require('node:crypto');
  const http = require('node:http'), net = require('node:net');
  const bytes = randomBytes(512 * 1024), token = 't'.repeat(43), instance = 'd'.repeat(32);
  const info = { schema: 1, id: 'a'.repeat(32), source_instance_id: 'b'.repeat(32), runtime_checksum: 'c'.repeat(64),
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), database_bytes: 4096, created_at: 100, revision: 2, files: 3 };
  const receipt = { id: createHash('sha256').update(instance + ':' + info.sha256).digest('hex').slice(0, 32), imported: true };
  let imported = false;
  const transferErrors = [], forwarded = new Set();
  const api = http.createServer(async (req, res) => {
    try {
      assert.equal(req.headers.authorization, 'Bearer ' + token);
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const data = Buffer.concat(chunks);
      const json = value => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ data: value })); };
      if (req.url === '/versions') return json({ checksum: info.runtime_checksum, revision: 2 });
      if (req.url === '/backup/import/status') return json({ ...receipt, imported });
      if (req.url === '/backup/export') {
        assert.deepEqual(JSON.parse(data), { id: info.id });
        res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': bytes.length,
          'X-Gaoji-Backup': Buffer.from(JSON.stringify(info)).toString('base64') });
        res.end(bytes); return;
      }
      assert.equal(req.url, '/backup/import');
      assert.deepEqual(data, bytes);
      imported = true; json(receipt);
    } catch (error) { transferErrors.push(error.message); res.destroy(); }
  });
  api.on('clientError', (_error, socket) => socket.destroy());
  await new Promise(resolve => api.listen(0, '127.0.0.1', resolve));
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048,
    privateKeyEncoding: { type: 'pkcs1', format: 'pem' }, publicKeyEncoding: { type: 'pkcs1', format: 'pem' } });
  const connections = new Set();
  const server = new Server({ hostKeys: [privateKey] }, connection => {
    connections.add(connection);
    connection.on('error', () => {});
    connection.once('close', () => connections.delete(connection));
    connection.on('authentication', context => {
      if (context.method === 'password' && context.username === 'fixture' && context.password === 'local-fixture-password') context.accept();
      else context.reject();
    });
    connection.on('ready', () => {
      connection.on('tcpip', (accept, reject, info) => {
        if (info.destIP !== '127.0.0.1' || info.destPort !== api.address().port) { reject(); return; }
        const socket = net.createConnection({ host: info.destIP, port: info.destPort });
        forwarded.add(socket); socket.on('error', () => socket.destroy());
        socket.once('close', () => forwarded.delete(socket));
        socket.once('connect', () => {
          const channel = accept();
          channel.on('error', () => socket.destroy());
          channel.once('close', () => socket.destroy());
          socket.once('close', () => channel.destroy());
          socket.pipe(channel).pipe(socket);
        });
      });
      connection.on('session', accept => {
        accept().on('exec', (acceptExec, reject, info) => {
          if (info.command !== 'fixture-echo') { reject(); return; }
          const stream = acceptExec(); stream.write('encrypted local reply'); stream.exit(0); stream.end();
        });
      });
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const result = await app.evaluate(async ({ app }, fixture) => {
      const base = app.getAppPath();
      const require = process.getBuiltinModule('module').createRequire(base + '/package.json');
      const ssh = require(base + '/lib/ssh.cjs');
      const binding = require(base + '/node_modules/ssh2/lib/protocol/crypto.js').bindingAvailable;
      const target = { host: '127.0.0.1', username: 'fixture', port: fixture.sshPort };
      const discovered = await ssh.discoverHost(target);
      const client = await ssh.connect(target, { type: 'password', password: 'local-fixture-password' }, discovered.fingerprint);
      const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
      const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-packaged-transfer-'));
      try {
        const transfer = require(base + '/lib/backup-transfer.cjs');
        const access = { port: fixture.apiPort, token: fixture.token, instance_id: fixture.instance };
        const filename = path.join(folder, 'fixture.gaojibak');
        await transfer.exportBackup(client, access, fixture.backupId, 'fixture-backup-password', filename);
        const restored = await transfer.importBackup(client, access, filename, 'fixture-backup-password');
        return { binding, ...(await ssh.execute(client, 'fixture-echo')), imported: restored.imported };
      } finally { client.end(); fs.rmSync(folder, { recursive: true, force: true }); }
    }, { sshPort: server.address().port, apiPort: api.address().port, token, instance, backupId: info.id });
    assert.equal(result.binding, false);
    assert.equal(result.code, 0);
    assert.equal(result.stdout, 'encrypted local reply');
    assert.equal(result.imported, true);
    assert.equal(imported, true);
    assert.deepEqual(transferErrors, []);
  } finally {
    for (const connection of connections) connection.end();
    for (const socket of forwarded) socket.destroy();
    api.closeAllConnections();
    await new Promise(resolve => api.close(resolve));
    await new Promise(resolve => server.close(resolve));
  }
}

async function main() {
  const { _electron: electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-electron-'));
  let app;
  try {
    const packaged = process.env.GAOJI_PACKAGED_EXECUTABLE;
    app = await electron.launch({ executablePath: packaged || require('electron'),
      args: [...(packaged ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + temporary] });
    const page = await app.firstWindow();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const info = await page.evaluate(() => window.gaojiDesktop.info());
    assert.equal(info.ok, true);
    assert.equal(info.data.runtimeAvailable, true);
    assert.equal(await page.evaluate(() => typeof window.require), 'undefined');
    assert.equal(await page.evaluate(() => typeof window.gaojiDesktop.exec), 'undefined');
    if (packaged) await checkPackagedSSH(app);
    await page.getByRole('button', { name: /^(连接第一台服务器|连接服务器)$/ }).click();
    await page.getByRole('heading', { name: '连接你的服务器' }).waitFor();
    await page.getByLabel('服务器 IP 或域名').fill('a-very-long-server-name.internal.example.com');
    await page.getByLabel('服务器密码', { exact: true }).fill('temporary-ui-test-password');
    assert.equal(await page.getByLabel('服务器密码', { exact: true }).getAttribute('type'), 'password');
    await page.getByRole('button', { name: '显示密码' }).click();
    assert.equal(await page.getByLabel('服务器密码', { exact: true }).getAttribute('type'), 'text');
    await page.getByLabel('服务器密码', { exact: true }).fill('');
    const directory = path.resolve(__dirname, '../release/qa'); fs.mkdirSync(directory, { recursive: true });
    await page.screenshot({ path: path.join(directory, `${packaged ? 'packaged-' : ''}connect-desktop.png`), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: path.join(directory, `${packaged ? 'packaged-' : ''}connect-mobile.png`), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('Electron smoke passed: real IPC, no Node in renderer, password masking, responsive layout.' + (packaged ? ' Packaged SSH encryption and encrypted backup export/import passed against localhost without native accelerators.' : ''));
  } finally {
    if (app) await app.close();
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
