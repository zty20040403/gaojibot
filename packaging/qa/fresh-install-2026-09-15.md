# 0.1.17 Fresh Ubuntu Installation

## Scope

Local disposable Ubuntu 24.04.5 x86_64 guest only, on macOS ARM64 QEMU TCG.
No production server, developer QQ session, API key or host directory mount.
The one-time h310 cleanup is separately recorded and is already complete;
it does not authorize remote deployment or testing.

The `restore-target` slot uses an independent disk, generated SSH keys,
loopback port, pinned host key and guest marker. It reuses only the verified
official base image, not the installed primary guest's disk or data.

## Candidate and Baseline

- Desktop: 0.1.17 development preview, unchanged installer artifacts.
- Runtime SHA-256: `ba31b0f903ac3546ae016882028e54edb728233a1b9add22ccbb183121320458`.
- Ubuntu image build: `20260911`.
- Image SHA-256: `612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354`.
- Resources: two emulated CPUs, 4 GiB RAM, 24 GiB sparse disk.
- Baseline: no Docker, PostgreSQL or Nix; cloud-init complete, no failed units.
- Cloud-init includes Python; this does not test a Python-free host.
- Initial boot ID: `4bf07ca0-b2b7-40da-9e66-fb1b8b476877`.
- Initial free bytes: `22107688960`.
- Installer job: `b0756932e1684241a0c8aa05aab8dd73`.
- Instance: `09990cc894e04963810403d9a72c25c6`.

## Observed Results

The actual desktop deployment module dispatched this job exactly once after
persisting its identity. Docker was installed automatically from its configured
official repository. The pinned runtime build installed media/PDF/font packages
and compiled whisper.cpp; no manual guest repair or dependency substitution was
used. Installation completed at `2026-09-15T05:43:18Z`, reaching `awaiting_setup`
with all six stages complete. The first installation took 56 minutes 7 seconds
under x86 TCG emulation; this is not a physical-server performance measurement.

The offline runtime check passed before database creation, including source
permissions, dependencies, Chinese fonts and browser rendering. Docker reports
the built runtime image at 2,061,463,364 bytes (about 1.92 GiB); that excludes
the database/QQ images, build cache, volumes and guest operating system.

Read-only runtime acceptance passed at `2026-09-15T05:44:03.962Z`:

- Database is running and healthy; schema is `0028_task_evidence`.
- pgvector version is `0.8.6`.
- NapCat is running and returns a real QR URL, without logging its value.
- QQ is not logged in, there are zero models, and Bot remains disabled.
- Original job, instance and runtime checksum match the prepared records.

The private raw result is `acceptance.json` in this local QA slot.

The packaged Mac executable passed the real SSH/IPC reconnect check during
the runtime stage. Closing and reopening the app retained the original job;
it did not dispatch another installation. Only the native file chooser was
substituted to select the generated test key. Desktop and narrow screenshots
were inspected, with no observed text overflow. The temporary app profile and
window were removed after the check.

The first 30-minute read-only observer reached its observation deadline while
the installer remained active. This is not recorded as an installation failure
or as success; a new observer follows the same job without retrying installation.

Screenshots are local ignored artifacts in `desktop/release/qa/`:

- `local-vm-restore-target-runtime-desktop.png`
- `local-vm-restore-target-runtime-narrow.png`

## Normal Reboot and Completed App Connection

Normal guest reboot passed at `2026-09-15T05:47:54.561Z`:

- Boot ID changed to `a993902d-dcc1-49db-9990-f80224817ade`.
- PostgreSQL system identifier remained `7685632946521976870`.
- Schema, instance, runtime checksum and configuration revision `1` were unchanged.
- Installation completion timestamp remained `1789450998`.
- Database was healthy and NapCat/manager services recovered automatically.
- This fresh generation retained its exact original installer boot link; the
  completed oneshot had no running process. It did not create another database.

The checksum-scoped `reboot-acceptance-*.json` in the private QA directory
contains both snapshots. This is a normal reboot, not a power-loss test.

After reboot, the packaged Mac executable passed the actual SSH/IPC connection
and read-only capacity page check. The original job remained `awaiting_setup`,
no new installation was dispatched, and there were no browser errors. Capacity
was ready at 199,323,648 instance bytes, with the original 100 GiB budget and
2 GiB free-space reserve. The budget is admission control, not a hard quota.

Four additional screenshots were viewed, with no observed text overflow:

- `local-vm-restore-target-verify-desktop.png`
- `local-vm-restore-target-verify-narrow.png`
- `local-vm-restore-target-verify-capacity-desktop.png`
- `local-vm-restore-target-verify-capacity-narrow.png`

The App window and temporary profile were closed/deleted by the check. No
model, QQ account or saved setting was added. QMP requested graceful guest
shutdown; the original QEMU process then reported `vmExited: true`, exit code
`0`, with no signal. Both installation observers and all checks terminated.
The guest disk is retained privately for later acceptance, not left running.

The three local VM profile tests passed: existing primary paths are unchanged,
distribution/slot records cannot collide, and remote names or path traversal
are rejected before connecting. Both modified acceptance scripts passed syntax
checks. These QA helpers are not part of the shipped application.

## Limits

This check does not establish Windows execution, real QQ/model/file delivery,
cross-server backup recovery, Debian/NixOS acceptance or signed release status.
The Debian image's official redirects still failed TLS validation at an expired
certificate during this session; validation was not bypassed. This is a source
download issue, not a successful Debian installation or an App failure result.
