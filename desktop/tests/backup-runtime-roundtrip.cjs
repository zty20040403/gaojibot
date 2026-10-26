'use strict';
// Spawned by the Python fixture, with synthetic credentials and loopback only.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const net = require('node:net');
const crypto = require('../lib/backup-crypto.cjs');
const transfer = require('../lib/backup-transfer.cjs');
const control = require('../lib/control.cjs');

async function main() {
  const { access, backupId, filename } = JSON.parse(process.argv[2]);
  const client = { forwardOut(source, _sourcePort, host, port, callback) {
    assert.equal(source, '127.0.0.1'); assert.equal(host, '127.0.0.1'); assert.equal(port, access.port);
    const socket = net.createConnection({ host, port });
    socket.once('error', error => callback(error)); socket.once('connect', () => callback(null, socket));
  } };
  const password = 'local-runtime-roundtrip-fixture';
  assert.equal((await transfer.exportBackup(client, access, backupId, password, filename)).exported, true);
  const encrypted = await fs.readFile(filename);
  assert.equal(encrypted.includes(Buffer.from('private-test-model-key')), false);
  assert.equal(encrypted.includes(Buffer.from('private-qq-session')), false);
  await assert.rejects(transfer.importBackup(client, access, filename, 'invalid-local-password'), { code: 'backup_password_or_damage' });
  const first = await transfer.importBackup(client, access, filename, password);
  assert.equal(first.imported, true); assert.equal(first.already_imported, false);
  const second = await transfer.importBackup(client, access, filename, password);
  assert.equal(second.id, first.id); assert.equal(second.already_imported, true);
  assert.equal((await control.request(client, access, 'backups')).backups.length, 2);
  const opened = await crypto.openEncrypted(filename, password);
  try { assert.equal(opened.metadata.source_instance_id, access.instance_id); } finally { await opened.close(); }
  console.log(JSON.stringify({ imported_id: first.id, repeated_without_duplicate: true }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
