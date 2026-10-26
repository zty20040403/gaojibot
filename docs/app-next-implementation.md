# App next implementation

Scope: local App only. Do not connect to h310, h610 or tank. Keep production
data, connection records and credentials unchanged. Release only distributable
App artifacts after focused verification; remove temporary test artifacts.

Published on 2026-09-17: [Gaoji App 0.1.33](https://github.com/zty20040403/gaoji-app/releases/tag/app-v0.1.33).
Real QQ/account acceptance remains deferred at the user's request.

## 1. Local runtime upgrades (implemented and released)

- Separate App, controller and Bot runtime versions.
- Prepare pinned runtime images before pausing the current instance.
- Run an instance-scoped durable Docker worker, independently of Electron.
- Reuse backup / candidate database / commit / recovery transactions.
- Preserve the named data volume, account, ports and credentials.
- Recover pre-commit failures to original data; never discard post-commit writes.
- Resume the same operation ID after interruptions; expose progress and rollback.
- Verify ownership and immutable image IDs before container operations.

Acceptance: successful upgrade, pre-commit failure, post-commit interruption,
duplicate submission, missing volume and wrong-resource refusal, UI recovery.

Verified locally: real Docker Desktop / PostgreSQL upgrade and rollback, isolated
credentials and Chinese files preserved; focused Python checks; Electron
desktop/narrow layout and Node identity/concurrency checks. No real QQ login or
Windows Docker acceptance claimed. QA containers, volumes and image tags removed.

## 2. Local sandbox (implemented and locally verified)

Adapt the isolated runner for Docker Desktop named volumes. Install large
optional tools on demand; retain Chinese fonts and basic document/code tools.
Expose capacity and scoped cleanup. Never mount the engine socket into the Bot
or user sandbox.

Acceptance: create, execute, generate a Chinese PDF, export a file, inspect
usage, destroy only the temporary task sandbox and verify cleanup. Actual QQ
file receipt remains part of the explicitly deferred account acceptance.

Real Docker Desktop acceptance passed: authenticated runner, Node execution,
on-demand Nix tool, Chinese text extraction and embedded PDF font, 22,599-byte
file export, 3,032,891,957-byte workspace/cache accounting and scoped destruction.
The real run exposed Desktop's normalized engine socket path; the shared
validator now accepts that exact proxy and still rejects unrelated mounts.

## 3. Deployment progress and recovery (implemented)

Show the current component, real stage/progress where available, elapsed time,
recoverable failure reason, and original-task reconnection. Do not invent a
download percentage or create a second database on retry.

Local installation now exposes component stages, completed steps, timestamps,
elapsed time and sanitized output categories. Retrying keeps the original job.
Electron desktop/narrow layout and interrupted-installation UI checks passed.

## 4. Beginner acceptance (packaged checks passed; account test deferred)

Verify install, QQ login, saved model, message connection, file delivery,
upgrade, and uninstall retaining data as one journey. Distinguish synthetic
checks from actual account / Windows verification; do not claim unrun checks.

User explicitly deferred real QQ/account acceptance on 2026-09-17 and requested
the installer first. Do not use a production account or connect to their servers.
Windows Docker Desktop and full platform acceptance remain documented limitations.

0.1.33 packaged Mac passed real Electron/preload, renderer isolation, password
masking, localhost SSH, encrypted backup transfer and update/readiness UI checks.
Mac and Windows artifact contents, runtime digests and package structures match.
The quickstart now distinguishes updating the App from updating the Bot and
lists manual QQ/file acceptance steps. Existing server uninstall/retention tests
are not claimed as a Docker Desktop uninstaller or Windows execution evidence.
