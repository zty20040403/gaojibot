# App Workspace 0.1.26

Date: 2026-09-16. Local preview build; not published to GitHub Releases in this change.

## Changes

- New floral doodle g artwork, packaged as PNG, macOS ICNS and Windows ICO.
- Compact workspace with saved connections, current-session inspection counts and environment blockers.
- Server search, name/recent sorting, pinning and pinned/attention filters.
- Forest, ocean, blossom and graphite palettes, each with light/dark/system appearance.
  Preferences persist locally; credentials are not stored in these preferences.
- Native save dialog for diagnostic export. Main-process allowlist retains only anonymous server numbers,
  controlled OS/architecture/privilege/status values, timestamps and numeric metrics.
  Hostnames, user names, passwords, fingerprints and raw error strings are excluded.
- Existing local/remote setup, model configuration, storage, backups and lifecycle paths retained.

## Evidence

- TypeScript and Vite production build passed.
- Two focused diagnostic allowlist tests passed, including malformed data and secret-like input.
- `tests/verify-workspace-ui.cjs` passed both from development files and the real packaged macOS app.
  Covers search, pin persistence, attention filtering, native diagnostic save, theme persistence,
  password masking and 1280px / 390px layouts. No renderer exceptions or horizontal overflow.
- Screenshots: `desktop/release/qa/workspace-0.1.26/{light,dark,mobile,connect-mobile}.png`.
- UI tests use temporary user data and synthetic server records; no real servers were contacted.
- Follow-up packaged macOS run also passed system light/dark media changes and persistence of
  the system-mode preference after reload. The four palettes and desktop/mobile checks passed again.
- Runtime archive remains version 0.20.0, SHA-256
  `22d3d1bad545e5304b8b6b320cef261d3aa01232c6130a2b239a1c6756865d9a`.
  This UI change does not replace or remove context logic in the deployment runtime.

## Boundaries

Environment inspection is not continuous bot health monitoring. Inspection records are session-local
and reset after App restart. The App does not automatically connect to saved servers in the background.
Windows execution and full server deployment were not re-tested for this UI change. Packages remain
unsigned preview builds. No server was deployed, restarted or upgraded. The installed App and the
public 0.1.25 release were not overwritten.

The 0.1.26 installers are now staged in an unpublished release draft. Windows acceptance stopped
before installation because its read-only workflow token cannot list draft releases. See
[Windows acceptance status](app-windows-0.1.26.md); a staged installer is not a passed Windows test.
