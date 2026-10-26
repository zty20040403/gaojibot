# Windows App 0.1.26 Acceptance Status

Date: 2026-09-16. Status: incomplete; Windows installation and App execution are not verified.

## Artifact Identity

- Manifest SHA-256: `1b4f98893d949cdd483a71d72216fcbee7de429767b9f9a76bbbf837b911aef6`.
- Setup EXE: 127446579 bytes; SHA-256 `3c3013bc9f5a5910e107f5c4b2b68d2e3e520eb6a538cfd6910242fd3d562ae1`.
- Portable ZIP: 170112649 bytes; SHA-256 `b96ce85eb0e8cff8668a0ab000acd680311580f29f996014d3b44b37b6be5f63`.
- Runtime remains 0.20.0; SHA-256 `22d3d1bad545e5304b8b6b320cef261d3aa01232c6130a2b239a1c6756865d9a`.
- Draft release ID: `389978750`, tag `app-v0.1.26`, repository `zty20040403/gaoji-app`.

## Observed Results

- Initial 32 MiB uploads failed with connection resets. No complete large part was accepted.
  Switching both uploader and assembler to 8 MiB parts allowed verified resumption after another
  interrupted part, without reuploading completed parts.
- [Assembly run 35107954254](https://github.com/zty20040403/gaoji-app/actions/runs/35107954254)
  completed successfully. All four installers were reassembled and checked against the pinned
  manifest. Temporary part assets were removed; eight final assets remain in the unpublished draft.
- [Windows acceptance run 35108562027](https://github.com/zty20040403/gaoji-app/actions/runs/35108562027)
  used commit `29838de4798ba856ac391dc8e534827a5514951d`. It stopped at download with
  `Unexpected release`: the read-only token did not list the unpublished release.
  Installation, App startup, credential persistence, theme checks and uninstallation were skipped.
- GitHub's [release API documentation](https://docs.github.com/en/rest/releases/releases#list-releases)
  states that draft listings require push access. A successful API response with only public releases
  is not evidence that the draft is missing.
- Draft asset download URLs use an `untagged-*` path. The verifier now downloads draft assets by
  validated immutable API asset ID and retains full manifest/installer SHA-256 checks. Published
  asset URLs still require the exact expected tag and repository.

## Pending Approval

A proposed two-job workflow is prepared locally in `desktop/scripts/windows-release-smoke.yml`:

1. A separate temporary Ubuntu job obtains the draft with `contents: write`, verifies its hashes,
   and transfers the installers as a one-day CI artifact. It never executes an installer.
2. The Windows job has only `contents: read`, verifies the transferred files again against the
   original manifest hash, and runs the real installer and App tests.

Security review blocked pushing this permission change. User approval was requested and is pending.
The prepared distribution commit is `b5859ef`; remote main remains `29838de` at this checkpoint.
Do not push it or substitute another privileged path without approval. YAML syntax and separation
of the two permission scopes were checked locally with Ruby's YAML parser.

## Intended Checks, Not Yet Passed

- NSIS installation in a Unicode path containing spaces; portable ZIP startup.
- Actual packaged IPC, loopback SSH encryption and encrypted file transfer with synthetic data.
- Windows credential encryption and reload after restart; actual public update lookup.
- Read-only local prerequisite inspection, without installing Docker or enabling WSL2.
- Four palettes, light/dark/system switching, preference persistence, diagnostics and narrow layout.
- NSIS uninstallation removes the executable and retains synthetic App data.

No h310, h610 or tank connections, production QQ login, real model calls or Windows/WSL2 Bot
deployment occurred. Hosted Windows Server acceptance would not by itself prove Windows 11 physical
device support or full server deployment. Public 0.1.25 and the installed Mac App remain unchanged.
