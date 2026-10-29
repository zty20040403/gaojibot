'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { UserError, shellLiteral } = require('./contracts.cjs');
const { execute, inspectServer } = require('./ssh.cjs');
const imagePolicy = require('./image-policy.cjs');
const platformOptions = require('./platform-options.cjs');
const applications = require('./applications.cjs');

const FILES = ['bootstrap.sh', 'installer.py', 'environment.nix', 'gaoji-runtime.tar.gz'];
function jobId(value) {
  if (typeof value !== 'string' || !/^[a-f0-9]{32}$/.test(value)) throw new UserError('invalid_job', '安装任务编号无效');
  return value;
}
function bundle(directory) {
  let manifest;
  try { manifest = JSON.parse(fs.readFileSync(path.join(directory, 'deployment.json'), 'utf8')); }
  catch { throw new UserError('missing_runtime', '这个客户端尚未包含完整运行包，请安装完整版本'); }
  if (manifest.schema !== 1 || Object.keys(manifest.files || {}).sort().join(',') !== [...FILES].sort().join(',')) {
    throw new UserError('invalid_runtime', '运行包清单无效，请重新安装客户端');
  }
  for (const name of FILES) {
    const filename = path.join(directory, name);
    const metadata = manifest.files[name];
    if (!fs.lstatSync(filename).isFile() || fs.statSync(filename).size !== metadata.bytes || metadata.bytes > 512 * 1024 ** 2
        || createHash('sha256').update(fs.readFileSync(filename)).digest('hex') !== metadata.sha256) {
      throw new UserError('invalid_runtime', '运行包校验未通过，未执行安装');
    }
  }
  imagePolicy.catalog(manifest);
  return manifest;
}
function upload(client, source, target) {
  return new Promise((resolve, reject) => {
    client.sftp((error, sftp) => {
      if (error) { reject(new UserError('upload_failed', '无法开启加密文件传输')); return; }
      const timer = setTimeout(() => { sftp.end(); reject(new UserError('upload_timeout', '上传超时，尚未启动服务器安装')); }, 300000);
      sftp.fastPut(source, target, { mode: 0o600 }, cause => {
        clearTimeout(timer); sftp.end();
        if (cause) reject(new UserError('upload_failed', '运行包上传失败，尚未启动安装'));
        else resolve();
      });
    });
  });
}

function launchScript(job, staging, manifest, ownerUid = 0, previousChecksum, options, application, selectedPlatforms) {
  jobId(job);
  if (!Number.isInteger(ownerUid) || ownerUid < 0 || ownerUid >= 2 ** 31) throw new UserError('invalid_owner', '安装账户身份无效');
  if (!/^\/tmp\/gaoji-upload\.[a-zA-Z0-9]{10}$/.test(staging)) throw new UserError('invalid_staging', '服务器临时目录无效');
  const checksum = manifest.files['gaoji-runtime.tar.gz'].sha256;
  if (previousChecksum && !/^[a-f0-9]{64}$/.test(previousChecksum)) throw new UserError('invalid_runtime', '原安装包校验信息无效');
  for (const name of FILES) if (!/^[a-f0-9]{64}$/.test(manifest.files[name].sha256)) throw new UserError('invalid_runtime', '运行包校验信息无效');
  const copies = FILES.map(name => `test ! -L "$work/${name}"\nif ! test -e "$work/${name}"; then install -m 600 ${shellLiteral(staging + '/' + name)} "$work/${name}"; fi\nprintf '%s  %s\\n' '${manifest.files[name].sha256}' "$work/${name}" | sha256sum --check --status`).join('\n');
  let appCopies = 'test ! -e "$work/application.json"';
  if (application) {
    application = applications.siteRequest(application);
    if (previousChecksum) throw new UserError('invalid_application', '网站任务不能替换原运行包');
    const data = shellLiteral(JSON.stringify(application));
    appCopies = `test ! -L "$work/application.json"
if test -e "$work/application.json"; then
  test "$(cat "$work/application.json")" = ${data}
else
  printf '%s\\n' ${data} > "$work/application.json"
fi`;
    if (application.content_sha256) appCopies += `
test ! -L "$work/website.tar.gz"
if ! test -e "$work/website.tar.gz"; then install -m 600 ${shellLiteral(staging + '/website.tar.gz')} "$work/website.tar.gz"; fi
printf '%s  %s\\n' '${application.content_sha256}' "$work/website.tar.gz" | sha256sum --check --status`;
  }
  const selected = platformOptions.platforms(selectedPlatforms);
  const platformData = shellLiteral(JSON.stringify(selected));
  const platformScript = application ? '' : `test ! -L "$jobroot/platforms.json"
if test -e "$jobroot/platforms.json"; then
  test "$(cat "$jobroot/platforms.json")" = ${platformData}
else
  printf '%s\\n' ${platformData} > "$jobroot/platforms.json"
fi`;
  // Only validated IDs and hashes enter this fixed privileged program. Credentials
  // travel through SSH stdin, not through command arguments, files or service units.
  return `set -eu
umask 077
root=/var/lib/gaoji-app
test ! -L "$root"
if test -e "$root"; then test "$(stat -c %u "$root")" = 0; fi
install -d -m 755 "$root" "$root/status"
install -d -m 700 "$root/bootstrap"
jobroot="$root/bootstrap/${job}"
test ! -L "$jobroot"
install -d -m 700 "$jobroot"
work="$jobroot/${checksum}"
test ! -L "$work"
install -d -m 700 "$work"
${copies}
${appCopies}
${platformScript}
state=$(systemctl show gaoji-app-install-${job}.service --property=ActiveState --value 2>/dev/null || true)
case "$state" in active|activating|reloading|deactivating) exit 1;; esac
${imagePolicy.script(manifest, options)}
if test -e "$jobroot/owner.json"; then
  test ! -L "$jobroot/owner.json"
  test "$(cat "$jobroot/owner.json")" = '{"uid":${ownerUid}}'
else
  printf '%s\\n' '{"uid":${ownerUid}}' > "$jobroot/owner.json"
fi
units=/usr/local/lib/systemd/system
test ! -L "$units"
install -d -m 755 "$units" "$units/multi-user.target.wants"
unit=$units/gaoji-app-install-${job}.service
test ! -L "$unit"
if test -e "$unit"; then
  ${previousChecksum ? `header=$(head -n 1 "$unit")
  if test "$header" != '# Gaoji installer ${job} ${checksum}'; then
    test "$header" = '# Gaoji installer ${job} ${previousChecksum}'
    state=$(systemctl show "gaoji-app-install-${job}.service" --property=ActiveState --value)
    case "$state" in failed|inactive) ;; *) exit 1;; esac
    python=/var/lib/gaoji-app/tools/bin/python3
    if ! test -x "$python"; then python=$(command -v python3); fi
    "$python" "$work/installer.py" --job ${job} --archive "$work/gaoji-runtime.tar.gz" --sha256 ${checksum} --previous-sha256 ${previousChecksum} --validate-repair
  fi` : `test "$(head -n 1 "$unit")" = '# Gaoji installer ${job} ${checksum}'`}
fi
if ! test -e "$unit" || test "$(head -n 1 "$unit")" != '# Gaoji installer ${job} ${checksum}'; then
  printf '%s\\n' '# Gaoji installer ${job} ${checksum}' '[Unit]' 'Description=Gaoji instance installation ${job}' 'After=network-online.target' 'Wants=network-online.target' '[Service]' 'Type=oneshot' 'RemainAfterExit=yes' 'ExecStart=/bin/sh /var/lib/gaoji-app/bootstrap/${job}/${checksum}/bootstrap.sh ${job} ${checksum}${previousChecksum ? ' ' + previousChecksum : ''}' 'TimeoutStartSec=infinity' '[Install]' 'WantedBy=multi-user.target' > "$work/install.service"
  install -m 644 "$work/install.service" "$unit"
fi
link=$units/multi-user.target.wants/gaoji-app-install-${job}.service
if test -L "$link"; then
  test "$(readlink "$link")" = '../gaoji-app-install-${job}.service'
else
  test ! -e "$link"
  ln -s '../gaoji-app-install-${job}.service' "$link"
fi
if ! test -e "$root/status/${job}.json"; then
  printf '%s\\n' '{"schema":1,"job":"${job}","stage":"prepare","completed":[],"status":"queued","message":"服务器已接收安装任务"}' > "$root/status/${job}.json"
  chmod 644 "$root/status/${job}.json"
fi
systemctl daemon-reload
systemctl start --no-block "gaoji-app-install-${job}.service"
`;
}
function privilegedCommand(command, privilege, password) {
  if (privilege === 'root') return { command };
  // This is the fixed program above, not a renderer-supplied shell argument.
  const script = "'" + command.replaceAll("'", "'\\''") + "'";
  if (privilege === 'passwordless') return { command: `sudo -n -- sh -c ${script}` };
  if (privilege !== 'password-required' || typeof password !== 'string' || !password || /[\r\n\0]/.test(password) || password.length > 8192) {
    throw new UserError('sudo_password_required', '安装需要此服务器的 sudo 密码');
  }
  return { command: `sudo -S -p '' -- sh -c ${script}`, stdin: password + '\n' };
}
async function privileged(client, command, privilege, password, { maxBytes = 1024 * 1024 } = {}) {
  const request = privilegedCommand(command, privilege, password);
  return execute(client, request.command, { stdin: request.stdin, timeoutMs: 45000, maxBytes });
}
async function start(client, directory, { job, password, onPrepared, previousChecksum, imageOptions, application, contentFile, platforms }) {
  imageOptions = imagePolicy.options(imageOptions);
  const manifest = bundle(directory);
  const inspection = await inspectServer(client);
  if (application) {
    application = applications.siteRequest(application);
    const probe = inspection.probe;
    if (!applications.supported(application.app_id, probe) || !probe.systemd || probe.privilege === 'unavailable') throw new UserError('server_not_ready', '网站需要 Ubuntu x86_64、systemd 和安装权限');
    if (['install', 'update'].includes(application.operation) && probe.freeBytes < applications.application(application.app_id).minimumFreeBytes) throw new UserError('server_not_ready', '网站部署至少需要 2 GiB 可用空间');
    if (application.content_sha256 && (!contentFile || !fs.lstatSync(contentFile).isFile() || fs.lstatSync(contentFile).isSymbolicLink()
        || fs.statSync(contentFile).size > 136 * 1024 ** 2 || createHash('sha256').update(fs.readFileSync(contentFile)).digest('hex') !== application.content_sha256)) throw new UserError('invalid_website', '本地网站上传包校验失败，请重新选择');
  } else if (!inspection.plan.eligible) throw new UserError('server_not_ready', inspection.plan.blockers.join('；'));
  if (inspection.probe.privilege === 'password-required' && !password) throw new UserError('sudo_password_required', '请先输入服务器 sudo 密码');
  const id = jobId(job || randomUUID().replaceAll('-', ''));
  const result = await execute(client, 'umask 077\nmktemp -d /tmp/gaoji-upload.XXXXXXXXXX');
  const staging = result.stdout.trim();
  if (result.code !== 0 || !/^\/tmp\/gaoji-upload\.[a-zA-Z0-9]{10}$/.test(staging)) throw new UserError('upload_failed', '无法创建服务器上传目录');
  let transferred = false;
  try {
    for (const name of FILES) await upload(client, path.join(directory, name), staging + '/' + name);
    if (application?.content_sha256) await upload(client, contentFile, staging + '/website.tar.gz');
    // Persist before dispatch: a lost reply must not create a second instance.
    onPrepared({ job: id, checksum: manifest.files['gaoji-runtime.tar.gz'].sha256, version: manifest.version, startedAt: Date.now(), imageOptions, ...(!application ? { platforms: platformOptions.platforms(platforms) } : {}), ...(application ? { application } : {}), ...(previousChecksum ? { previousChecksum } : {}) });
    const launched = await privileged(client, launchScript(id, staging, manifest, inspection.probe.uid, previousChecksum, imageOptions, application, platforms), inspection.probe.privilege, password);
    if (launched.code !== 0) throw new UserError('install_not_started', '安装尚未确认启动，请先查询任务状态；可能需要重新输入 sudo 密码');
    transferred = true;
    return { job: id, status: 'queued', stage: 'prepare', completed: [], message: '服务器已接收安装任务' };
  } finally {
    // Clean only this newly created upload directory, after the root-owned copy.
    if (transferred) await execute(client, `rm -f ${[...FILES, ...(application?.content_sha256 ? ['website.tar.gz'] : [])].map(name => shellLiteral(staging + '/' + name)).join(' ')}\nrmdir ${shellLiteral(staging)}`).catch(() => {});
  }
}
async function status(client, job) {
  const id = jobId(job);
  const result = await execute(client, `cat /var/lib/gaoji-app/status/${id}.json`, { maxBytes: 64 * 1024 });
  if (result.code !== 0) return { job: id, status: 'unconfirmed', stage: 'prepare', completed: [], message: '尚未确认服务器是否已接收任务，请重新查询；不会重复创建实例' };
  let value;
  try { value = JSON.parse(result.stdout); } catch { throw new UserError('invalid_status', '服务器安装状态暂不可读，请稍后重试'); }
  if (value.schema !== 1 || value.job !== id || !Array.isArray(value.completed)
      || value.checksum !== undefined && !/^[a-f0-9]{64}$/.test(value.checksum)
      || !['queued', 'waiting', 'running', 'failed', 'awaiting_setup', 'completed'].includes(value.status)) throw new UserError('invalid_status', '服务器返回了无效的安装状态');
  if (['queued', 'waiting', 'running', 'failed'].includes(value.status)) {
    const unit = await execute(client, `systemctl show gaoji-app-install-${id}.service --property=ActiveState --value`, { maxBytes: 4096 });
    if (value.status === 'failed') {
      if (unit.code === 0 && unit.stdout.trim() === 'activating') return { ...value, status: 'queued', message: '原任务正在继续，等待后台更新进度' };
      return value;
    }
    if (unit.code === 0 && unit.stdout.trim() === 'failed') {
      return { ...value, status: 'failed', message: '安装进程提前退出，已保留现有数据；可继续原安装任务' };
    }
    if (unit.code === 0 && unit.stdout.trim() === 'inactive') {
      const started = await execute(client, `systemctl show gaoji-app-install-${id}.service --property=ExecMainStartTimestampMonotonic --value`, { maxBytes: 4096 });
      if (started.code === 0 && /^[1-9]\d*$/.test(started.stdout.trim())) return { ...value, status: 'failed', message: '后台任务已停止，数据和进度已保留；可继续原任务' };
      return { ...value, status: 'unconfirmed', message: '尚未确认后台任务已启动，请查询或继续原任务；不会新建实例' };
    }
  }
  return value;
}

async function retry(client, directory, deployment, password, onPrepared, imageOptions = deployment.imageOptions) {
  imageOptions = imagePolicy.options(imageOptions);
  const current = await status(client, deployment.job);
  if (current.status !== 'failed' && current.status !== 'unconfirmed') return current;
  if (current.status === 'failed') {
    const manifest = bundle(directory);
    const repairPending = deployment.previousChecksum && current.checksum !== deployment.checksum;
    if (deployment.checksum !== manifest.files['gaoji-runtime.tar.gz'].sha256 || repairPending) {
      if (!['prepare', 'engine', 'runtime'].includes(current.stage)) throw new UserError('update_required', '此实例已有数据库，不能替换安装包；请使用实例更新功能');
      return start(client, directory, { job: deployment.job, password, onPrepared, imageOptions, platforms: deployment.platforms,
        previousChecksum: deployment.checksum !== manifest.files['gaoji-runtime.tar.gz'].sha256 ? deployment.checksum : deployment.previousChecksum });
    }
    const inspection = await inspectServer(client);
    const work = `/var/lib/gaoji-app/bootstrap/${jobId(deployment.job)}/${deployment.checksum}`;
    const result = await privileged(client, `set -eu\nstate=$(systemctl show gaoji-app-install-${deployment.job}.service --property=ActiveState --value)\ncase "$state" in failed|inactive) ;; *) exit 1;; esac\nwork=${shellLiteral(work)}\n${imagePolicy.script(manifest, imageOptions)}\nsystemctl start --no-block gaoji-app-install-${deployment.job}.service`, inspection.probe.privilege, password);
    if (result.code !== 0) throw new UserError('retry_failed', '未能继续原安装任务，请检查管理员密码和网络');
    const { previousChecksum: _previous, ...accepted } = deployment;
    onPrepared({ ...accepted, imageOptions });
    return { ...current, status: 'queued', message: '已请求继续原安装任务' };
  }
  const manifest = bundle(directory);
  if (manifest.files['gaoji-runtime.tar.gz'].sha256 !== deployment.checksum) throw new UserError('bundle_changed', '此任务使用的运行包与客户端不同，请先核对原任务状态');
  return start(client, directory, { job: deployment.job, password, onPrepared, previousChecksum: deployment.previousChecksum, imageOptions, platforms: deployment.platforms });
}

module.exports = { FILES, bundle, jobId, launchScript, privilegedCommand, privileged, upload, start, status, retry };
