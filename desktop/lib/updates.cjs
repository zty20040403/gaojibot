'use strict';

const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { UserError, shellLiteral } = require('./contracts.cjs');
const { execute, inspectServer } = require('./ssh.cjs');
const deploy = require('./deploy.cjs');
const control = require('./control.cjs');

function digest(value) {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new UserError('invalid_update', '运行版本摘要无效');
  return value;
}
function validateRecord(value) {
  if (!value || !['update', 'rollback'].includes(value.kind)) throw new UserError('invalid_update', '版本任务无效');
  const record = { id: deploy.jobId(value.id), job: deploy.jobId(value.job), kind: value.kind,
    expected_checksum: digest(value.expected_checksum), target_checksum: digest(value.target_checksum),
    revision: value.revision, runnerChecksum: digest(value.runnerChecksum) };
  if (!Number.isSafeInteger(record.revision) || record.revision < 1) throw new UserError('invalid_update', '配置版本无效');
  if (record.kind === 'rollback') {
    record.rollback_id = deploy.jobId(value.rollback_id);
    if (value.confirm_data_replacement !== true) throw new UserError('confirmation_required', '请确认回退会恢复升级前数据');
    record.confirm_data_replacement = true;
  }
  return record;
}
async function plan(client, directory, deployment) {
  const manifest = deploy.bundle(directory);
  const access = await control.accessFor(client, deployment.job);
  let current;
  try { current = await control.request(client, access, 'versions'); }
  catch (error) {
    if (error.code !== 'setup_unsupported') throw error;
    const status = await control.request(client, access, 'status');
    const installed = await deploy.status(client, deployment.job);
    current = { checksum: digest(installed.checksum), version: deployment.version,
      revision: status.instance.revision, rollbacks: [], operation: status.operation,
      maintenance_pending: status.maintenance_pending === true, legacy: true };
  }
  if (!current || !Number.isSafeInteger(current.revision) || current.revision < 1 || !Array.isArray(current.rollbacks)) {
    throw new UserError('invalid_update', '服务器版本信息无效');
  }
  digest(current.checksum);
  const available = { version: manifest.version, checksum: digest(manifest.files['gaoji-runtime.tar.gz'].sha256) };
  const points = current.rollbacks.map(point => ({ id: deploy.jobId(point.id), target_checksum: digest(point.target_checksum),
    version: String(point.version).slice(0,80), created_at: Number(point.created_at) || 0 }));
  return { current: { checksum: current.checksum, version: String(current.version).slice(0,80), revision: current.revision,
    maintenance_pending: current.maintenance_pending === true, operation: current.operation || null, legacy: current.legacy === true },
    available, rollbacks: points, canUpdate: available.checksum !== current.checksum };
}
function launchScript(recordValue, staging, manifest, ownerUid) {
  const record = validateRecord(recordValue);
  if (!/^\/tmp\/gaoji-upload\.[a-zA-Z0-9]{10}$/.test(staging) || !Number.isInteger(ownerUid) || ownerUid < 0 || ownerUid >= 2 ** 31) {
    throw new UserError('invalid_update', '上传目录或登录账户身份无效');
  }
  if (digest(manifest.files['gaoji-runtime.tar.gz'].sha256) !== record.runnerChecksum) throw new UserError('invalid_update', '执行器运行包已变化');
  const { runnerChecksum, ...request } = record;
  const copies = deploy.FILES.map(name => {
    const hash = digest(manifest.files[name].sha256);
    return `test ! -L "$work/${name}"\nif ! test -e "$work/${name}"; then install -m 600 ${shellLiteral(staging + '/' + name)} "$work/${name}"; fi\nprintf '%s  %s\\n' '${hash}' "$work/${name}" | sha256sum --check --status`;
  }).join('\n');
  return `set -eu
umask 077
root=/var/lib/gaoji-app
test ! -L "$root"
test "$(stat -c %u "$root")" = 0
jobroot="$root/bootstrap/${record.job}"
test ! -L "$jobroot"
test ! -L "$jobroot/owner.json"
test "$(cat "$jobroot/owner.json")" = '{"uid":${ownerUid}}'
work="$jobroot/${runnerChecksum}"
test ! -L "$work"
install -d -m 700 "$work"
${copies}
${require('./image-policy.cjs').script(manifest)}
test ! -L "$work/requests"
install -d -m 700 "$work/requests"
request="$work/requests/${record.id}.json"
test ! -L "$request"
if test -e "$request"; then
  test "$(cat "$request")" = ${shellLiteral(JSON.stringify(request))}
else
  printf '%s\\n' ${shellLiteral(JSON.stringify(request))} > "$request"
fi
python=/var/lib/gaoji-app/tools/bin/python3
if ! test -x "$python"; then python=$(command -v python3); fi
PYTHONPATH= PYTHONHOME= PYTHONDONTWRITEBYTECODE=1 "$python" "$work/installer.py" --job ${record.job} --archive "$work/gaoji-runtime.tar.gz" --sha256 ${runnerChecksum} --prepare-update --operation ${record.id}
`;
}
async function send(client, directory, record, password, onPrepared) {
  const manifest = deploy.bundle(directory);
  const inspection = await inspectServer(client);
  if (inspection.probe.privilege === 'password-required' && !password) throw new UserError('sudo_password_required', '此操作需要服务器 sudo 密码');
  const result = await execute(client, 'umask 077\nmktemp -d /tmp/gaoji-upload.XXXXXXXXXX');
  const staging = result.stdout.trim();
  if (result.code !== 0 || !/^\/tmp\/gaoji-upload\.[a-zA-Z0-9]{10}$/.test(staging)) throw new UserError('upload_failed', '无法创建更新上传目录');
  let copied = false;
  try {
    for (const name of deploy.FILES) await deploy.upload(client, path.join(directory, name), staging + '/' + name);
    const script = launchScript(record, staging, manifest, inspection.probe.uid);
    onPrepared(record);
    const accepted = await deploy.privileged(client, script, inspection.probe.privilege, password);
    if (accepted.code !== 0) throw new UserError('update_unconfirmed', '更新是否启动尚未确认，请查询或继续原任务，不要重复更新');
    copied = true;
    return status(client, record);
  } finally {
    if (copied) await execute(client, `rm -f ${deploy.FILES.map(name => shellLiteral(staging + '/' + name)).join(' ')}\nrmdir ${shellLiteral(staging)}`).catch(() => {});
  }
}
async function status(client, recordValue) {
  const record = validateRecord(recordValue);
  const response = await execute(client, `cat /var/lib/gaoji-app/status/updates/${record.id}.json`, { maxBytes: 32768 });
  if (response.code !== 0) return { id: record.id, job: record.job, target_checksum: record.target_checksum,
    status: 'unconfirmed', stage: 'dispatch', message: '尚未确认服务器是否已接收，继续时沿用原任务', resumable: true, finished: false };
  let value;
  try { value = JSON.parse(response.stdout); } catch { throw new UserError('invalid_update_status', '更新记录暂不可读，请稍后再查'); }
  if (value.schema !== 1 || value.id !== record.id || value.job !== record.job || value.target_checksum !== record.target_checksum
      || !['queued', 'running', 'failed', 'completed'].includes(value.status) || typeof value.stage !== 'string' || typeof value.message !== 'string') {
    throw new UserError('invalid_update_status', '服务器返回的更新记录与此任务不符');
  }
  if (['queued', 'running'].includes(value.status)) {
    const unit = await execute(client, `systemctl show gaoji-app-update-${record.id}.service --property=ActiveState --value`, { maxBytes: 4096 });
    if (unit.code === 0 && ['inactive', 'failed'].includes(unit.stdout.trim())) return { ...value, status: 'interrupted',
      message: '更新执行器已经停止，数据与任务记录保留，可继续原任务核对', resumable: true, finished: false };
  }
  return value;
}
async function start(client, directory, server, options, onPrepared) {
  if (!options || Object.keys(options).some(key => !['id', 'kind', 'revision', 'expectedChecksum', 'rollbackId', 'confirm', 'password'].includes(key))
      || !['update', 'rollback'].includes(options.kind) || options.confirm !== true) throw new UserError('invalid_update', '请确认版本操作');
  if (server.update) {
    const previous = await status(client, server.update);
    if (previous.finished !== true && previous.status !== 'completed') throw new UserError('update_busy', '上一个版本任务尚未结束，请先继续或查询原任务');
  }
  const details = await plan(client, directory, server.deployment);
  if (details.current.checksum !== options.expectedChecksum || details.current.revision !== options.revision) throw new UserError('update_conflict', '配置或版本已变化，请刷新后确认');
  if (details.current.maintenance_pending || ['queued', 'running'].includes(details.current.operation?.status)) throw new UserError('update_busy', '实例还有未完成操作，暂不能更新');
  const point = options.kind === 'rollback' ? details.rollbacks.find(item => item.id === options.rollbackId) : null;
  if (options.kind === 'rollback' && !point || options.kind === 'update' && !details.canUpdate) throw new UserError('invalid_update', '所选版本不可用于此操作');
  const record = validateRecord({ id: randomUUID().replaceAll('-', ''), job: server.deployment.job, kind: options.kind,
    revision: options.revision, expected_checksum: options.expectedChecksum, target_checksum: point?.target_checksum || details.available.checksum,
    runnerChecksum: details.available.checksum, ...(point ? { rollback_id: point.id, confirm_data_replacement: true } : {}) });
  return send(client, directory, record, options.password, onPrepared);
}
async function resume(client, directory, recordValue, password, onPrepared) {
  const record = validateRecord(recordValue);
  const current = await status(client, record);
  if (current.finished === true || ['completed', 'queued', 'running'].includes(current.status)) return current;
  if (current.status === 'unconfirmed') return send(client, directory, record, password, onPrepared);
  const inspection = await inspectServer(client);
  const result = await deploy.privileged(client, `systemctl start --no-block gaoji-app-update-${record.id}.service`, inspection.probe.privilege, password);
  if (result.code !== 0) throw new UserError('update_unconfirmed', '尚未确认更新任务继续，请检查权限后查询原任务');
  return { ...current, status: 'queued', message: '已请求继续原版本任务' };
}

module.exports = { validateRecord, plan, launchScript, status, start, resume };
