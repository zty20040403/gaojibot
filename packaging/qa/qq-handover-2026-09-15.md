# QQ migration handover: 0.1.18

This is the historical 0.1.18 acceptance record. Subsequent real cross-guest
migration, reboot, handover and maintenance results are in
[migration-2026-09-15.md](migration-2026-09-15.md). The desktop transport defect
found by that acceptance requires the 0.1.19 client.

## Scope and defect

Cross-instance restore previously stopped NapCat immediately, but subsequent
Compose rendering still enabled it by default. A normal instance start could
therefore start the copied QQ session without explicit handover. The login page
also had no action to start that stopped component intentionally.

New cross-instance restores persist `config/qq-handover.json` before changing
live data. Pending handover excludes NapCat from the default Compose profile and
sets its restart policy to `no`. Failed restore restores the previous marker and
data. Same-instance restore cannot bypass an existing pending handover.

The authenticated `resume_qq` operation requires explicit confirmation and the
current configuration revision. It persists consent before starting QQ, verifies
an online or QR response, and leaves the Bot disabled. Lost acknowledgements use
the same operation ID. A failed startup can be explicitly retried; a crash after
verified readiness finishes the original operation without another launch.

This does not retroactively change restores completed by older versions.

## Evidence

- Python: 88 tests run, 87 passed, 1 skipped across handover, backup transfer,
  backup maintenance, manager, updates and lifecycle modules. The skipped case
  requires `GAOJI_TEST_PG_BIN`; no real PostgreSQL restore was tested this turn.
- The new cases cover pending profile persistence, controller reconstruction,
  invalid markers, confirmation and revision checks, duplicate requests, cold
  login readiness, timeout and retry, crash recovery, both rollback marker
  branches and same-instance restore while handover is pending.
- Node: 23 encryption, backup transport and management channel tests passed.
  These use local fixtures and do not represent a full two-server migration.
- Actual Electron and the final packaged Mac App passed `verify-setup-ui.cjs
  --handover`: explicit checkbox, no unavailable QR, refresh preserving consent,
  lost response retaining operation ID, explicit retry after failure, readiness
  displaying a QR without activating the Bot, and desktop/narrow layouts.
- That UI check exposed invisible progress on the QQ tab. Operation status now
  appears above both tabs. Final screenshots were visually inspected.
- Final Mac 0.1.18 also passed `verify-ui.cjs`: real launch and IPC, no Node in
  the renderer, password masking, responsive layout and encrypted localhost SSH.
- Mac and Windows archives passed package/source checks; Windows executable is
  x64 PE. Four output SHA-256 values were independently rechecked. DMG integrity
  was verified by `hdiutil`; both ZIP archives passed `unzip -tq` CRC checks.

Both packages use runtime checksum
`04d61cac62c4e75ca01e10c958b2f86f9317fa5651646bee918f1252b4e4dc19`,
with 276 runtime files. `desktop/release/release.json` and `SHA256SUMS` identify
the exact outputs. Packages remain unsigned development previews, not formal V1.

## Real Linux evidence and remaining work

The primary local Ubuntu guest previously completed its real update to the
0.1.17 runtime, operation `42ba5ce4a92a49088c2c9c10e2c717fd`, checksum
`ba31b0f903ac3546ae016882028e54edb728233a1b9add22ccbb183121320458`.
Its checksum-scoped acceptance record was re-read and shows completed/finished.
The associated QEMU session is gone and its recorded PIDs are absent.

Neither local guest was started this turn. Both still need the 0.1.18 runtime
before real cross-guest backup migration, because import requires matching
runtime checksums. No cross-guest restore, reboot handover or real QQ/model/file
delivery is claimed here. Windows runtime acceptance also remains outstanding.

No user server was connected, no h310/h610/tank service changed, no host Docker
resource touched, and no commit, push or rebuild was performed. Local test
windows and sockets were closed by their test cleanup.
