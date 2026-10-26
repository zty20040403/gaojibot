# Local Clean Linux QA

This is an opt-in developer check, not a deployment script for a real server.
The desktop app remains the installation entry point for users.

Current evidence: [Ubuntu local acceptance, 2026-09-15](ubuntu-2026-09-15.md).
The independent 0.1.17 clean guest is recorded separately in
[fresh installation acceptance](fresh-install-2026-09-15.md).

## Isolation

- The helper accepts only the fixed `ubuntu` or `debian` image recipes here.
- A separate `restore-target` slot can start from the official base image with
  its own disk, keys, port, marker and deployment record; it does not clone an
  installed guest. Existing `primary` records retain their original location.
- It verifies the published image digest before preparation and every boot.
- SSH connects only to `127.0.0.1`, with generated test keys, a pinned host key,
  and a unique guest marker. It does not use the user's SSH configuration.
- QEMU gets two emulated x86_64 CPUs, 4 GiB RAM and a 24 GiB sparse overlay.
  There are no host directory mounts. Public dependency downloads use guest NAT.
- Only the app runtime bundle is uploaded. No developer API keys, QQ sessions,
  production databases or server login credentials are copied.
- The helper does not use or clean the host Docker engine.

Requires macOS `hdiutil`, Node dependencies in `desktop/`, and QEMU binaries
`~/.nix-profile/bin/qemu-system-x86_64` and `qemu-img`. Cross-architecture TCG
emulation can make installation significantly slower than a physical x86 host.

## Run

From `desktop/`, after generating the deployment bundle:

```sh
node scripts/local-vm.cjs prepare ubuntu
node scripts/local-vm.cjs run ubuntu
```

Keep `run` in its own terminal. In another terminal, in the same directory:

```sh
node scripts/local-vm.cjs inspect ubuntu
node scripts/local-vm.cjs install ubuntu
node scripts/local-vm.cjs diagnostics ubuntu
```

For a separate clean guest, pass the fixed slot name to every helper command:

```sh
node scripts/local-vm.cjs prepare ubuntu restore-target
node scripts/local-vm.cjs run ubuntu restore-target
node scripts/local-vm.cjs inspect ubuntu restore-target
node scripts/local-vm.cjs install ubuntu restore-target
```

Acceptance scripts select this same guest with `GAOJI_QA_SLOT=restore-target`;
`GAOJI_QA_DISTRIBUTION=debian` selects the other locked distribution. Without
these variables they continue to use the original primary Ubuntu guest. No
arbitrary server name or filesystem path is accepted for either selector.

If offline runtime acceptance fails, `runtime-diagnostics ubuntu` repeats just
that check in a non-root, read-only, network-disabled container inside the VM.
It mounts no instance data and exposes the otherwise hidden Python traceback.

`install` calls the actual app deployment module. The job identity is saved
before dispatch. It refuses to create another job if one already exists.
To observe or resume that same job after a confirmed failure:

```sh
node scripts/local-vm.cjs installation-status ubuntu
node scripts/local-vm.cjs retry ubuntu
```

An SSH timeout is not proof of failure. Inspect the same job before retrying.
Successful dispatch is not installation success. Require `awaiting_setup`,
component health checks, and then separate user-owned QQ/model acceptance.

After `awaiting_setup`, the read-only check below verifies PostgreSQL schema and
pgvector, running database/QQ components, a real QR response (without printing
its value), an unconfigured Bot remaining stopped, and the original instance.

```sh
node tests/verify-local-vm-runtime.cjs
```

The opt-in `verify-local-vm-lifecycle.cjs` and `verify-local-vm-update.cjs` perform
real maintenance inside this same disposable VM. They require an unconfigured,
not-logged-in instance, persist task IDs, and disconnect after dispatch before
following the original worker. Neither accepts a server address. Lifecycle
checks preserve the original PostgreSQL system identifier; they do not prove
preservation of real user message contents. Re-running a completed acceptance
record does not execute another maintenance cycle. These files are not shipped.

For a fresh candidate bundle, use checksum-scoped records instead of replacing
earlier evidence:

```sh
node tests/verify-local-vm-update.cjs --current-bundle
node tests/verify-local-vm-lifecycle.cjs --current-bundle
node tests/verify-local-vm-reboot.cjs --reboot
```

The last command reboots only the marker-verified localhost guest. It records
the request before sending it; reconnecting does not send another reboot.
It checks the changed boot ID, unchanged database identity/schema/configuration,
current version and runtime services. For an updated instance it checks the
retired original installer boot link; a fresh instance must retain its exact
original link and restore the completed state without running installation again.
It requires an unconfigured Bot and no QQ login. It is not a production test.
Only after an update is confirmed terminal, failed and non-resumable may the
update check accept `--new-attempt`; the old request/result stays in its history.

### Capacity Admission

After updating this same unconfigured guest to the current bundled runtime:

```sh
node tests/verify-local-vm-capacity.cjs --exercise
```

This checks real Docker volume accounting and the Bot service's non-root,
read-only capacity mount. It creates one randomly named, instance-labelled QA
volume containing 64 KiB; it does not build another sandbox image. A temporary
1024 GiB free-space reserve forces a denial without filling the guest disk.
The same runtime entrypoint configuration is loaded in short-lived containers;
no Bot event loop, QQ login, model request or message send is started. Read/send/
cleanup *admission* is checked, not actual delivery.

The original capacity policy, exact QA object names and database identity are
saved before mutation in a private checksum-scoped record. The test restores
the policy and removes only its labelled objects, without force or prune. It
verifies that writes are admitted again and the database identity/schema did
not change. An unexpected policy change or ownership mismatch stops cleanup.
After an interruption, use `--recover` on the same guest and bundle before a
new exercise. A failed or cleanup-only attempt is not a passing acceptance.

Stop the test guest and wait for the `run` terminal to report `vmExited`:

```sh
node scripts/local-vm.cjs system_powerdown ubuntu
```

## Real Cross-Guest Migration

`tests/verify-local-vm-migration.cjs` is opt-in and operates only on the fixed
Ubuntu `primary` and `restore-target` guests. It refuses configured models or a
logged-in QQ account. It never accepts a remote host. Both guests must already
be installed and updated to the exact current runtime checksum.

The source fixture contains two synthetic conversations, four Chinese messages,
reply foreign keys, two 1024-dimensional vectors, a UTF-8 text file and a binary
file. The target receives a different fixture. The encrypted backup goes through
the production desktop transfer and controller APIs, not filesystem copying.

1. Boot/update the primary guest, then run `node tests/verify-local-vm-migration.cjs source`.
2. Shut down the primary and wait for its QEMU process to exit. Do not run both guests together.
3. Boot/update `restore-target`, then run `node tests/verify-local-vm-migration.cjs target`.
4. After success, shut down and boot the same target; run `node tests/verify-local-vm-migration.cjs verify-reboot`.
5. Run `node tests/verify-local-vm-migration.cjs handover` to explicitly start the unused QA QQ login entry. The Bot stays disabled; nobody scans the QR.
6. To also verify maintenance, run `GAOJI_QA_SLOT=restore-target node tests/verify-local-vm-lifecycle.cjs --current-bundle`, then `node tests/verify-local-vm-migration.cjs verify-maintenance`. The first uninstalls while retaining data, disconnects the desktop after dispatch, then reinstalls the same instance; the second requires both durable receipts and rechecks the original data witness.
7. Shut down the target and wait for QEMU to exit.

Import must not change live target data. Restore must preserve the source
witness exactly, replace the target fixture, retain target instance/ports/control
identity, and copy the expected private configuration (only hashes are compared).
After reboot, data must still match and QQ must remain pending until handover.

Task IDs and the generated backup password are stored in a private,
checksum-scoped QA record before dispatch. Never publish that record or archive.
An observer timeout is not task failure: inspect the same operation, and rerun
the same stage only after checking its state. Completed restore must never be
replayed after handover. These scripts are excluded from release packages and
are not an installation shortcut for real servers.

## Real Instance Removal

After migration, handover and maintenance acceptance, the unused `restore-target`
guest can be consumed by `node tests/verify-local-vm-purge.cjs`. This is destructive
to that QA instance: it must have no model keys or QQ login, and the source guest
must be off with its encrypted backup retained on the host. Never run this as an
acceptance shortcut on a user server. It accepts no target host or instance ID.

The script creates a labeled task workspace and a separate QA container, volume
and marker file. It uninstalls through the App, previews the exact deletion,
confirms that instance and drops the client connection after dispatch. It then
checks deleted resources, revoked access, retired service definitions, unchanged
shared images and unchanged unrelated container ID/start time/PID/file hashes.
Only after those assertions does it clean up its own unrelated QA fixture.

Task IDs and receipts are recorded before dispatch in the private
`purge-acceptance-<runtime-checksum>.json` file. An observation timeout does not
mean the worker stopped; inspect the same task and live worker before resuming.
Do not delete the record or recreate the instance to make a failing check pass.

If a defect was fixed after a terminal, non-resumable rejection, the optional
`recover-local-vm-purge.cjs` re-enables only that retained QA target through the
App. After a verified App update, pass `--source-checksum <original-checksum>`
to the purge verifier to retain the original migration witness and failed
attempt while generating a new uninstall/preview. It requires the completed
recovery/update records and the actual current runtime; it does not reuse the
old deletion approval. Keep that same source-checksum argument for the reboot
verification.

After completion, shut down the target and wait for QEMU to exit, boot it again,
wait for SSH and Docker (the App manager was intentionally removed), and run
`node tests/verify-local-vm-purge.cjs --after-reboot`. This requires a changed boot
ID and verifies that data, services and resources were not recreated. Shut the
guest down again and confirm process exit. The target is no longer available for
the earlier migration/maintenance checks; its removal record is retained as QA
evidence, not treated as a damaged installation to repair automatically.

The purge must preserve exact unrelated network IDs. Across the later Docker
restart, only the built-in bridge ID may change: this isolated fixture must have
exactly the three default networks with expected names/drivers/scope, unchanged
host/none IDs, and no custom network. Image, container and volume inventories
still use exact comparisons, accounting only for the explicitly cleaned fixture.

`query-status` inspects only this VM's local QMP socket. `quit` is an abrupt
fallback for this VM only, not the normal shutdown path. Do not delete the
overlay while QEMU is running. Test keys and disk state are private, ignored
build artifacts under `packaging/build/linux-qa-ubuntu/`; they are not shipped.

## Image Sources

- [Ubuntu 24.04 fixed build](https://cloud-images.ubuntu.com/noble/20260911/)
  and [SHA-256 manifest](https://cloud-images.ubuntu.com/noble/20260911/SHA256SUMS).
- [Debian 13 fixed build](https://cloud.debian.org/images/cloud/trixie/20260914-2601/)
  and [SHA-512 manifest](https://cloud.debian.org/images/cloud/trixie/20260914-2601/SHA512SUMS).
- Seed format: [cloud-init NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html).
- VM control: [QEMU QMP](https://www.qemu.org/docs/master/interop/qmp-spec.html).

The Debian distribution endpoint redirected to a server with an expired TLS
certificate during the first check. Certificate validation was not disabled;
the Ubuntu image was used instead. This does not count as Debian acceptance.

## Packaged Desktop Connection

While the guest and its original installation exist, from `desktop/`:

```sh
PLAYWRIGHT_MODULE=/absolute/path/to/playwright \
GAOJI_PACKAGED_EXECUTABLE=/absolute/path/to/Gaoji.app/Contents/MacOS/Gaoji \
node tests/verify-local-vm-ui.cjs
```

This uses a temporary app data directory. Only the native file chooser is
substituted to select the generated QA key. SSH, fingerprint verification,
server inspection, status transport, IPC and the renderer are real. The test
attaches the existing installer job to its temporary connection record while
the app is closed; it never clicks Install or creates another server instance.
After reopening, it logs in again without stored credentials and verifies the
same job. It captures desktop/narrow screenshots, closes the app and removes
only its temporary profile. It does not assert that an unfinished installation
has succeeded, or that Windows and QQ delivery have been verified.

Add `--capacity` after updating to a capacity-capable bundle and restoring the
normal capacity policy. This additionally checks the real storage API through
the packaged renderer IPC and SSH tunnel, the completed instance's ready state
and both filesystem measurements. Desktop/narrow screenshots are saved as
`local-vm-capacity-*.png`; values and status badges must fit their containers.
It does not change settings, clean data or dispatch an installation.
