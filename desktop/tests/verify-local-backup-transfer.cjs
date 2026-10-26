'use strict';

// Real main-process IPC and encrypted loopback transfer; no Docker or SSH server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { createHash, randomBytes } = require('node:crypto');

async function main() {
  const { _electron } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'gaoji-local-backup-ipc-'));
  const bytes = randomBytes(256 * 1024);
  const token = 't'.repeat(43), instance = 'b'.repeat(32);
  const info = { schema: 1, id: 'a'.repeat(32), source_instance_id: instance, runtime_checksum: 'c'.repeat(64),
    bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex'), database_bytes: 4096, created_at: 100, revision: 2, files: 3 };
  const receipt = { id: createHash('sha256').update(instance + ':' + info.sha256).digest('hex').slice(0, 32), imported: true };
  const requests = [], errors = [];
  let imported = false, app;
  const server = http.createServer(async (req, res) => {
    try {
      assert.equal(req.headers.authorization, 'Bearer ' + token);
      requests.push(req.url);
      const chunks = []; for await (const chunk of req) chunks.push(chunk);
      const data = Buffer.concat(chunks);
      const json = value => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ data: value })); };
      if (req.url === '/versions') return json({ instance_id: instance, checksum: info.runtime_checksum, revision: 2 });
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
    } catch (error) { errors.push(error.message); res.destroy(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const packaged = process.env.GAOJI_PACKAGED_EXECUTABLE;
    app = await _electron.launch({ executablePath: packaged || require('electron'),
      args: [...(packaged ? [] : [path.resolve(__dirname, '..')]), '--user-data-dir=' + directory] });
    const page = await app.firstWindow();
    await page.getByRole('heading', { name: '我的服务器', exact: true }).waitFor();
    const filename = path.join(directory, '本机备份.gaojibak');
    await app.evaluate(({ app, safeStorage, dialog }, fixture) => {
      const require = process.getBuiltinModule('module').createRequire(app.getAppPath() + '/package.json');
      const path = require('node:path');
      const { LocalDeployment } = require(app.getAppPath() + '/lib/local.cjs');
      const local = new LocalDeployment(path.join(app.getPath('userData'), 'local'), '', safeStorage);
      local.write({ schema: 1, job: 'd'.repeat(32), checksum: 'c'.repeat(64), port: fixture.port,
        botPort: fixture.port === 54321 ? 54322 : 54321, engineId: 'local-fixture-only',
        credential: safeStorage.encryptString(fixture.token).toString('base64') });
      dialog.showSaveDialog = async () => ({ canceled: false, filePath: fixture.filename });
      dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [fixture.filename] });
    }, { port: server.address().port, token, filename });
    const exported = await page.evaluate(request => window.gaojiDesktop.exportBackup(request),
      { id: 'desktop-local', backupId: info.id, password: 'local-backup-test-password' });
    assert.equal(exported.ok, true, JSON.stringify(exported));
    assert.equal(exported.data.exported, true);
    assert.equal(fs.readFileSync(filename).includes(bytes), false);
    const status = await page.evaluate(() => window.gaojiDesktop.backupTransferStatus('desktop-local'));
    assert.equal(status.data.status, 'completed');
    const selected = await page.evaluate(() => window.gaojiDesktop.selectBackup());
    assert.equal(selected.ok, true);
    const before = requests.length;
    const wrong = await page.evaluate(request => window.gaojiDesktop.importBackup(request),
      { id: 'desktop-local', selection: selected.data.selection, password: 'wrong-backup-password' });
    assert.equal(wrong.ok, false);
    assert.equal(requests.length, before, 'Wrong password must not send any request');
    const result = await page.evaluate(request => window.gaojiDesktop.importBackup(request),
      { id: 'desktop-local', selection: selected.data.selection, password: 'local-backup-test-password' });
    assert.equal(result.ok, true, JSON.stringify(result));
    assert.equal(result.data.imported, true);
    assert.equal(imported, true);
    assert.equal(requests.includes('/operation'), false, 'Import must not silently restore');
    assert.deepEqual(errors, []);
    console.log('Local backup IPC passed: real encrypted export/import, wrong-password rejection, transfer status and no implicit restore. Only loopback fixtures.');
  } finally {
    await app?.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
