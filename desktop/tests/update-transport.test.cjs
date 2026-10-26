'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { Readable } = require('node:stream');
const { updateTransport } = require('../lib/update-transport.cjs');

function fixture(action) {
  let request;
  const net = { request(options) {
    assert.equal(options.redirect, 'manual');
    assert.equal(options.credentials, 'omit');
    request = new EventEmitter();
    request.setHeader = () => {};
    request.abort = () => { request.emit('error', new Error('Redirect was cancelled')); request.emit('close'); };
    request.end = () => queueMicrotask(() => action(request));
    return request;
  } };
  return updateTransport(net);
}

test('Electron redirect is exposed for host validation instead of rejected or followed', async () => {
  const fetch = fixture(request => request.emit('redirect', 302, 'GET', 'https://release-assets.githubusercontent.com/test'));
  const response = await fetch('https://github.com/test');
  assert.equal(response.status, 302);
  assert.equal(response.headers.get('location'), 'https://release-assets.githubusercontent.com/test');
  assert.equal(response.body, null);
});

test('Electron body streams without buffering and initial cancellation issues no request', async () => {
  const fetch = fixture(request => {
    const response = Readable.from([Buffer.from('complete installer')]);
    response.statusCode = 200;
    response.headers = { 'content-type': ['application/octet-stream'] };
    request.emit('response', response);
    response.once('end', () => request.emit('close'));
  });
  assert.equal(await (await fetch('https://github.com/test')).text(), 'complete installer');
  await assert.rejects(updateTransport({ request() { assert.fail('Cancelled request must not start'); } })('https://github.com/test', {
    signal: AbortSignal.abort(new Error('cancelled')),
  }), /cancelled/);
});

test('abort during a response propagates to the stream', async () => {
  const controller = new AbortController();
  const fetch = fixture(request => {
    const response = new Readable({ read() {} });
    response.statusCode = 200; response.headers = {};
    request.emit('response', response);
  });
  const response = await fetch('https://github.com/test', { signal: controller.signal });
  const body = response.text();
  controller.abort(new Error('cancelled'));
  await assert.rejects(body, /cancelled/);
});
