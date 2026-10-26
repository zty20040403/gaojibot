'use strict';
// Opt-in development VM only. No user SSH configuration, mounts or remote hosts.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawn } = require('node:child_process');

const root = path.resolve(__dirname, '../..');
const distribution = (require.main === module ? process.argv[3] : process.env.GAOJI_QA_DISTRIBUTION) || 'ubuntu';
const slot = (require.main === module ? process.argv[4] : process.env.GAOJI_QA_SLOT) || 'primary';
assert.ok(['debian', 'ubuntu'].includes(distribution), 'Choose a fixed QA distribution');
assert.ok(['primary', 'restore-target'].includes(slot), 'Choose a fixed local QA slot');
const primaryDirectory = path.join(root, 'packaging/build/linux-qa-' + distribution);
const directory = primaryDirectory + (slot === 'primary' ? '' : '-' + slot);
const lock = require('../../packaging/qa/' + distribution + '.lock.json');
const qemu = path.join(os.homedir(), '.nix-profile/bin/qemu-system-x86_64');
const qemuImg = path.join(os.homedir(), '.nix-profile/bin/qemu-img');
const file = name => path.join(directory, name);
const run = (command, args) => execFileSync(command, args, { stdio: 'inherit' });
const save = (name, value) => fs.writeFileSync(file(name), JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
function privateRoot() {
  const info = fs.lstatSync(directory);
  assert.ok(info.isDirectory() && info.uid === process.getuid() && !(info.mode & 0o077), 'QA directory is not private');
}
function metadata() {
  privateRoot();
  const value = JSON.parse(fs.readFileSync(file('vm.json'), 'utf8'));
  assert.match(value.id, /^gaoji-local-qa-[a-f0-9]{32}$/);
  assert.equal(value.distribution, distribution);
  assert.equal(value.slot || 'primary', slot);
  assert.ok(Number.isInteger(value.port) && value.port > 1024 && value.port <= 65535);
  return value;
}
async function verifyImage() {
  const digest = crypto.createHash(lock.sha512 ? 'sha512' : 'sha256');
  for await (const chunk of fs.createReadStream(file('base.qcow2'))) digest.update(chunk);
  assert.equal(digest.digest('hex'), lock.sha512 || lock.sha256, 'Official image checksum mismatch');
}
async function prepare() {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  privateRoot();
  if (fs.existsSync(file('vm.json')) || fs.existsSync(file('disk.qcow2'))) throw Error('Existing VM is preserved; do not prepare over it');
  if (slot !== 'primary' && !fs.existsSync(file('base.qcow2'))) {
    const source = path.join(primaryDirectory, 'base.qcow2');
    if (fs.existsSync(source)) {
      assert.ok(fs.lstatSync(source).isFile(), 'QA base image must be a regular file');
      // Reuse only the immutable official image, never the other guest disk or keys.
      fs.copyFileSync(source, file('base.qcow2'), fs.constants.COPYFILE_EXCL | fs.constants.COPYFILE_FICLONE);
    }
  }
  if (!fs.existsSync(file('base.qcow2'))) {
    run('curl', ['--fail', '--location', '--proto', '=https', '--connect-timeout', '20', '--max-time', '1800',
      '--retry', '2', '--continue-at', '-', '--output', file('base.qcow2.partial'), lock.url]);
    fs.renameSync(file('base.qcow2.partial'), file('base.qcow2'));
  }
  await verifyImage();
  for (const name of ['client-key', 'host-key']) if (!fs.existsSync(file(name))) {
    run('/usr/bin/ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-C', 'gaoji-local-qa', '-f', file(name)]);
  }
  const id = 'gaoji-local-qa-' + crypto.randomUUID().replaceAll('-', '');
  const listener = net.createServer();
  await new Promise((resolve, reject) => { listener.once('error', reject); listener.listen(0, '127.0.0.1', resolve); });
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const seed = file('seed'); fs.mkdirSync(seed, { mode: 0o700 });
  const publicKey = fs.readFileSync(file('client-key.pub'), 'utf8').trim();
  const config = { hostname: 'gaoji-local-qa', manage_etc_hosts: true, disable_root: true, ssh_pwauth: false,
    package_update: false, package_upgrade: false,
    users: [{ name: 'qa', groups: ['sudo'], shell: '/bin/bash', sudo: 'ALL=(ALL) NOPASSWD:ALL', lock_passwd: true, ssh_authorized_keys: [publicKey] }],
    ssh_keys: { ed25519_private: fs.readFileSync(file('host-key'), 'utf8'), ed25519_public: fs.readFileSync(file('host-key.pub'), 'utf8') },
    write_files: [{ path: '/etc/gaoji-local-qa', content: id + '\n', permissions: '0644' }],
  };
  fs.writeFileSync(path.join(seed, 'user-data'), '#cloud-config\n' + JSON.stringify(config), { mode: 0o600 });
  fs.writeFileSync(path.join(seed, 'meta-data'), JSON.stringify({ 'instance-id': id, 'local-hostname': 'gaoji-local-qa' }), { mode: 0o600 });
  run('/usr/bin/hdiutil', ['makehybrid', '-o', file('seed.iso'), seed, '-iso', '-joliet', '-default-volume-name', 'cidata']);
  run(qemuImg, ['create', '-f', 'qcow2', '-F', 'qcow2', '-b', file('base.qcow2'), file('disk.qcow2'), '24G']);
  save('vm.json', { schema: 1, id, port, distribution, slot, image: lock.version, memoryMiB: 4096, cpus: 2 });
  console.log(JSON.stringify(metadata()));
}
async function qmp(action) {
  assert.ok(['query-status', 'system_powerdown', 'quit'].includes(action));
  const value = metadata();
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(file('qmp.sock'));
    let buffer = '', stage = 0, done = false;
    const timer = setTimeout(() => socket.destroy(Error('Local VM QMP timed out')), 10000);
    const send = execute => socket.write(JSON.stringify({ execute, id: execute }) + '\n');
    socket.on('error', reject);
    socket.on('close', () => { clearTimeout(timer); if (!done) reject(Error('Local VM QMP closed before acknowledgement')); });
    socket.on('data', chunk => {
      buffer += chunk;
      if (buffer.length > 1024 * 1024) return socket.destroy(Error('Invalid QMP reply'));
      while (buffer.includes('\n')) {
        const line = buffer.slice(0, buffer.indexOf('\n')); buffer = buffer.slice(buffer.indexOf('\n') + 1);
        let reply;
        try { reply = JSON.parse(line); } catch { socket.destroy(Error('Invalid QMP JSON')); return; }
        if (reply.error) { socket.destroy(Error('QMP rejected ' + action)); return; }
        if (stage === 0 && reply.QMP) { stage++; send('qmp_capabilities'); }
        else if (stage === 1 && reply.id === 'qmp_capabilities') { stage++; send('query-name'); }
        else if (stage === 2 && reply.id === 'query-name') {
          if (reply.return?.name !== value.id) { socket.destroy(Error('Not this QA virtual machine')); return; }
          stage++; send(action);
        } else if (stage === 3 && reply.id === action) { done = true; resolve(reply.return); socket.end(); }
      }
    });
  });
}
async function boot() {
  const value = metadata();
  try { await qmp('query-status'); throw Error('This VM is already running'); }
  catch (error) { if (!['ENOENT', 'ECONNREFUSED'].includes(error.code)) throw error; }
  await verifyImage();
  const log = fs.openSync(file('qemu.log'), 'a', 0o600);
  const args = ['-name', value.id, '-machine', 'q35,accel=tcg', '-cpu', 'max', '-smp', '2', '-m', '4096',
    '-display', 'none', '-monitor', 'none', '-serial', 'file:' + file('boot.log'),
    '-qmp', 'unix:' + file('qmp.sock') + ',server=on,wait=off',
    '-drive', 'file=' + file('disk.qcow2') + ',if=virtio,format=qcow2',
    '-drive', 'file=' + file('seed.iso') + ',if=virtio,format=raw,readonly=on',
    '-netdev', 'user,id=net0,ipv6=off,hostfwd=tcp:127.0.0.1:' + value.port + '-:22',
    '-device', 'virtio-net-pci,netdev=net0', '-object', 'rng-random,filename=/dev/urandom,id=rng0', '-device', 'virtio-rng-pci,rng=rng0'];
  const child = spawn(qemu, args, { stdio: ['ignore', log, log] });
  fs.closeSync(log);
  console.log(JSON.stringify({ id: value.id, pid: child.pid, ssh: '127.0.0.1:' + value.port, memoryMiB: 4096, cpus: 2, hostMounts: false }));
  process.once('SIGINT', () => child.kill('SIGTERM'));
  process.once('SIGTERM', () => child.kill('SIGTERM'));
  await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', (code, signal) => {
    console.log(JSON.stringify({ vmExited: true, code, signal })); code ? reject(Error('VM exited with ' + code)) : resolve();
  }); });
}
async function connection() {
  const value = metadata();
  const ssh = require('../lib/ssh.cjs');
  const { utils } = require('ssh2');
  const parsed = utils.parseKey(fs.readFileSync(file('host-key.pub')));
  if (parsed instanceof Error) throw parsed;
  const fingerprint = 'SHA256:' + crypto.createHash('sha256').update(parsed.getPublicSSH()).digest('base64').replace(/=+$/, '');
  const client = await ssh.connect({ host: '127.0.0.1', port: value.port, username: 'qa' },
    { type: 'private-key', privateKey: fs.readFileSync(file('client-key')) }, fingerprint);
  try {
    const marker = await ssh.execute(client, 'cat /etc/gaoji-local-qa');
    assert.equal(marker.code, 0); assert.equal(marker.stdout.trim(), value.id, 'Not this isolated VM');
    return client;
  } catch (error) { client.end(); throw error; }
}
async function inspectOrInstall(action) {
  const ssh = require('../lib/ssh.cjs'), deploy = require('../lib/deploy.cjs');
  const client = await connection();
  try {
    if (action === 'inspect') {
      console.log(JSON.stringify(await ssh.inspectServer(client)));
      const baseline = await ssh.execute(client, 'printf "boot_id="; cat /proc/sys/kernel/random/boot_id\nprintf "docker="; command -v docker || true\nprintf "postgres="; command -v postgres || true\ncloud-init status\nsystemctl --failed --no-legend --no-pager');
      console.log(baseline.stdout);
    } else if (action === 'install') {
      if (fs.existsSync(file('deployment.json'))) throw Error('Existing installation must be inspected, not duplicated');
      console.log(JSON.stringify(await deploy.start(client, path.join(root, 'packaging/build/deployment'), { onPrepared: value => save('deployment.json', value) })));
    } else {
      const value = JSON.parse(fs.readFileSync(file('deployment.json'), 'utf8'));
      if (action === 'retry') {
        console.log(JSON.stringify(await deploy.retry(client, path.join(root, 'packaging/build/deployment'), value,
          undefined, prepared => save('deployment.json', prepared))));
      } else {
        console.log(JSON.stringify(await deploy.status(client, value.job)));
        if (action === 'runtime-diagnostics') {
          const id = deploy.jobId(value.job);
          const record = await ssh.execute(client, `sudo -n python3 -c 'import json; print(json.load(open("/var/lib/gaoji-app/jobs/${id}.json"))["runtime_image"])'`);
          assert.equal(record.code, 0);
          const image = record.stdout.trim();
          assert.match(image, /^gaoji-app-runtime:[a-f0-9]{64}$/);
          const result = await ssh.execute(client,
            `sudo -n docker run --rm --platform linux/amd64 --pull=never --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true --memory 1g --cpus 1 --pids-limit 256 --shm-size 128m --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 --env HOME=/tmp --entrypoint python ${image} -c 'from src.local_runtime.runtime_check import check_runtime; print(check_runtime())'`,
            { timeoutMs: 150000, maxBytes: 128 * 1024 });
          console.log(result.stdout); console.error(result.stderr);
          if (result.code !== 0) throw Error('Offline image acceptance failed: ' + result.code);
        } else if (action === 'diagnostics') {
          const id = deploy.jobId(value.job);
          const result = await ssh.execute(client,
            `systemctl show gaoji-app-install-${id}.service --property=ActiveState,SubState,Result,ExecMainStatus\nsudo -n tail -n 40 /var/lib/gaoji-app/jobs/${id}.log`);
          console.log(result.stdout);
          if (result.code !== 0) throw Error('Local installer diagnostics unavailable: ' + result.stderr);
        }
      }
    }
  } finally { client.end(); }
}
async function main() {
  const action = process.argv[2];
  if (action === 'prepare') await prepare();
  else if (action === 'run') await boot();
  else if (['inspect', 'install', 'installation-status', 'diagnostics', 'runtime-diagnostics', 'retry'].includes(action)) await inspectOrInstall(action);
  else if (['query-status', 'system_powerdown', 'quit'].includes(action)) console.log(JSON.stringify(await qmp(action)));
  else throw Error('Choose prepare, run, inspect, install, installation-status, diagnostics, runtime-diagnostics, retry, query-status, system_powerdown or quit');
}
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { connection, metadata, directory, qmp };
