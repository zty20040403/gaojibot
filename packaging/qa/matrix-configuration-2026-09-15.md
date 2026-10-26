# Desktop Matrix Configuration QA

Date: 2026-09-15. Desktop preview: 0.1.14. Runtime: 0.20.0.

All checks in this record are local. No h310, h610, tank, production database,
QQ account, Matrix account, or developer credential was used. The existing
local QA VM and Docker resources were not started or changed.

## Coverage

- Explicit QQ group / Matrix room mappings and opt-in enablement.
- Private, revisioned token storage, endpoint/account binding and archive inclusion.
- Fixed authenticated management operation and fixed container probe command.
- Read-only checks of identity, membership, encryption state and send permission.
- Rejection of encrypted rooms, unrecognized missing-state responses and redirects.
- Delivery-time route validation for both Matrix-bound and QQ-bound mirror queues.
- Encryption recheck before a Matrix send, without plaintext fallback on errors.
- Old-manager feature negotiation, unsaved input preservation, masked keys and
  stable per-mapping controls in the actual Electron renderer.

This is message mirroring, not standalone Matrix AI interaction. Attachments
retain the existing renderer's fallback behavior; E2EE and desktop iMessage
configuration are not implemented by this change.

## Verified Results

73 Python tests passed across `test_desktop_matrix`, `test_bridges`,
`test_onebot_delivery`, `test_instance_manager`, `test_desktop_components` and
`test_local_runtime`. Provider environment variables were cleared, the working
directory was `/private/tmp`, and database/state paths were temporary.

The Matrix probe tests use a real local HTTP server. They check request paths,
authorization placement, read-only methods, identity mismatch, absent membership,
encryption, muted rooms, unknown 404 responses, failed credentials and redirects.
An additional transport check injects credentials and private text into upstream
error bodies; send, sync and backfill errors retain only the HTTP status.
Bridge transports, Docker and live QQ API calls use mocks; these results do not
prove real provider behavior or a complete QQ/Matrix exchange.

10 Node protocol tests passed in `desktop/tests/control.test.cjs`. The forwarding
interface connects only to a loopback fixture. It is not an SSH deployment test.

`desktop/tests/verify-setup-ui.cjs` passed against the built local Electron source
and the first packaged 0.1.14 Mac build. That first build was subsequently replaced
to include the transport-error redaction fix; final package evidence is below.
Connection, discovery and installation handlers are disabled in this test. It
verified old-manager compatibility, token save/clear/reuse, account-change refusal,
mapping add/delete, polling preservation, and existing model-catalog behavior.
TypeScript/Vite builds passed. The 390-pixel-wide Matrix section screenshot was
visually inspected: no clipped fields, overlapping buttons or horizontal overflow.

Screenshot: `desktop/release/qa/component-matrix-mobile.png`.

## Release Verification

Both final platform packages contain runtime archive:

`fdec9208dc3a8e3ec86d4170d11ed681e172b8dbb87741cbe9e1d57cac6b53f3`

The verifier compared the Matrix configuration, manager, runtime entrypoint,
bridge and OneBot delivery modules to current source hashes. It checked required
desktop assets, excluded private files and validated Windows x64 PE structure.
The final Mac DMG and both platform ZIP integrity checks passed. Package names,
sizes and SHA-256 values are in `desktop/release/release.json` and `SHA256SUMS`.

The final packaged Mac executable passed `verify-setup-ui.cjs` again after the
redaction fix, including Matrix and model-catalog checks. It also passed
`verify-ui.cjs`: actual startup, IPC availability, renderer Node isolation,
password masking, responsive connection layout, and encrypted SSH communication
against a generated localhost SSH fixture. No real server was contacted.

All test windows, loopback fixtures and build processes exited normally. No
commit, push, Nix rebuild or production service operation was performed.
Neither package is signed, and Windows execution is still unverified.

## Limits

No real Matrix service, room permissions, room encryption transition, SSH install,
Linux container probe, QQ account exchange or Windows executable was exercised.
Signing and the remaining complete V1 requirements are still outstanding; see
`docs/server-app-v1-plan.md`. Testing the local HTTP protocol and renderer does
not replace user acceptance on a clean server.
