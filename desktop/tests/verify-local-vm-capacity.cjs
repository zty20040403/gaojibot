'use strict';
// Opt-in system acceptance, restricted by local-vm's loopback/key/marker checks.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { isDeepStrictEqual } = require('node:util');
const vm = require('../scripts/local-vm.cjs');
const ssh = require('../lib/ssh.cjs');
const control = require('../lib/control.cjs');
const { bundle } = require('../lib/deploy.cjs');

const target = bundle(path.resolve(__dirname, '../../packaging/build/deployment')).files['gaoji-runtime.tar.gz'].sha256;
const record = path.join(vm.directory, 'capacity-acceptance-' + target + '.json');
const deployment = JSON.parse(fs.readFileSync(path.join(vm.directory, 'deployment.json'), 'utf8'));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const recover = process.argv.includes('--recover');
  assert.ok(recover !== process.argv.includes('--exercise'), 'Choose --exercise or --recover');
  assert.match(deployment.job, /^[a-f0-9]{32}$/);
  const previous = fs.existsSync(record) ? JSON.parse(fs.readFileSync(record, 'utf8')) : null;
  if (previous && !previous.cleaned && !recover) throw Error('Pending QA cleanup; use --recover before another exercise');
  if (recover) assert.ok(previous && !previous.cleaned, 'No pending QA cleanup');
  const client = await vm.connection();
  let state = recover ? previous : null;
  const save = changes => {
    state = { ...state, ...changes };
    fs.writeFileSync(record + '.partial', JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
    fs.renameSync(record + '.partial', record);
  };
  try {
    const access = await control.accessFor(client, deployment.job);
    const version = await control.request(client, access, 'versions');
    assert.equal(version.checksum, target, 'Update the existing QA instance first');
    const settings = await control.request(client, access, 'status');
    assert.equal(settings.capacity_configuration.schema, 1);
    assert.equal(settings.desired.bot_enabled, false);
    assert.deepEqual(settings.instance.settings.models, []);
    assert.equal(settings.instance.settings.bot_qq, '');
    assert.equal((await control.request(client, access, 'qqStatus')).online, false);

    // Keep guest commands structured and local. Never return raw config or stderr.
    const guest = async body => {
      const script = `import hashlib, json, os, pathlib, subprocess
root = pathlib.Path('/var/lib/gaoji-app')
cfg = json.loads((root / 'jobs' / '${deployment.job}.json').read_text())
assert cfg['instance_id'] == '${access.instance_id}'
assert cfg['checksum'] == '${target}'
assert cfg['docker_host'].startswith('unix:///')
assert pathlib.Path(cfg['docker']).is_absolute()
directory = root / 'data' / 'instances' / cfg['instance_id']
release = root / 'releases' / cfg['checksum']
assert json.loads((release / '.bundle.json').read_text())['sha256'] == cfg['checksum']
manifest = json.loads((release / 'manifest.json').read_text())
identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
image = 'gaoji-app-runtime:' + identity
assert cfg['runtime_image'] == image
compose = json.loads((directory / 'compose.json').read_text())
assert compose['name'] == 'gaoji-' + cfg['instance_id']
assert compose['services']['bot']['image'] == image
env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8', 'DOCKER_HOST': cfg['docker_host']}
def docker(args, timeout=120):
    result = subprocess.run([cfg['docker'], *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, timeout=timeout, check=False)
    if result.returncode: raise RuntimeError('Local QA Docker check failed: ' + args[0])
    return result.stdout.decode()
${body}`;
      const result = await ssh.execute(client, 'sudo -n python3 -', { stdin: script, timeoutMs: 150000, maxBytes: 16384 });
      if (result.code !== 0) {
        const lines = [...result.stderr.matchAll(/File "<stdin>", line (\d+)/g)].map(match => match[1]);
        const kind = result.stderr.match(/^([A-Za-z]+Error|AssertionError)(?::|$)/m)?.[1] || 'command';
        throw Error('Local QA guest check failed: ' + kind + ', probe lines ' + lines.join(',') + ' (private output omitted)');
      }
      return JSON.parse(result.stdout);
    };
    const database = () => guest(`sql = "SELECT system_identifier FROM pg_control_system(); SELECT version_num FROM qq_bot.alembic_version;"
rows = docker(['compose', '--file', str(directory / 'compose.json'), 'exec', '-T', 'database',
               'psql', '-U', 'gaoji', '-d', 'gaoji', '-Atc', sql]).strip().splitlines()
assert len(rows) == 2
print(json.dumps({'identifier': rows[0], 'schema': rows[1]}))`);
    const scan = async () => {
      for (let attempt = 0; attempt < 4; attempt++) {
        try { await control.request(client, access, 'storageScan'); break; }
        catch (error) {
          if (!/扫描已在进行|容量统计已在运行/.test(error.message) || attempt === 3) throw error;
          await pause(2000);
        }
      }
      return (await control.request(client, access, 'storageStatus')).capacity;
    };
    const setPolicy = async (from, to) => {
      const current = await control.request(client, access, 'status');
      assert.equal(current.desired.bot_enabled, false);
      assert.deepEqual(current.instance.settings.components.capacity, from, 'Unexpected policy change; do not overwrite it');
      await control.request(client, access, 'save', { revision: current.instance.revision,
        settings: { ...current.instance.settings,
          components: { ...current.instance.settings.components, capacity: to } }, model_keys: {} });
    };
    if (!recover) {
      const baseline = await scan();
      assert.equal(baseline.state, 'ready', baseline.reason);
      assert.ok(baseline.complete && baseline.disks.every(disk => disk.total_bytes < 1024 ** 4));
      const run = crypto.randomUUID().replaceAll('-', '');
      save({ schema: 1, target, localhost_only: true, vm_id: vm.metadata().id,
        instance_id: access.instance_id, job: deployment.job, run,
        volume: 'gaoji-app-' + access.instance_id + '-work-s' + run,
        writer: 'gaoji-qa-capacity-' + run, probe: 'gaoji-qa-probe-' + run,
        original_policy: settings.instance.settings.components.capacity,
        blocked_policy: { ...settings.instance.settings.components.capacity, reserve_free_gib: 1024 },
        baseline, database_before: await database(), started_at: new Date().toISOString(), cleaned: false });
    }
    assert.equal(state.vm_id, vm.metadata().id);
    assert.equal(state.instance_id, access.instance_id);
    assert.equal(state.job, deployment.job);
    assert.equal(state.target, target);
    assert.match(state.run, /^[a-f0-9]{32}$/);
    assert.equal(state.volume, 'gaoji-app-' + access.instance_id + '-work-s' + state.run);
    assert.equal(state.writer, 'gaoji-qa-capacity-' + state.run);
    assert.equal(state.probe, 'gaoji-qa-probe-' + state.run);
    const ownership = `volume = ${JSON.stringify(state.volume)}
labels = {'gaoji.app.instance': cfg['instance_id'], 'gaoji.app.resource': 'workspace', 'gaoji.qa.run': '${state.run}'}
def inspect_volume():
    rows = json.loads(docker(['volume', 'inspect', volume]))
    assert len(rows) == 1 and rows[0]['Name'] == volume
    row = rows[0]
    assert row['Labels'] == labels and row['Driver'] == 'local' and not row.get('Options')
    engine = pathlib.Path(json.loads(docker(['info', '--format', '{{json .DockerRootDir}}'])))
    mount = engine / 'volumes' / volume / '_data'
    assert engine.is_absolute() and mount.resolve() == mount and row['Mountpoint'] == str(mount)
    return mount
`;
    const probe = async blocked => guest(`code = ${JSON.stringify(`import json, os, pathlib, stat
from src.local_runtime.entrypoint import load_environment
from src.storage_admission import CapacityError, read_snapshot, require_capacity, require_tool_capacity
load_environment(pathlib.Path('/run/gaoji/runtime.json'))
assert os.geteuid() == 10001
filename = pathlib.Path(os.environ['AI_APP_CAPACITY_FILE'])
assert str(filename) == '/run/gaoji-capacity/status.json'
value = read_snapshot(filename, os.environ['AI_APP_INSTANCE_ID'])
assert value['instance_id'] == '${access.instance_id}' and value['complete']
info = filename.stat()
assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o644
mounts = [line.split() for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines()]
assert any(row[4] == '/run/gaoji-capacity' and 'ro' in row[5].split(',') for row in mounts)
expected_block = ${blocked ? 'True' : 'False'}
for action in (lambda: require_capacity(), lambda: require_tool_capacity('sandbox_exec', ('write:sandbox',))):
    try: action()
    except CapacityError as error:
        assert expected_block and '预留' in str(error), 'Wrong admission failure'
    else: assert not expected_block, 'Write admission did not block'
for name, effects in [('sandbox_read_file', ('read',)), ('send_file_from_sandbox', ('send:message',)),
                      ('sandbox_destroy', ('write:sandbox',)), ('browser_clear', ('write:browser',))]:
    require_tool_capacity(name, effects)
print(json.dumps({'uid': os.geteuid(), 'readonly_mount': True, 'snapshot_mode': '0644',
                  'blocked': expected_block, 'read_send_cleanup_admitted': True,
                  'revision': value['revision'], 'policy': value['policy']}))`)}
print(docker(['compose', '--file', str(directory / 'compose.json'), 'run', '--rm', '--no-deps', '-T',
              '--name', '${state.probe}', '--label', 'gaoji.qa.run=${state.run}', '--entrypoint', 'python', 'bot', '-c', code]))`);
    try {
      if (!recover) {
        const bytes = await guest(ownership + `assert volume not in docker(['volume', 'ls', '--format', '{{.Name}}']).splitlines()
args = ['volume', 'create']
for key, value in labels.items(): args += ['--label', key + '=' + value]
assert docker(args + [volume]).strip() == volume
mount = inspect_volume()
code = "from pathlib import Path; p=Path('/work/payload.bin'); p.write_bytes(b'Q'*65536)"
docker(['run', '--rm', '--name', '${state.writer}', '--label', 'gaoji.qa.run=${state.run}',
        '--pull=never', '--network', 'none', '--read-only', '--user', '0:0', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--memory', '128m', '--cpus', '1', '--pids-limit', '32',
        '--mount', 'type=volume,src=' + volume + ',dst=/work', '--entrypoint', 'python', image, '-c', code])
info = (mount / 'payload.bin').stat()
assert info.st_size == 65536 and info.st_blocks * 512 >= 65536
print(json.dumps({'logical': info.st_size, 'allocated': info.st_blocks * 512}))`);
        save({ volume_bytes: bytes });
        const measured = await scan();
        assert.equal(measured.state, 'ready', measured.reason);
        assert.ok(measured.sandbox_bytes >= state.baseline.sandbox_bytes + bytes.allocated, 'Working volume was not accounted');
        save({ measured, changing_policy: true });
        await setPolicy(state.original_policy, state.blocked_policy);
        const blocked = await scan();
        assert.equal(blocked.state, 'blocked');
        assert.ok(blocked.reason.includes('预留'), 'Expected disk reserve admission failure');
        assert.deepEqual(blocked.policy, state.blocked_policy);
        save({ blocked });
        save({ blocked_container: await probe(true) });
        await setPolicy(state.blocked_policy, state.original_policy);
        assert.equal((await scan()).state, 'ready');
        save({ recovered_container: await probe(false) });
      }
    } finally {
      const current = await control.request(client, access, 'status');
      if (!isDeepStrictEqual(current.instance.settings.components.capacity, state.original_policy)) {
        await setPolicy(state.blocked_policy, state.original_policy);
      }
      // Only the recorded QA objects may be removed; never force removal or prune.
      await guest(ownership + `for name in ['${state.writer}', '${state.probe}']:
    ids = docker(['ps', '-aq', '--no-trunc', '--filter', 'name=^/' + name + '$']).splitlines()
    if ids:
        assert len(ids) == 1
        row = json.loads(docker(['container', 'inspect', ids[0]]))[0]
        assert row['Name'] == '/' + name and row['Config']['Labels']['gaoji.qa.run'] == '${state.run}'
        assert not row['State']['Running'], 'QA container is still running; do not remove it'
        docker(['container', 'rm', ids[0]])
if volume in docker(['volume', 'ls', '--format', '{{.Name}}']).splitlines():
    inspect_volume()
    docker(['volume', 'rm', volume])
print(json.dumps({'own_volume_removed': True, 'shared_resources_untouched': True}))`);
      const restored = await scan();
      assert.equal(restored.state, 'ready', restored.reason);
      assert.deepEqual(restored.policy, state.original_policy);
      const after = await database();
      assert.deepEqual(after, state.database_before, 'Database identity or schema changed');
      save({ cleaned: true, restored, database_after: after, cleaned_at: new Date().toISOString() });
    }
    if (!recover) {
      const final = await control.request(client, access, 'status');
      assert.equal(final.desired.bot_enabled, false);
      assert.deepEqual(final.instance.settings.models, []);
      assert.equal((await control.request(client, access, 'qqStatus')).online, false);
      save({ completed: true, verified_at: new Date().toISOString() });
    }
    console.log(JSON.stringify({ completed: !!state.completed, cleaned: state.cleaned, record,
      database_unchanged: true, bot_enabled: false, localhost_only: true }));
  } finally { client.end(); }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
