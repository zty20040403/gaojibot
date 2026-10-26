# Desktop Cluster Configuration QA

Date: 2026-09-15. Desktop preview: 0.1.13. Runtime: 0.20.0.

This work was entirely local. No connection or deployment to h310, h610, tank,
the developer's cluster gateway, or the production bot was performed. The local
Ubuntu QA VM remained off. No real QQ account or model credential was used.

## Changes Covered

- Optional user-supplied Gaoji Cluster Control gateway, token, timeout and group scopes.
- Authenticated, read-only capability probe through a fixed container command.
- Separate gateway connectivity and upstream-query readiness results.
- Versioned private credentials, address binding and existing backup compatibility.
- Container-private temporary token file for the existing fleet client; no additional
  persistent token mount and no change to its write-authorization requirements.
- Feature gating for older managers and preservation of unsaved form input during polling.

## Results

54 Python checks passed across `test_desktop_cluster`, `test_desktop_components`,
`test_instance_manager`, `test_local_runtime`, and the new
`test_fleet_client.FleetControlClientTests.test_desktop_file_credential_works_without_bypassing_write_authorization`.

These used temporary directories, a local HTTP server, fake database/container
operations, and the existing fleet client's HTTP mock transport. The original
fleet test imports initialize NoneBot; the run explicitly cleared inherited
provider settings and used temporary local test state, with dotenv loading
disabled by the test. No production database was involved.

18 additional archive/maintenance/transfer checks passed across
`test_desktop_backups.ArchiveTests`, `test_desktop_backups.MaintenanceTests`, and
`test_desktop_backup_transfer`. This is not a real cross-server database migration.

10 Node management-protocol checks passed in `desktop/tests/control.test.cjs`.
The SSH forwarding interface was a loopback fixture, not a real SSH connection.

`desktop/tests/verify-setup-ui.cjs` passed against both the local Electron source
build and the final packaged macOS application, including its model-catalog
subchecks. All management responses were test-only IPC fixtures. Actual connection,
discovery and deployment handlers were disabled. Tested behavior includes:

- Unsupported cluster fields are not sent to old managers.
- New managers expose the configuration and probe controls.
- Tokens are masked, cleared after saving and never copied into public settings.
- Changing the gateway address cannot reuse a stored token.
- An authenticated gateway with an unavailable upstream is not shown as fully healthy.
- Multi-line group input preserves trailing newlines, focus and unsaved content across polling.
- Desktop and 390-pixel-wide layouts render without horizontal overflow.

The first UI run exposed a mutable implicit label on the group textareas; explicit
label IDs fixed it. The initial Python run was prevented from binding loopback by
the execution sandbox; reruns were explicitly permitted for local fixtures only.
Incorrect test setup assumptions were corrected before the passing runs; initial
failed attempts are not counted as successful acceptance.

Screenshot inspected: `desktop/release/qa/component-cluster-mobile.png`.

## Package Evidence

Both platforms contain runtime archive:

`64d8f45c409f182c6448ef04c42f9ae60a5314afc3816fa01b612daa99add638`

The package verifier compared the cluster/configuration/entrypoint sources to the
current files, checked required assets and excluded private files. macOS DMG
checksum validation passed; Windows executable structure is x64 PE. Archive hashes
and filenames are in `desktop/release/release.json` and `SHA256SUMS`.

Neither package is signed. Windows has not been executed on a Windows machine.
No real external gateway, QQ end-to-end exchange, server authorization flow or
Linux container execution of the new cluster probe was exercised in this round.
The local HTTP/client tests and package verification do not replace those checks.

## Reproduction

From `desktop/`, build using `npm run build`, then run
`node tests/verify-setup-ui.cjs` with `PLAYWRIGHT_MODULE` pointing to the installed
Playwright runtime. Set `GAOJI_PACKAGED_EXECUTABLE` to the local Mac app's executable
to test the packaged version. The script uses temporary client data and closes its
window on completion. Do not substitute a production server for the fixtures.

The full release requirements remain in `docs/server-app-v1-plan.md`; this is
component acceptance, not a claim that V1 is finished.
