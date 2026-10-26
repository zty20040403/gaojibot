# App Update Redirect Fix

Date: 2026-09-17. Scope: desktop updates and local release cleanup only.

## Reproduction

The running App was 0.1.25 from the repository build directory. A separate
temporary profile reproduced its actual Electron transport: the public releases
API returned 200 and selected 0.1.28, but fetching app-update.json failed with
`Redirect was cancelled`. The effective macOS proxy was used correctly. No server
credentials or user profiles were read by this reproduction.

This is the documented Electron manual-redirect behavior, not evidence of low
disk space or an offline proxy:
https://www.electronjs.org/docs/latest/api/client-request
https://github.com/electron/electron/issues/43715

## Fix And Evidence

- The updater uses a dedicated net.request transport. Redirect responses are
  returned to the existing URL allowlist before any next request is made.
- Requests omit cookies/credentials; system proxy behavior and certificate
  validation remain intact. Responses stream without buffering the entire file.
- Eleven focused updater/transport checks passed, including cancellation,
  redirects, unsafe-host rejection and installer SHA-256 checks.
- The repaired transport inside a real Electron process downloaded all
  150799844 bytes of the public 0.1.28 DMG and passed the manifest SHA-256.
- The final packaged 0.1.29 includes the new transport; separate packaged
  verification uses a temporary profile, not the user's running App data.

## Cleanup

Two stopped, marker-identified local Ubuntu QA machines and 0.1.26/0.1.27
build outputs were removed after process and open-disk checks. Available disk
space increased from about 88 GiB to 107 GiB before building 0.1.29.
QA documentation remains; disposable guest disks/keys/fixtures do not.

Publication now invokes clean-release.cjs after final release verification.
It removes recognized older installers/App copies and stopped, marked QA guests,
but skips paths used by running processes and retains small manifests/notes.
It does not delete source, user profiles, connections, Docker data or servers.

The verified 0.1.29 App was installed at `/Applications/Gaoji.app` after the old
App exited. Its app.asar SHA-256 matched the built copy exactly:
`eb6ee7dbc065d38d353aedf6bcba8a17de69b28b57411ebb27ddcef82ee390dd`.
The normal user profile reopened with its saved h310 connection, without connecting
to that server. Old 0.1.25 downloads, old build App copies, duplicate unpacked
0.1.29 builds and unused test virtualenvs were deleted. Disk availability settled
at about 108 GiB. Latest installers occupy 597 MiB; small QA records remain.

0.1.29 was published after successful assembly run 35120640263:
https://github.com/zty20040403/gaoji-app/releases/tag/app-v0.1.29 .
Eight final assets were checked; no installer parts were published.

## Limits

This is not QQ/model/server acceptance. Unsigned App limitations remain.
0.1.28 Windows CI installed NSIS and passed real IPC/SSH/layout, then failed
the credential/update step; uninstall was skipped. Its run is
https://github.com/zty20040403/gaoji-app/actions/runs/35118016432 .
Do not treat that partial run as full Windows acceptance.
