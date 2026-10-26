'use strict';

const { UserError } = require('./contracts.cjs');

// This probe contains no user-controlled shell text and makes no system changes.
const PROBE_COMMAND = `
export LC_ALL=C
printf '\\n__GAOJI_OS__\\n'
cat /etc/os-release 2>/dev/null
printf '\\n__GAOJI_ARCH__\\n'
uname -m
printf '\\n__GAOJI_UID__\\n'
id -u
printf '\\n__GAOJI_DISK__\\n'
df -Pk / | tail -n 1
printf '\\n__GAOJI_SYSTEMD__\\n'
if test -d /run/systemd/system; then printf 'yes\\n'; else printf 'no\\n'; fi
printf '\\n__GAOJI_DOCKER__\\n'
if command -v docker >/dev/null 2>&1; then command -v docker; fi
printf '\\n__GAOJI_NIX__\\n'
if command -v nix >/dev/null 2>&1; then command -v nix; fi
printf '\\n__GAOJI_SUDO__\\n'
if test "$(id -u)" = 0; then printf 'root\\n'; elif command -v sudo >/dev/null 2>&1; then
  if sudo -n true </dev/null 2>/dev/null; then printf 'passwordless\\n'; else printf 'password-required\\n'; fi
else printf 'unavailable\\n'; fi
printf '\\n__GAOJI_DONE__\\n'
`;

function parseProbe(output) {
  if (typeof output !== 'string' || output.length > 128 * 1024 || !output.includes('__GAOJI_DONE__')) {
    throw new UserError('incomplete_probe', '服务器检测没有完成，请重新连接');
  }
  const sections = {};
  let section;
  for (const line of output.split(/\r?\n/)) {
    const match = /^__GAOJI_([A-Z]+)__$/.exec(line);
    if (match) {
      if (Object.hasOwn(sections, match[1])) throw new UserError('invalid_probe', '服务器检测返回重复字段');
      section = match[1]; sections[section] = [];
    } else if (section && line.trim()) sections[section].push(line.trim());
  }
  const os = {};
  for (const line of sections.OS || []) {
    const match = /^(ID|VERSION_ID|PRETTY_NAME)=([\s\S]*)$/.exec(line);
    if (match) {
      let value = match[2];
      if (value.startsWith('"') && value.endsWith('"') || value.startsWith("'") && value.endsWith("'")) value = value.slice(1, -1);
      os[match[1]] = value;
    }
  }
  const single = name => sections[name]?.length === 1 ? sections[name][0] : '';
  const disk = single('DISK').split(/\s+/);
  const uid = single('UID');
  if (!/^[a-z0-9_-]+$/.test(os.ID || '') || !/^[0-9.]+$/.test(os.VERSION_ID || '') || !/^\d+$/.test(uid)
      || disk.length < 6 || !/^\d+$/.test(disk[3])) {
    throw new UserError('invalid_probe', '无法识别服务器系统或磁盘信息');
  }
  const freeBytes = Number(disk[3]) * 1024;
  if (!Number.isSafeInteger(freeBytes)) throw new UserError('invalid_probe', '服务器磁盘数据无效');
  const privilege = single('SUDO');
  if (!['root', 'passwordless', 'password-required', 'unavailable'].includes(privilege)) throw new UserError('invalid_probe', '无法确认安装权限');
  return { os: os.ID, version: os.VERSION_ID, label: os.PRETTY_NAME || os.ID,
    arch: single('ARCH'), uid: Number(uid), freeBytes, systemd: single('SYSTEMD') === 'yes',
    dockerPath: single('DOCKER'), nixPath: single('NIX'), privilege };
}

function deploymentPlan(probe) {
  const blockers = [];
  const warnings = [];
  const supported = probe.os === 'nixos' || probe.os === 'debian' && ['12', '13'].includes(probe.version)
    || probe.os === 'ubuntu' && ['22.04', '24.04'].includes(probe.version);
  if (!supported) blockers.push('第一版尚未验证这个系统版本，不会自动修改它');
  if (probe.arch !== 'x86_64') blockers.push('当前安装链路先验证 x86_64，其他架构暂不执行');
  if (!probe.systemd) blockers.push('服务器需要 systemd 管理后台服务');
  if (probe.privilege === 'unavailable') blockers.push('此账户没有可用的管理员安装方式');
  if (probe.freeBytes < 10 * 1024 ** 3) blockers.push('至少需要 10 GiB 可用空间完成基础安装');
  if (probe.os === 'nixos' && !probe.nixPath) blockers.push('找不到 Nix，无法安装 NixOS 运行组件');
  if (probe.privilege === 'password-required') warnings.push('安装时需要输入服务器的 sudo 密码');
  if (probe.dockerPath) warnings.push('检测到已有 Docker；将核对可用性，不卸载或清空原有环境');
  warnings.push('QQ 需要用户扫码及可能的设备确认；不会复制开发者登录状态');
  warnings.push('将注册本实例的管理与沙盒服务；沙盒未启用时不会启动运行服务');
  return { eligible: blockers.length === 0, adapter: probe.os === 'nixos' ? 'nixos' : 'apt', blockers, warnings,
    steps: [
      { id: 'prepare', title: '检查安装权限与独立目录' },
      { id: 'engine', title: probe.dockerPath ? '核对隔离运行环境' : '安装隔离运行环境' },
      { id: 'runtime', title: '安装机器人和 QQ 接入组件' },
      { id: 'database', title: '创建独立数据库' },
      { id: 'service', title: '配置后台常驻与重启恢复' },
      { id: 'verify', title: '检查组件健康状态' },
    ],
    boundaries: ['数据库和管理接口默认不向公网开放', '不修改现有机器人或其他人的服务', '不删除用户数据或全局清理镜像', '不执行整机 NixOS rebuild'] };
}

module.exports = { PROBE_COMMAND, parseProbe, deploymentPlan };
