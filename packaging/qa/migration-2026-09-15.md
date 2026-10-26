# Real cross-instance migration: desktop 0.1.19

## Boundary

Only the two existing, isolated local Ubuntu 24.04.5 x86_64 QEMU guests were
used. They bind SSH to localhost, have distinct generated QA identities and
disks, and do not mount user directories. The source was shut down before the
target restore and was not restarted during target acceptance. Neither instance
has configured model keys, a bot QQ number or a logged-in QQ account. Bot
activation stayed disabled throughout. No h310/h610/tank connection, production
service change, host Docker cleanup, commit, push or rebuild was performed.

This is the Linux server deployment product, not a Mac/Windows local Bot mode.
At completion the target was shut down normally and its QEMU process exited
with code 0. The source's exact QMP endpoint remained absent; no QA VM was left
running. The packaged Electron test also closed its temporary App and sockets.

## Versions and operation evidence

Both guests completed real updates from the 0.1.17 runtime to runtime checksum
`04d61cac62c4e75ca01e10c958b2f86f9317fa5651646bee918f1252b4e4dc19`
(276 files, runtime version 0.20.0). Desktop 0.1.19 changes only the client;
it uses that same runtime as 0.1.18, without another server image build.

| Operation | Durable ID | Result |
| --- | --- | --- |
| Source update | `13edddb442834e9f91f9016f8a68f436` | Completed |
| Target update | `831a71992e6147ae8d18753bd6f1d16a` | Completed |
| Source backup | `10a4c6098bc046dab29589ab414a57ae` | Completed, backed_up |
| Target restore | `b6369a27bea341f1a51618e441c34731` | Completed, restored |
| Target QQ handover | `babd147b587b4f3798b3b48f10c5d815` | Completed, qq_ready |
| Target uninstall, retain data | `bda30362bbc545e48c492718694b18e0` | Completed, retained |
| Target reinstall | `8c108a3dfd4d4fbf8f965715d653026f` | Completed, installed |

The initial source readiness observer expired before update dispatch; readiness
was checked again before proceeding. A later QQ status read briefly returned a
cached-credential setup error while the unused component initialized. The
read-only QA readiness check now waits for a definite offline response; it never
interprets that error as offline or clears the QQ state to make a test pass.

## Real failure found and fixed

The first source export crashed with `TypeError: sock.setTimeout is not a
function` in Node's HTTP client. `ssh2.forwardOut()` supplies a Duplex channel,
not a `net.Socket`; socket-shaped test substitutes had hidden the failure.

`backup-transfer.cjs` no longer calls `ClientRequest.setTimeout`. It maintains
its own 60-second activity watchdog and one-hour total deadline, refreshing on
headers and consumed/uploaded chunks. It preserves backpressure, cancellation,
partial-file cleanup and durable duplicate-import receipts. It does not add
fake socket methods or remove transfer timeouts. Old clients through 0.1.18
should not be used for backup export/import.

## Actual migration assertions

`verify-local-vm-migration.cjs` and the fixed `qa-migration-guest.py` fixture
perform the checks through the actual App control/transfer APIs and real
PostgreSQL in the guests. They do not replace database commands with mocks.

- Source: two synthetic group conversations, four Chinese messages, actual
  reply foreign keys, two 1024-dimensional synthetic vectors, one UTF-8 text
  file and one 256 KiB binary file. The target starts with a different fixture.
- The source encrypted archive is 17,285,537 bytes. Its password is randomly
  generated and kept only in the private ignored QA record, not published in
  logs, API requests or command arguments.
- Import leaves live target data unchanged. Re-importing the same archive
  returns the same imported ID with `already_imported: true`.
- Restore reproduces every selected source row, including IDs, JSON, reply
  keys and vector values; both file sizes and SHA-256 values match. The old
  target fixture is absent from the live database and file tree.
- Target instance identity, assigned ports and control credentials remain
  its own. Restored settings match the source and the target revision advances
  once. Private admin/database settings are compared by hashes, not revealed.
- After a normal shutdown and boot, the boot ID changes, data still matches,
  PostgreSQL is healthy, and NapCat/Bot are not running. The persisted QQ
  handover gate still reports pending.
- Explicit handover completes, clears the gate and returns a real QR login
  entry while the Bot stays disabled. No real account was scanned or logged in.
- Uninstall and reinstall both finish after the desktop SSH connection is
  deliberately dropped after dispatch. Removed service definitions have no
  process or boot link; reinstall preserves the PostgreSQL system identifier,
  schema, instance and revision. The old installer is not relaunched at boot.
- A separate post-maintenance pass checks all source rows/files again, target
  identity, database health, NapCat running, QR readiness and Bot still off.
  Migration/handover completed at `2026-09-15T07:18:02.725Z`; maintenance
  completed at `2026-09-15T07:21:00.058Z` and its data recheck passed.

## Desktop and package checks

- 25 Node encryption/transfer/control tests passed with no skips. New tests use
  a real localhost SSH server and a real channel without `setTimeout`, plus an
  idle response that must abort and remove only its incomplete output.
- The actual packaged Mac 0.1.19 App passed real startup, IPC isolation,
  password masking, responsive layout and encrypted backup export/import over
  localhost SSH from inside its packaged ASAR. Final desktop/narrow screenshots
  were visually inspected; long input text stays within its field.
- Both unpacked Mac and Windows packages match current transport, installer
  and checked runtime sources. Windows has the expected x64 PE format. All four
  release SHA-256 checks pass, both ZIP archives pass CRC verification, and the
  Mac DMG passes `hdiutil verify`.
- Exact deliverables are listed in `desktop/release/release.json` and
  `SHA256SUMS`. Both platforms remain unsigned development previews; Windows
  has not been executed on an actual Windows system.

## Remaining scope

This proves same-runtime cross-instance migration on the two local Ubuntu
guests, not cross-version backup compatibility, real QQ session transfer,
model replies, QQ file delivery, power-loss recovery, or a complete rollback
of the old target database. Other supported Linux distributions and actual
Windows execution still need acceptance. External cluster/bridge integrations
require user-owned endpoints and credentials. These limitations do not shrink
the V1 plan or turn this preview into a completed V1 release.
