'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { PROBE_COMMAND, parseProbe, deploymentPlan } = require('../lib/preflight.cjs');
const { connectionTarget, hostFingerprint, shellLiteral, instanceId } = require('../lib/contracts.cjs');

function fixture({ os = 'nixos', version = '26.05', sudo = 'passwordless', docker = '', arch = 'x86_64', disk = '191889408' } = {}) {
  return `\n__GAOJI_OS__\nID=${os}\nVERSION_ID="${version}"\nPRETTY_NAME="Server Test"\n__GAOJI_ARCH__\n${arch}\n__GAOJI_UID__\n1000\n__GAOJI_DISK__\n/dev/sda3 209715200 15728640 ${disk} 8% /\n__GAOJI_SYSTEMD__\nyes\n__GAOJI_DOCKER__\n${docker}\n__GAOJI_NIX__\n/run/current-system/sw/bin/nix\n__GAOJI_SUDO__\n${sudo}\n__GAOJI_DONE__\n`;
}

test('bare NixOS requires no preinstalled Docker or Python', () => {
  const probe = parseProbe(fixture());
  assert.equal(probe.os, 'nixos');
  assert.equal(probe.dockerPath, '');
  assert.equal(deploymentPlan(probe).eligible, true);
  assert.equal(deploymentPlan(probe).adapter, 'nixos');
  assert.ok(deploymentPlan(probe).steps.some(item => item.id === 'engine'));
});

test('supported Debian and Ubuntu are planned separately from NixOS', () => {
  for (const [os, version] of [['debian', '12'], ['debian', '13'], ['ubuntu', '22.04'], ['ubuntu', '24.04']]) {
    const plan = deploymentPlan(parseProbe(fixture({ os, version })));
    assert.equal(plan.eligible, true);
    assert.equal(plan.adapter, 'apt');
  }
});

test('unsupported OS, missing privileges, low disk and wrong architecture block install', () => {
  for (const options of [{ os: 'fedora', version: '44' }, { sudo: 'unavailable' }, { arch: 'aarch64' }, { disk: '1024' }]) {
    const plan = deploymentPlan(parseProbe(fixture(options)));
    assert.equal(plan.eligible, false);
    assert.ok(plan.blockers.length);
  }
});

test('password-required sudo is an explicit prompt, not an unsupported server', () => {
  const plan = deploymentPlan(parseProbe(fixture({ sudo: 'password-required' })));
  assert.equal(plan.eligible, true);
  assert.match(plan.warnings.join(' '), /sudo/);
});

test('existing engine is preserved rather than automatically replaced', () => {
  const plan = deploymentPlan(parseProbe(fixture({ docker: '/usr/bin/docker' })));
  assert.match(plan.warnings.join(' '), /不卸载/);
});

test('truncated or repeated probe sections are rejected', () => {
  assert.throws(() => parseProbe(fixture().replace('__GAOJI_DONE__', '')));
  assert.throws(() => parseProbe(fixture() + '\n__GAOJI_UID__\n0\n'));
  assert.throws(() => parseProbe(fixture({ disk: 'no-data' })));
  assert.throws(() => parseProbe(fixture({ sudo: 'whatever' })));
});

test('probe does not install, delete or restart anything', () => {
  assert.doesNotMatch(PROBE_COMMAND, /apt-get|nixos-rebuild|systemctl (start|restart|stop)|rm -|curl|wget/);
});

test('SSH targets reject shell and URL input, accept IPv4 IPv6 and domains', () => {
  for (const host of ['192.0.2.10', '2001:db8::1', 'server.example.com']) {
    assert.equal(connectionTarget({ host, username: 'alice' }).port, 22);
  }
  for (const host of ['server;reboot', 'http://server', 'server/abc', 'server$(id)', '-oProxyCommand=evil']) {
    assert.throws(() => connectionTarget({ host, username: 'alice' }));
  }
  assert.throws(() => connectionTarget({ host: 'example.com', username: 'root;id' }));
  assert.throws(() => connectionTarget({ host: 'example.com', username: 'root', port: '22' }));
});

test('fingerprint and instance identifiers cannot be bypassed with arbitrary text', () => {
  assert.equal(hostFingerprint('SHA256:' + 'a'.repeat(43)), 'SHA256:' + 'a'.repeat(43));
  assert.throws(() => hostFingerprint('ignore'));
  assert.equal(instanceId('a'.repeat(32)), 'a'.repeat(32));
  assert.throws(() => instanceId('../other'));
});

test('fixed installer arguments are escaped rather than interpolated', () => {
  assert.equal(shellLiteral("a'b"), `'a'"'"'b'`);
  assert.throws(() => shellLiteral('a\nb'));
});

module.exports = { fixture };
