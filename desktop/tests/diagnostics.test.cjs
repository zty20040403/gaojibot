'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { diagnostics } = require('../lib/diagnostics.cjs');

test('runtime report exports only fixed status codes, never model names or raw failures', () => {
  const report = diagnostics('0.1.32', { runtime: { checked_at: 1700000000000, stale: false,
    key: 'secret', checks: [{ id: 'model', state: 'ok', detail: 'secret private-model', label: 'private' },
      { id: 'qq', state: 'secret' }, { id: 'secret', state: 'ok' }] } });
  assert.equal(report.runtime.checks.find(row => row.id === 'model').state, 'ok');
  assert.equal(report.runtime.checks.find(row => row.id === 'qq').state, 'unknown');
  assert.equal(report.runtime.stale, false);
  assert.doesNotMatch(JSON.stringify(report), /secret|private/);
  assert.equal(diagnostics('0.1.32', { runtime: { checked_at: Number.MAX_SAFE_INTEGER } }).runtime.checked_at, null);
});

test('diagnostics retain only controlled metrics and anonymize server identity', () => {
  const value = diagnostics('0.1.26', { version: 'untrusted', password: 'secret', servers: [{
    host: 'private.invalid', username: 'alice', name: 'private name', fingerprint: 'secret', key: 'secret',
    checked_at: '2026-09-16T12:00:00Z', status: 'eligible', os: 'ubuntu', arch: 'x86_64',
    free_bytes: 12345, privilege: 'passwordless', blockers: 0, warnings: 2,
  }] });
  assert.equal(value.app_version, '0.1.26');
  assert.equal(value.servers[0].server, 'server-1');
  assert.equal(value.servers[0].free_bytes, 12345);
  assert.doesNotMatch(JSON.stringify(value), /secret|private|alice|untrusted/);
});

test('unexpected values cannot leak into a diagnostic report', () => {
  const value = diagnostics('0.1.26', { servers: [null, { status: 'eligible', os: 'secret', arch: 'secret',
    privilege: 'secret', checked_at: 'secret', free_bytes: -1, blockers: Infinity, warnings: {} }] });
  assert.equal(value.servers[0].status, 'not_checked');
  assert.equal(value.servers[1].free_bytes, 0);
  assert.equal(value.servers[1].os, 'unknown');
  assert.doesNotMatch(JSON.stringify(value), /secret/);
  assert.equal(diagnostics('0.1.26', { servers: Array(1001).fill({}) }).servers.length, 1000);
});
