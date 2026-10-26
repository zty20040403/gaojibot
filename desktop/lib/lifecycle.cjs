'use strict';

const { UserError, shellLiteral } = require('./contracts.cjs');
const ssh = require('./ssh.cjs');
const deploy = require('./deploy.cjs');

const STATES = new Set(['installed', 'retained', 'removing', 'restoring', 'purging', 'removed', 'unknown']);
function validateRecord(value) {
  if (!value || !['uninstall', 'reinstall', 'purge'].includes(value.kind) || !STATES.has(value.service_state)
      || typeof value.dispatched !== 'boolean' || typeof value.settled !== 'boolean'
      || !Number.isSafeInteger(value.created_at) || value.created_at < 0) throw new UserError('invalid_lifecycle', '服务维护记录无效');
  return { id: deploy.jobId(value.id), job: deploy.jobId(value.job), instance_id: deploy.jobId(value.instance_id),
    kind: value.kind, created_at: value.created_at, service_state: value.service_state, dispatched: value.dispatched, settled: value.settled };
}
function receipt(value, expected) {
  const purge = expected.kind === 'purge';
  const names = [`gaoji-app-install-${expected.job}.service`, `gaoji-app-${expected.instance_id}.service`,
    `gaoji-app-sandbox-${expected.instance_id}.service`, `gaoji-app-control-${expected.instance_id}.service`];
  if (!value || value.schema !== 1 || value.id !== expected.id || value.job !== expected.job || value.instance_id !== expected.instance_id || value.kind !== expected.kind
      || !['preview', 'queued', 'running', 'failed', 'completed'].includes(value.status) || !STATES.has(value.service_state)
      || typeof value.stage !== 'string' || value.stage.length > 80 || typeof value.message !== 'string' || value.message.length > 2000
      || typeof value.resumable !== 'boolean' || !Number.isSafeInteger(value.created_at) || !Number.isSafeInteger(value.updated_at)
      || value.created_at < 0 || value.updated_at < value.created_at
      || !Array.isArray(value.units) || value.units.length > (purge ? 5000 : 4) || new Set(value.units).size !== value.units.length
      || value.units.some(name => purge ? !/^gaoji-app-(lifecycle|update)-[a-f0-9]{32}\.service$/.test(name) : !names.includes(name))
      || value.data_retained !== !purge || value.shared_software_not_removed !== true
      || value.data_directory !== `/var/lib/gaoji-app/data/instances/${expected.instance_id}`) throw new UserError('invalid_lifecycle_status', '服务维护回执与此实例不符');
  let deletion = {};
  if (purge) {
    const resources = value.delete_resources;
    const kinds = ['containers', 'volumes', 'networks'];
    if (!resources || Object.keys(resources).sort().join() !== kinds.slice().sort().join() || value.audit_retained !== true
        || !Number.isSafeInteger(value.data_bytes) || value.data_bytes < 0
        || kinds.some(kind => !Array.isArray(resources[kind]) || resources[kind].length > 5000
          || resources[kind].some(row => !row || typeof row.name !== 'string' || row.name.length > 255
            || !(kind === 'networks' ? row.name === `gaoji-${expected.instance_id}_default`
              : kind === 'volumes' ? row.name === row.id && row.name.startsWith(`gaoji-app-${expected.instance_id}-`)
              : row.name.startsWith(`gaoji-${expected.instance_id}-`) || row.name.startsWith(`gaoji-app-${expected.instance_id}-`))
            || !(kind === 'volumes' ? /^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$/ : /^[a-f0-9]{64}$/).test(row.id)))) {
      throw new UserError('invalid_lifecycle_status', '永久删除清单不完整');
    }
    deletion = { delete_resources: Object.fromEntries(kinds.map(kind => [kind, resources[kind].map(({ id, name }) => ({ id, name }))])), data_bytes: value.data_bytes, audit_retained: true };
  }
  return { schema: 1, id: value.id, job: value.job, instance_id: value.instance_id, kind: value.kind, status: value.status,
    stage: value.stage, message: value.message, created_at: value.created_at, updated_at: value.updated_at, resumable: value.resumable,
    service_state: value.service_state, units: value.units, data_retained: !purge, shared_software_not_removed: true, data_directory: value.data_directory, ...deletion };
}

// Isolated Python has no user-home import path. Only the root-owned, verified
// installed controller may execute only the fixed lifecycle operations.
const GATEWAY = `import json, os, re, stat, sys
from pathlib import Path
job, uid, action, argument, erase_instance = sys.argv[1:]
root = Path('/var/lib/gaoji-app')
def safe_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Invalid managed directory')
def read(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 65536:
            raise ValueError('Invalid managed record')
        return json.load(stream)
try:
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{32}', job) or not uid.isdigit():
        raise ValueError('Invalid identity')
    for directory in (root, root / 'bootstrap', root / 'bootstrap' / job, root / 'jobs', root / 'releases'):
        safe_dir(directory)
    if read(root / 'bootstrap' / job / 'owner.json') != {'uid': int(uid)}:
        raise ValueError('Wrong deployment owner')
    deployment = read(root / 'jobs' / (job + '.json'))
    digest = deployment.get('controller_checksum', deployment['checksum'])
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Invalid controller release')
    release = root / 'releases' / digest
    safe_dir(release)
    if not (release / 'src/local_runtime/lifecycle_worker.py').is_file():
        print(json.dumps({'ok': False, 'error': {'code': 'lifecycle_unsupported', 'message': '请先通过更新与回退升级服务端管理器，再使用服务维护'}}))
        sys.exit(0)
    sys.path.insert(0, str(release))
    from src.local_runtime.releases import load_release
    from src.local_runtime.lifecycle import Lifecycle
    from src.local_runtime.lifecycle_worker import dispatch
    from src.local_runtime.setup_clients import SetupError
    load_release(root / 'releases', digest)
    if action == 'prepare' and argument == 'purge' and not erase_instance:
        if not (release / 'src/local_runtime/purge.py').is_file():
            print(json.dumps({'ok': False, 'error': {'code': 'lifecycle_unsupported', 'message': '请先升级管理器，再使用彻底删除'}}))
            sys.exit(0)
        from src.local_runtime.purge import Purge
        result = Purge(job).prepare()
    elif action == 'prepare' and argument in ('uninstall', 'reinstall') and not erase_instance:
        result = Lifecycle(job).prepare(argument)
    elif action == 'dispatch' and re.fullmatch('[a-f0-9]{32}', argument):
        result = dispatch(root, job, argument, confirm=True, erase_instance_id=erase_instance or None)
    else:
        raise ValueError('Invalid action')
    print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False))
except Exception as error:
    safe_error = 'SetupError' in globals() and isinstance(error, SetupError)
    print(json.dumps({'ok': False, 'error': {'code': 'lifecycle_rejected', 'message': str(error) if safe_error else '维护未通过实例归属或版本检查，未开放其他服务器操作'}}, ensure_ascii=False))
`;

function script(job, uid, action, argument, eraseInstanceId = '') {
  deploy.jobId(job);
  if (!Number.isSafeInteger(uid) || uid < 0 || uid >= 2 ** 31 || !['prepare', 'dispatch'].includes(action)
      || action === 'prepare' && (!['uninstall', 'reinstall', 'purge'].includes(argument) || eraseInstanceId)) throw new UserError('invalid_lifecycle', '服务维护参数无效');
  if (action === 'dispatch') deploy.jobId(argument);
  if (eraseInstanceId) deploy.jobId(eraseInstanceId);
  return `set -eu\ncd /\npython=/var/lib/gaoji-app/tools/bin/python3\nif ! test -x "$python"; then python=$(command -v python3); fi\nPYTHONPATH= PYTHONHOME= PYTHONDONTWRITEBYTECODE=1 "$python" -I - ${[job, String(uid), action, argument, eraseInstanceId].map(shellLiteral).join(' ')} <<'GAOJI_FIXED_LIFECYCLE'\n${GATEWAY}\nGAOJI_FIXED_LIFECYCLE\n`;
}
async function invoke(client, job, action, argument, password, eraseInstanceId = '') {
  const inspection = await ssh.inspectServer(client);
  const result = await deploy.privileged(client, script(job, inspection.probe.uid, action, argument, eraseInstanceId), inspection.probe.privilege, password, { maxBytes: 2 * 1024 ** 2 });
  if (result.code !== 0) throw new UserError('lifecycle_unconfirmed', '服务维护尚未确认，请核对权限并查询原任务');
  let value;
  try { value = JSON.parse(result.stdout); } catch { throw new UserError('lifecycle_unconfirmed', '尚未收到有效维护回执，请查询原任务'); }
  if (value?.ok !== true) throw new UserError(value?.error?.code === 'lifecycle_unsupported' ? 'lifecycle_unsupported' : 'lifecycle_rejected',
    typeof value?.error?.message === 'string' && value.error.message.length <= 2000 ? value.error.message : '服务器未接受这项维护');
  return value.data;
}
async function status(client, value) {
  const record = validateRecord(value);
  const result = await ssh.execute(client, `cat /var/lib/gaoji-app/status/lifecycles/${record.id}.json`, { maxBytes: 2 * 1024 ** 2 });
  const uncertain = { ...record, status: 'unconfirmed', stage: 'dispatch', message: '尚未确认服务端状态；不会自动创建第二个维护任务', resumable: record.dispatched };
  if (result.code !== 0) return uncertain;
  let parsed;
  try { parsed = JSON.parse(result.stdout); } catch { throw new UserError('invalid_lifecycle_status', '服务维护状态暂不可读'); }
  const current = receipt(parsed, record);
  if (current.status === 'preview' && record.dispatched) return { ...current, status: 'unconfirmed', resumable: true, message: '服务端仍是预览，提交尚未确认；继续时沿用此任务编号' };
  if (['queued', 'running'].includes(current.status)) {
    const result = await ssh.execute(client, `systemctl show gaoji-app-lifecycle-${record.id}.service --property=ActiveState --value`, { maxBytes: 4096 });
    if (result.code === 0 && ['inactive', 'failed'].includes(result.stdout.trim())) return { ...current, status: 'interrupted', resumable: true, message: '维护执行器已停止，数据和任务记录保留，可继续原任务' };
  }
  return current;
}
async function prepare(client, server, options, onPrepared) {
  if (!options || Object.keys(options).some(key => !['id', 'kind', 'password'].includes(key)) || !['uninstall', 'reinstall', 'purge'].includes(options.kind)) throw new UserError('invalid_lifecycle', '请选择固定的实例服务维护操作');
  if (server.lifecycle?.dispatched) {
    const previous = await status(client, server.lifecycle);
    if (previous.status !== 'completed' && !(previous.status === 'failed' && !previous.resumable)) throw new UserError('lifecycle_busy', '原服务维护尚未结束，请先继续原任务');
  }
  const value = await invoke(client, deploy.jobId(server.deployment.job), 'prepare', options.kind, options.password);
  const record = validateRecord({ ...value, dispatched: false, settled: false });
  if (record.job !== server.deployment.job || record.kind !== options.kind) throw new UserError('invalid_lifecycle_status', '预览不属于当前实例或所选操作');
  const checked = receipt(value, record);
  if (checked.status !== 'preview') throw new UserError('invalid_lifecycle_status', '预览请求返回了非预览结果');
  onPrepared(record);
  return checked;
}
async function submit(client, server, options, onPrepared) {
  if (!options || Object.keys(options).some(key => !['id', 'operationId', 'confirm', 'password', 'eraseInstanceId'].includes(key)) || options.confirm !== true) throw new UserError('confirmation_required', '请确认服务维护清单');
  const record = validateRecord(server.lifecycle);
  if (record.kind === 'purge' ? options.eraseInstanceId !== record.instance_id : options.eraseInstanceId !== undefined) throw new UserError('confirmation_required', '永久删除需要单独确认这个实例');
  if (record.job !== server.deployment.job || record.id !== options.operationId) throw new UserError('invalid_lifecycle', '不能提交另一个维护任务');
  const current = await status(client, record);
  if (current.status === 'completed' || record.dispatched && ['queued', 'running'].includes(current.status)) return current;
  if (current.status === 'failed' && !current.resumable) throw new UserError('lifecycle_expired', '维护预览已失效，请重新预览');
  onPrepared({ ...record, dispatched: true, settled: false });
  const value = await invoke(client, record.job, 'dispatch', record.id, options.password, options.eraseInstanceId);
  return receipt(value, record);
}

module.exports = { validateRecord, receipt, script, GATEWAY, status, prepare, submit };
