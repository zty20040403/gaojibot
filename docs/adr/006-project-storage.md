# ADR 006: Project quota volumes and retained versions

Date: 2026-10-27. Owner: Kenneth.
Status: **M0 validation in progress, not a production capability announcement.**
Plan: `docs/project-workspace-v1-plan.md`. This ADR supplements that unchanged
plan. It does not enable projects, alter current sandbox retention or deploy a
helper to any server.

## Problem

Persistent projects cannot use an unbounded workspace and rely on a periodic
capacity check alone. A running writer can consume the instance's remaining
space before a sampler rejects the next command. Versions and browser profiles
also need accounting; deleting a file must eventually release physical storage.

Hard quota means the filesystem itself rejects writes at the project's limit.
It does not mean a project gets all of the disk image's advertised bytes as usable
file space, nor that total instance capacity is reserved in advance.

## Current Evidence

All experiments below used local throwaway resources, not h610, h310 or tank.
No product code was changed in M0. The runtime image was already installed; no
image was pulled, built or committed. Every created container, named volume,
loop mapping and temporary QCOW2 image was cleaned up.

### Docker Desktop on Apple Silicon

Environment: Docker Desktop 29.6.1, aarch64, LinuxKit 6.12.76, overlayfs.
Image: existing local Python runtime, selected by immutable image ID. The
helper had root/mount privileges but no network or pre-existing host-data
mounts. Each writer ran separately as UID 65534, no capabilities, read-only root,
no network, one CPU, 128 MiB RAM and 32 PIDs.

Reproducer: `tools/verify_project_storage.py`. It requires an explicit local Unix
Docker socket and existing Python image with `losetup`, `mkfs.ext4`, `mount`,
`umount`, `fstrim` and `sync`. It never scans for or deletes unrelated resources.
Cleanup checks a random ownership label and exact loop backing-file identity.

```sh
.build-venv/bin/python tools/verify_project_storage.py \
  --docker /path/to/docker \
  --socket /path/to/local/docker.sock \
  --image EXISTING_LOCAL_IMAGE \
  --qemu-img /path/to/qemu-img \
  --qemu-io /path/to/qemu-io \
  --output .cache/roadmap/m0-storage.json
```

The two QEMU options are optional and must be supplied together.

| Experiment | Actual result | Meaning |
| --- | --- | --- |
| A: helper creates sparse ext4 images and loop devices; Docker local driver mounts those devices | Pass, two separate 96 MiB images; `nosuid,nodev` | A helper-owned loop device is viable on this particular Desktop backend |
| Write project A until full | `ENOSPC` / errno 28 after 86,536,192 bytes | Filesystem hard limit works; ext4 metadata consumes part of the image |
| Write project B after A is full | Additional 16,777,216 bytes succeeded; pre-existing sentinel hash unchanged | A's exhaustion did not corrupt or block B |
| Delete A's fill file, then `fstrim` | Image allocated bytes 91,078,656 -> 4,571,136 | Loop/ext4 releases physical file allocation on this backend |
| B: local driver directly receives an image filename with `o=loop` | Mount rejected, `invalid argument` | This driver does not replace the loop-allocation helper here |
| Bind helper mount through its `/proc/<pid>/root` path | Rejected by OCI mount setup | Not an implementation strategy; do not depend on proc-path exposure |

The workable A variant **still needs a privileged, narrowly scoped loop manager**.
It is not the same as B and is not proof that an ordinary App container can mount
images itself. The helper unmounts its initialization mount before Docker mounts
the device; the same filesystem is never mounted through competing devices.

Small sequential-write experiment, three fsynced 32 MiB writes per path:

- Ordinary named volume median: 973.6 MiB/s.
- Quota-image volume median: 608.2 MiB/s, ratio 0.625.
- These are noisy short local samples, not production disk benchmarks. They do
  not establish a general 37.5% overhead or predict random-write/server results.

Evidence: ignored `.cache/roadmap/m0-storage-desktop-device.json`. The first
probe inherited the App image's non-root user and was corrected to select root
only for the ephemeral helper; a separate failed proc-bind attempt is recorded
in `.cache/roadmap/m0-storage-desktop-second.json`. Failed attempts also cleaned
up their own resources. None of these prove Windows Docker Desktop support.

### QCOW2 Allocation and a Real Local Guest

Host QEMU 10.2.2, temporary 128 MiB QCOW2:

- Empty allocated bytes: 200,704.
- After writing 64 MiB through `qemu-io`: 67,436,544.
- After `qemu-io -d unmap` discards the same range: 327,680.
- `qemu-img check`: zero errors. Temporary image removed.

That first check validates only QCOW2's discard implementation. A second probe,
`tools/verify_project_guest_discard.py`, then booted an actual offline Linux
guest with QEMU TCG, one vCPU and 768 MiB RAM. It used Docker Desktop's installed
ARM64 kernel and a private read-only APFS clone of its vendor EROFS root image;
there was no image download or host-directory/credential/network passthrough.
The clone avoids opening the root image already locked by the Docker VM.

The only formatted disk was a new 128 MiB QCOW2 overlay over a newly created,
read-only QCOW2 base. Its virtual capacity and unique virtio serial were checked
inside the guest before formatting. Fixed PCI slots avoid MMIO enumeration
reversing the root/test disk order. Commands wait for the actual shell prompt.

Measured with a 48 MiB random file and a separate retained sentinel:

| Stage | Physical bytes allocated to the overlay |
| --- | ---: |
| File written and fsynced | 51,838,976 |
| File deleted and synced, without trim | 51,838,976 |
| Guest `fstrim` through virtio `discard=unmap` | 1,638,400 |

The sentinel survived trim and unmount/remount. The base's SHA-256 stayed
unchanged; `qemu-img check` reported zero errors. QEMU stopped and the root clone,
base and overlay were deleted. Evidence:
`.cache/roadmap/m0-guest-discard-backing.json`. An earlier standalone-QCOW2 guest
run also passed, reducing 51,777,536 to 1,576,960 bytes.

```sh
.build-venv/bin/python tools/verify_project_guest_discard.py \
  --qemu /path/to/qemu-system-aarch64 \
  --qemu-img /path/to/qemu-img \
  --kernel /path/to/ARM64/kernel \
  --root-image /path/to/read-only/linux-root.img \
  --output .cache/roadmap/m0-guest-discard.json
```

The supplied root must contain `/bin/sh`, ext4 tools, `fstrim` and basic utilities.
This is real guest/filesystem/virtio/backing-chain evidence, **not** acceptance of
the project's production KVM/libvirt XML, crash recovery, native Ubuntu/NixOS or
VM restart behavior. Only an unmount/remount, not a guest restart, was tested.

### Synthetic Scheduled Turn

`tests/test_project_m0.py` exercises the existing `RoutineDispatcher`, restored
OneBot event, ordinary tool executor, real Agent loop, tool result, turn journal,
PostgreSQL stores and durable Outbox. Only the provider and QQ network boundaries
are simulated. It does not insert a fabricated inbound message to start a turn.

Assertions cover an existing canonical message remaining unchanged, a negative
synthetic ID with no canonical row, a turn with no inbound-message reference,
the original group identity, an actual prepared expression plus final text in
Outbox, and takeover returning the same delivery without more model requests.
Actual QQ receipt and live model quality are deliberately outside this check.

## Provisional Decision

1. Keep project features off by default. A backend may advertise hard quota only
   after its mount/ENOSPC/isolation checks pass on that deployment's platform.
   An unavailable helper is not silently replaced with an unbounded volume.
2. Prefer candidate A's fixed-size sparse ext4 plus a constrained loop manager
   for OCI hosts. The Desktop experiment demonstrates a possible Docker-device
   mount variant, not a universal implementation. Candidate B is rejected on the
   measured Desktop environment; native Linux results are still pending.
3. Keep Desktop soft quota as the documented fallback when hard quota is not
   verified. Label it explicitly; stop new writes and active writers on excess,
   but do not promise the sampling interval prevents every overshoot. Preserve
   outer instance admission and reserve-space protections in either mode.
4. VM hard capacity and physical reclamation are distinct. The guest experiment
   proves explicit trim is needed here; deleting files alone did not shrink the
   host allocation. Wire and verify the production backend before enabling it.
5. Versioned retained data uses scoped manifests and content-addressed blobs.
   Rebuildable dependencies are excluded only when reconstruction is possible.
   Neither soft quota nor space pressure authorizes deleting retained versions.

## Implementation Boundaries for M2

- Helper requests use project identifiers, not arbitrary host paths or device
  names supplied by the model. Derive images under an owner-controlled root;
  reject symlinks/path traversal, foreign ownership and changed backing files.
- Record the image identity, mount identity and generation durably. After helper
  restart, verify the exact backing file; never assume `/dev/loopN` is still the
  same project. Reconcile allocations and mounts without detaching other owners.
- No shrink, fsck, snapshot or trim against a mounted writer. Lease/fence each
  operation and protect running tasks and undelivered products from lifecycle
  cleanup. Online expansion and offline shrink need separate acceptance tests.
- Instance admission still counts all projects, versions and temporary work.
  Per-project caps cannot reserve capacity that the instance does not possess.
- Count browser profiles within the project allocation. Prune only declared,
  reproducible caches; include retained data in immutable version manifests.
- Keep the current one-hour disposable-task cleanup separate from project
  storage. Copy accepted artifacts into a project version before task cleanup.
- Product API must report backend and `quota_mode` honestly. Enabling a GUI
  switch cannot upgrade soft enforcement into a filesystem guarantee.

## Outstanding M0 Gates

- [x] Docker Desktop Mac: filesystem ENOSPC and independent second writer.
- [x] Docker Desktop Mac: A-variant and B comparison, sample performance, cleanup.
- [x] Host QCOW2 discard smoke check (partial VM evidence only).
- [ ] Disposable native Ubuntu host: A/B, concurrent writers and performance.
- [ ] Disposable native NixOS host: A/B, concurrent writers and performance.
- [x] Real local guest: ext4 delete + `fstrim` + virtio `discard=unmap`, measured
      backing-chain allocation, unchanged base and integrity after remount.
- [ ] Native backend/libvirt integration and integrity after VM restart.
- [x] Synthetic turn, tool, immutable original ledger and Outbox takeover:
      `tests.test_project_m0`, one test, OK in 0.981 seconds. Log:
      `.cache/roadmap/m0-synthetic-accepted.log`.
- [x] Local checkpoint full regression: 2168 tests, OK (12 skipped), 358.407
      seconds; `.cache/roadmap/m0-final-full.log`. Ruff, compileall and whitespace
      checks pass. This does not close the native-host/guest gates above.

No native Ubuntu/NixOS test VM was available in the local workspace during this
checkpoint. The LinuxKit guest closes a different gate. Do not substitute
containers with those distribution names for a native-host test: they share
the Desktop kernel and Docker daemon. M0 remains open; native/backend gates must
be recorded before a production hard-quota claim or automatic project enablement.
