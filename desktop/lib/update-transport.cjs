'use strict';

const { Readable } = require('node:stream');

// Electron net.fetch rejects manual redirects instead of exposing their response.
// Return each redirect to the updater so it can validate the next host first.
function updateTransport(net) {
  return (url, { signal, headers = {} } = {}) => new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(signal.reason); return; }
    const request = net.request({ url, method: 'GET', redirect: 'manual', credentials: 'omit',
      useSessionCookies: false, cache: 'no-store' });
    let settled = false;
    let incoming;
    const fail = error => {
      if (!settled) { settled = true; reject(error); }
      else incoming?.destroy(error);
    };
    const abort = () => { fail(signal.reason); request.abort(); };
    signal?.addEventListener('abort', abort, { once: true });
    request.once('close', () => signal?.removeEventListener('abort', abort));
    request.on('error', fail);
    request.once('redirect', (status, _method, location) => {
      if (settled) return;
      settled = true;
      resolve(new Response(null, { status, headers: { location } }));
      request.abort();
    });
    request.once('response', response => {
      incoming = response;
      if (settled) { response.destroy(); return; }
      try {
        const responseHeaders = new Headers();
        for (const [name, values] of Object.entries(response.headers)) {
          for (const value of Array.isArray(values) ? values : [values]) responseHeaders.append(name, value);
        }
        const empty = [204, 205, 304].includes(response.statusCode);
        const result = new Response(empty ? null : Readable.toWeb(response), { status: response.statusCode, headers: responseHeaders });
        settled = true;
        resolve(result);
        if (empty) response.resume();
      } catch (error) { fail(error); request.abort(); }
    });
    for (const [name, value] of new Headers(headers)) request.setHeader(name, value);
    request.end();
  });
}

module.exports = { updateTransport };
