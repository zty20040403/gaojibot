# App 0.1.27 Local Backup Acceptance

Date: 2026-09-16. Local preview, not published to the update feed.

## Changes

- Docker Desktop uses the existing persistent backup/restore operations through a
  named-volume adapter. Temporary restore validation has its own internal network.
- Derived database and bot credential mounts are refreshed before restarting the
  original configuration after a failed restore.
- Controller startup resumes maintenance before starting writers and preserves
  the explicit QQ handover gate after a cross-instance restore.
- Local backup export/import uses authenticated loopback transport and the same
  encrypted archive format as SSH deployments. The target instance identity comes
  from the authenticated controller and must agree with any stored identity.
- Four palettes, light/dark/system modes, preferences, workspace search/pins and
  diagnostic export remain available.

## Evidence

- 52 focused Python tests completed, one existing optional test skipped. These
  cover local startup, backup/restore, interrupted recovery, QQ handover and shared
  server update behavior. They do not constitute the full project test suite.
- 14 Node backup-transfer checks passed, including a real loopback SSH channel,
  wrong passwords, disconnects, cancellation and conflicting/missing identity.
- A fresh, randomly labeled Docker Desktop volume passed actual PostgreSQL
  dump/restore. Chinese records, pgvector values and file hashes matched the
  snapshot after deliberately changing the live data. Bot and QQ stayed stopped.
- Docker evidence is in `desktop/release/qa/local-backups-linux.json`. It uses the
  existing runtime/controller base images recorded there, with this checkout's
  controller source mounted read-only. It is not a fresh image build acceptance.
- The first two Docker attempts failed in fixture setup because the test assumed
  pgvector was installed in public. The corrected fixture resolves the extension's
  actual namespace. The final run passed; temporary containers, networks and data
  volumes were cleaned after each attempt. Existing user resources were unchanged.
- The packaged Mac 0.1.27 App passed actual main/preload IPC encrypted export/import
  against synthetic loopback data. Only native file selection was replaced in the
  fixture; transfer handlers and crypto were real. Import did not trigger restore.
- The packaged Mac App passed all four palettes, light/dark/system changes and
  persistence, 1280px/390px layout, search/pins, redacted diagnostics and masked
  passwords. Screenshots: `desktop/release/qa/workspace-0.1.27/`.

## Boundaries

- No h310, h610, tank, real QQ account or user model API was accessed.
- New local backup support requires the new controller/runtime. Replacing the App
  does not upgrade an existing local instance. Do not delete its volume to upgrade.
- Imports require an identical runtime checksum, not merely the same version label.
- Windows execution remains unverified. The earlier 0.1.26 CI stopped before
  installation; the draft-access permission change is awaiting user approval.
- Local advanced sandbox and runtime upgrades remain unavailable. Preview packages
  are unsigned. Full V1 and real message/file delivery acceptance are not complete.

Installer checksums are generated separately in `desktop/release/0.1.27/release.json`
and `SHA256SUMS`; these are integrity checks, not proof of Windows execution.
