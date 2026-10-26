'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { ApplicationMonitor, botState, websiteState } = require('../lib/application-monitor.cjs');

function healthy() {
  return {
    settings: { instance: { revision: 2, settings: { bot_qq: '12345', default_model: 'local', models: [{ id: 'local' }] } }, desired: { revision: 2, bot_enabled: true } },
    health: { services: ['database', 'napcat', 'bot'].map(name => ({ name, state: 'running', health: 'healthy' })), connection: 'connected' },
  };
}

test('ready bot requires DB, applied settings, running services and fresh QQ heartbeat', () => {
  const { settings, health } = healthy();
  assert.equal(botState(settings, health).state, 'ready');
  for (const connection of ['stale', 'unavailable', 'not_running', undefined]) {
    assert.equal(botState(settings, { ...health, connection }).state, 'unknown');
  }
  assert.equal(botState(settings, { ...health, connection: 'wrong_account' }).state, 'attention');
  assert.equal(botState({ ...settings, desired: { revision: 1, bot_enabled: true } }, health).state, 'attention');
  assert.equal(botState({ ...settings, desired: { bot_enabled: false } }, health).state, 'stopped');
  health.services[0].health = 'starting';
  assert.equal(botState(settings, health).label, '数据库未就绪');
});

test('website status never equates running containers to confirmed HTTP success', () => {
  assert.equal(websiteState({ site: { status: 'running' }, health: 'ready' }).state, 'ready');
  assert.equal(websiteState({ site: { status: 'running' }, health: 'unknown' }).state, 'unknown');
  assert.equal(websiteState({ site: { status: 'running' }, health: 'unhealthy' }).state, 'attention');
  assert.equal(websiteState({ site: { status: 'running' }, health: 'ready', task: { status: 'failed', message: 'Candidate rejected; original remains' } }).state, 'attention');
  assert.equal(websiteState({ task: { status: 'unconfirmed' } }).state, 'unknown');
  assert.equal(websiteState({ site: { status: 'retained' } }).state, 'retained');
});

test('draft has no network work; failed read returns unknown without exposing raw errors', async () => {
  let calls = 0;
  const instance = { appId: 'gaoji' };
  const monitor = new ApplicationMonitor({ instance: () => instance }, {}, () => { calls++; throw Error('private-key-secret'); });
  assert.equal((await monitor.status('app:fixture')).state, 'draft');
  assert.equal(calls, 0);
  instance.deployment = { job: 'original' };
  const value = await monitor.status('app:fixture');
  assert.equal(value.state, 'unknown');
  assert.ok(value.checkedAt > 0);
  assert.ok(!JSON.stringify(value).includes('private-key-secret'));
});

test('single-flight read shares one check and uses only status/services, no model tests or activation', async () => {
  const { settings, health } = healthy();
  const calls = [];
  let resolve;
  const gate = new Promise(done => { resolve = done; });
  const instance = { deployment: { job: 'original' } };
  const monitor = new ApplicationMonitor({ instance: () => instance }, {}, async () => { await gate; return 'client'; }, {
    deploy: { status: async (_client, job) => { assert.equal(job, 'original'); return { status: 'awaiting_setup' }; } },
    control: { accessFor: async () => 'access', request: async (_client, _access, action) => { calls.push(action); return action === 'status' ? settings : health; } },
  });
  const first = monitor.status('fixture');
  const second = monitor.status('fixture');
  assert.equal(first, second);
  resolve();
  assert.equal((await first).state, 'ready');
  assert.deepEqual(calls, ['status', 'services']);
  assert.equal(monitor.pending.size, 0);
});

test('maintenance and updates take priority and cannot be hidden by old successful installation', async () => {
  const instance = { deployment: { job: 'original' }, lifecycle: { dispatched: true } };
  let lifecycle = { status: 'running' };
  const monitor = new ApplicationMonitor({ instance: () => instance }, {}, async () => 'client', {
    lifecycle: { status: async () => lifecycle },
    updates: { status: async () => ({ status: 'failed' }) },
    deploy: { status: async () => { throw Error('must not reach old installation'); } },
  });
  assert.equal((await monitor.status('fixture')).state, 'working');
  lifecycle = { status: 'completed', service_state: 'retained' };
  assert.equal((await monitor.status('fixture')).state, 'retained');
  delete instance.lifecycle;
  instance.update = {};
  assert.equal((await monitor.status('fixture')).state, 'attention');
});
