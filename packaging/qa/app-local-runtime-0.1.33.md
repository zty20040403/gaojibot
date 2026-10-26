# Gaoji App 0.1.33: local runtime

Date: 2026-09-17. Published:
https://github.com/zty20040403/gaoji-app/releases/tag/app-v0.1.33
Eight allowlisted assets were verified against local sizes and SHA-256 digests.
The release tag was normalized to `app-v0.1.33`; future publication now supplies
and validates the explicit tag rather than accepting GitHub's temporary tag.

## Verified

- Real Docker Desktop/PostgreSQL runtime upgrade and rollback in a fresh named
  volume. Original credentials, Chinese database text and files preserved.
- Pre-commit restore, post-commit recovery without a second migration, active
  task refusal, immutable resource ownership and same-operation resume checks.
- Changed configuration during preparation produces a terminal refusal receipt;
  it cannot get stuck endlessly retrying a stale revision.
- Real sandbox startup exposed Docker Desktop resolving the engine socket to
  `/run/host-services/docker.proxy.sock`. Validation now accepts that exact
  alias only with a matching engine/proxy mount request and writable bind type;
  other sources remain rejected. Focused regressions passed after the fix.
- 47 focused upgrade/runner regression checks passed. Two installation progress
  tests and 19 local JavaScript routing/identity/concurrency tests passed.
- Packaged Apple Silicon Mac: real Electron IPC, renderer isolation, secret
  masking, localhost SSH and encrypted backup transfer, responsive layout.
- Packaged update/readiness UI checks used controlled IPC fixtures. Their
  successful results are not real model or QQ service evidence.
- Mac/Windows package structure verified; both carry runtime checksum
  `4dfa95248fcd43c7956ea5849f252b762dfb4b90378d623f8b636c1787d076f7`.
- Local installation lists component stages, elapsed time, last update and
  classified errors, retaining the same installation on retry.

## Sandbox

Docker Desktop named-volume builder, authenticated runner, workload volume
accounting and scoped cleanup passed real isolated acceptance in 2,247.9 seconds.
Most time was first-time amd64 Nix dependency compilation on Apple Silicon.
The result is recorded in `desktop/release/qa/local-sandbox-linux.json`:

- Node execution and on-demand `hello` installation succeeded.
- Chinese PDF text and embedded font were verified; 22,599 bytes exported.
- Workspace/cache volume accounting returned 3,032,891,957 bytes.
- Sandbox deletion and runner shutdown succeeded. Temporary build containers,
  owned volumes and generated image tags were then removed.

The final socket validator was copied into the disposable QA controller before
its lazy import to reuse the ongoing tool-image build. The separate final
upgrade/rollback test rebuilt current controller sources and passed in 203.8s.
The generated installer payload includes the same final source revision.
No file was delivered through QQ; account acceptance is deferred, not passed.

## Limits

- User explicitly chose installer delivery first and deferred real QQ login,
  model replies and actual group file receipt. No production QQ account used.
- Windows installers are built/structurally verified, not a full Windows Docker
  Desktop acceptance run. No statement that current Windows execution passed.
- Docker Desktop tool image currently uses amd64 emulation on Apple Silicon;
  first Nix build is slower than native. Ordinary chat does not require it.
- Server uninstall/data retention and local Docker lifecycle are separate.
  No new local full-instance uninstaller claimed.
- No h310, h610 or tank connection, deployment, restart or deletion.

## Cleanup

Random test containers, namespaces, volumes and generated image tags are removed
by scoped ownership-checked teardown. Existing local instance and its data remain.
The stopped unpacked 0.1.33 Mac/Windows test Apps, QA screenshots and old 0.1.32
installers were removed. Latest installers and compact JSON/Markdown evidence
remain. Docker inspection confirmed no current QA container, sandbox image or
job data volume remained; the original local instance stayed up throughout.
The running installed App was not forcibly replaced. No current source pushed.
