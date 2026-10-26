# Deployment platform, phase one

The fuller software-manager V1, including independent website backups and
encrypted migration, is released in App 0.1.36. See
`software-manager-v1-acceptance.md` for its complete acceptance and limits.

## Scope

Generalize the existing desktop installer into a catalog, independent application
instances, and durable deployment tasks. Ship Gaoji and a static website adapter.
Keep existing Gaoji job IDs, storage paths, database contents and QQ sessions.
New generic deployment acceptance targets Ubuntu x86_64; existing NixOS and local
Mac/Windows entry points must remain available. No production server connections
or deployments are authorized by this implementation task.

## Completion checklist

- [x] Built-in catalog with pinned images, input schema, storage and health policy.
- [x] Multiple application instances per server; legacy instances remain usable.
- [x] Durable shared task contract, idempotent dispatch, disconnect/restart recovery.
- [x] Gaoji adapter retains setup, update/rollback and maintenance functionality.
- [x] Static folder upload validates paths, links, limits and content hashes.
- [x] Static deployment runs non-root, uses loopback only and verifies real HTTP.
- [x] Website update validates a candidate; failure preserves/restores old website.
- [x] Start, stop, resume, update, rollback and uninstall operate on owned resources.
- [x] Uninstall defaults to retaining data; purge needs a separate confirmation.
- [x] Application list, detail, installation impact, progress and SSH access in UI.
- [x] Local isolated Ubuntu coexistence and failure/recovery acceptance.
- [x] Desktop/mobile-size visual inspection, legacy focused regression tests.
- [x] Build and publish the finished App installer/update manifest, not source.
- [x] Remove only this task's temporary test resources and outdated build output.

## Boundaries

No fetched manifest may specify a root command. Adapter IDs select code shipped
with the App. Credentials stay in the main process. Uploaded website code is
served as static content, never executed on the host. Root-owned immutable task
inputs bind the app, instance, version, content checksum and operator identity.
Server tasks outlive the desktop; losing observation is not proof of failure.
Never delete shared Docker images, volumes, engine services or other instances.

Real QQ login/group delivery and execution on a real Windows host remain deferred
and must not be represented as tested by this phase's local acceptance.

## Evidence

Phase one acceptance, publication and local cleanup are complete. Deferred
account and Windows runtime checks remain explicitly outside this acceptance.

2026-09-18 local implementation:

- Added the two-entry built-in catalog. Resolved the official nginx-unprivileged
  stable-alpine multi-platform digest before pinning it; runtime does not follow a
  moving image tag. Gaoji continues to use its existing locked runtime images.
- Added per-server application records and explicit instance routing. Original
  server/deployment records are projected in place without moving data or jobs.
- Added native folder selection, bounded archive snapshots, safe server extraction
  and digest-verified HTTP health checks. Website containers are non-root, read-only
  and restricted to loopback publishing with a dedicated bridge. Docker internal
  networks do not publish the requested port; the verified implementation uses
  a separate bridge with outbound NAT and inter-container communication disabled.
  This is not a guarantee of complete egress filtering on every routed host.
- Added server-side website task state, candidate promotion, interrupted-commit
  recovery, start/stop, retained-data uninstall and separate purge confirmation.
- Added the catalog/application list and website management UI, reusing Gaoji's
  existing configuration, update and lifecycle panels for bot instances.
- Corrected installer exit status on persisted failure so systemd will not leave
  a failed installation looking active and refuse its retry.

Executed checks:

- `node --test tests/applications.test.cjs tests/connections.test.cjs tests/deploy.test.cjs`
  in desktop: 18 passed (local files/mocks, not remote deployment proof).
- `.venv/bin/python -m unittest tests.test_deployment_platform -q`: 8 passed
  (archive checks and transaction recovery, Docker methods mocked).
- `.venv/bin/python -m unittest tests.test_server_installer -q`: 12 passed;
  only local temporary directories and random loopback socket allocation.
- `npm run build`: TypeScript and Vite passed.
- `desktop/tests/verify-applications-ui.cjs`: actual Electron/preload with isolated
  fixture IPC passed. Checked old bot entry, site selection, impact confirmation,
  confirmation fields surviving polling, single dispatch and 1280/390px layouts.
  Desktop and narrow-screen screenshots inspected; no remote servers contacted.

No outstanding phase-one release work; deferred tests are listed in Boundaries.

Local acceptance environment (stopped and deleted after acceptance):

- `packaging/build/linux-qa-ubuntu`, Ubuntu 24.04 amd64 image 20260911,
  verified against the existing pinned SHA-256 in `packaging/qa/ubuntu.lock.json`.
- VM marker `gaoji-local-qa-6dd8978211e94925b546667c758cb8f0`; SSH forwarded only
  on `127.0.0.1:59581`, two emulated CPUs, 4 GiB RAM, no host directory mounts.
- Created through `desktop/scripts/local-vm.cjs prepare ubuntu`. Use its
  `connection()`/QMP identity checks, not production SSH config. The guest disk is
  a sparse 24 GiB overlay, not a production server. Delete this task's VM after
  acceptance and after QMP confirms it has stopped.
- Rebuild `packaging/build-runtime.mjs` before using the guest: the content
  validation/UI received further changes after the initial local runtime build.

2026-09-18 additional evidence:

- Fresh Ubuntu VM actually installed Docker; two independent websites now pass
  HTTP SHA-256 checks through the same SSH transport used by the App. Updates,
  rollback, stop and start passed with the same instance identity.
- Found and fixed missing-image `[]` detection and the internal-network port
  publishing error through real deployment, not mocked acceptance.
- Docker 28+ is required for the new website adapter's loopback isolation. The
  adapter never upgrades an existing shared engine automatically.
- A failed candidate preserved the current HTTP response. Stopping the candidate
  worker during verification also preserved the old site; resuming the original
  job succeeded without a new instance or replacement task.
- The original bot job completed after correcting the host-only catalog manifest
  classification. It retained the same job/instance IDs and reused the built
  dependencies. PostgreSQL is healthy with pgvector 0.8.6 and migration
  `0029_native_ssh_operations`; NapCat runs with a QR code available, but no QQ
  account or model is configured and the bot is disabled. Two websites coexist.
- 21 focused Node tests, 18 website/runtime-manifest Python tests and 12 original installer tests
  passed. The legacy setup readiness Electron fixture passed after navigating
  through the new application list. Application desktop/narrow screenshots were
  inspected. Other older UI fixture scripts have not all been migrated or run.
- Corrected an observation race: while the original failed task resumes, its
  previous failure receipt no longer hides the activating worker.
- Real guest reboot exposed Docker's automatic HostPort reassignment, leaving
  a healthy container behind a stale saved port. Candidate creation now chooses
  an available loopback port and explicitly pins it in Docker configuration.
  Docker still validates availability; a competing claim fails the candidate
  without replacing the original. The reboot test now checks exact configured
  bindings and before/after port identity, not only container uptime.
- Both 0.1.34 installers rebuilt and passed packaged-file integrity checks.
  The Mac packaged Electron application passed the catalog/site UI smoke test.
  Windows PE/package checks do not establish actual Windows runtime behavior.
- Removed the superseded unpublished draft only after matching all eight assets
  to the local build. Corrected installers were staged privately until acceptance.

Final local acceptance:

- Real reboot with explicitly bound website ports passed. Both website hashes,
  ports, container identities and revisions survived; PostgreSQL system identifier,
  schema and bot instance/revision remained unchanged.
- Uninstalled the primary site while retaining its files, verified the second
  website and bot database, then started the primary again and verified its HTTP
  response. Uninstalled it again and completed the separately confirmed purge.
  The primary's releases directory was absent; the other website and bot database
  remained healthy. No shared image or other application was removed.
- Final Mac packaged UI, Windows PE/package integrity, TypeScript builds,
  21 focused Node tests and 18 focused Python tests passed. The final runtime
  archive is `2cf639d411d5c663c0a02fcf7cf594daf1f69c714017e1120a23fd3cabdf5772`.

Publication and cleanup:

- Published `https://github.com/zty20040403/gaoji-app/releases/tag/app-v0.1.34`.
  All eight final assets matched the allowlisted sizes and SHA-256 digests; upload
  fragments were removed. No project source commit or production switch occurred.
- Checked through the real AppUpdater against the public release: 0.1.33 reports
  `available` for 0.1.34; 0.1.34 reports `up_to_date`. Public Mac and Windows
  download endpoints return HTTP 200 with matching Content-Length.
- Saved the non-secret receipts to `desktop/release/0.1.34/acceptance.json` before
  shutting down QEMU normally (exit 0). Removed the marker-verified 8.3 GiB VM,
  0.1.33 installers, unpacked test applications and generated UI screenshots.
  Approximately 9.6 GiB reclaimed; the four final installers and small manifests,
  notes and acceptance records remain. User configuration and source are retained.

## Desktop follow-up: 0.1.35

- Added read-only per-instance monitoring to the application list. Bot readiness
  requires healthy PostgreSQL, applied settings, running bot/NapCat services and
  a recent matching QQ heartbeat. Website readiness requires its existing HTTP
  content check. This does not certify model billing or answer quality.
- Reads are single-flight per instance and sequential within the visible list;
  list polling stops in instance details or hidden windows. Stale checks become
  unknown after one minute instead of keeping a green indicator indefinitely.
- Search and state filters retain their values during refresh. Local instance
  aliases can be renamed without changing host identity or remote paths. Legacy
  installations keep their original job IDs and credentials on reconnect.
- Removing a draft affects only a never-deployed local record. Main-process
  validation rejects existing deployment/update/lifecycle records and in-flight
  dispatches; server uninstallation is not hidden behind local deletion.
- Deployment panels show server-created timestamps and original job IDs. A
  pre-submit poll cannot overwrite the new receipt. Unknown timestamps remain
  explicitly unknown, and disconnected elapsed time uses the last server record.
- A regression test also covers a completed old website poll racing a newer
  submission. Only the matching current job may be marked settled or have its
  cached upload collected; an old response cannot replace the new job record.
- TypeScript build, 20 focused Node tests, Mac packaged Electron fixture and both
  package-integrity checks passed. Desktop and narrow layouts were inspected.
  No production SSH, paid model call, QQ account use or source push. This desktop
  iteration did not rerun the prior release's heavyweight VM acceptance; Windows
  actual runtime and real QQ receipt acceptance remain outstanding.
- Published App 0.1.35 after successful GitHub assembly run `35320693252` and
  exact eight-asset inventory/digest verification. Public AppUpdater checks on
  Mac and Windows 0.1.34 return `available:0.1.35`; Mac 0.1.35 is `up_to_date`.
  Mac DMG and Windows installer download endpoints return HTTP 200 with matching
  Content-Length. Removed the old 0.1.34 installers, unpacked test applications,
  generated screenshots and temporary transfer files. Current release artifacts
  occupy about 587 MiB; prior small acceptance records and user data are retained.
