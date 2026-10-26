'use strict';

function diagnostics(version, input) {
  const rows = Array.isArray(input?.servers) ? input.servers.slice(0, 1000) : [];
  const choose = (value, options, fallback = 'unknown') => options.includes(value) ? value : fallback;
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value : 0;
  const runtime = input?.runtime;
  const checkIds = ['manager', 'database', 'napcat', 'bot', 'qq', 'connection', 'model', 'groups', 'configuration', 'components', 'maintenance'];
  const checks = Array.isArray(runtime?.checks) ? runtime.checks : [];
  return { schema: 1, app_version: version, exported_at: new Date().toISOString(),
    ...(runtime && typeof runtime === 'object' ? { runtime: {
      checked_at: Number.isSafeInteger(runtime.checked_at) && runtime.checked_at > 0 && runtime.checked_at <= 8640000000000000 ? new Date(runtime.checked_at).toISOString() : null,
      stale: runtime.stale !== false,
      checks: checkIds.map(id => ({ id, state: choose(checks.find(row => row?.id === id)?.state, ['ok', 'attention', 'unknown', 'off']) })),
    } } : {}), servers: rows.map((row, index) => {
    row = row && typeof row === 'object' ? row : {};
    const date = typeof row.checked_at === 'string' ? Date.parse(row.checked_at) : NaN;
    // Only controlled enums and numeric metrics cross into an exported report.
    return { server: `server-${index + 1}`, checked_at: Number.isFinite(date) ? new Date(date).toISOString() : null,
      status: choose(row.status, ['eligible', 'blocked', 'not_checked', 'inspection_failed'], 'not_checked'),
      ...(row.status === 'eligible' || row.status === 'blocked' ? {
        os: choose(row.os, ['ubuntu', 'debian', 'nixos', 'darwin', 'win32']),
        arch: choose(row.arch, ['x86_64', 'aarch64', 'arm64', 'x64']), free_bytes: count(row.free_bytes),
        privilege: choose(row.privilege, ['root', 'passwordless', 'password-required', 'unavailable']),
        blockers: count(row.blockers), warnings: count(row.warnings),
      } : {}),
    };
  }) };
}
module.exports = { diagnostics };
