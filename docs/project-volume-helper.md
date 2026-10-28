# Project volume helper checkpoint

M2 work in progress. This is a private helper primitive, not a public project
feature. `serve()` does not construct `ProjectVolumes`, so an ordinary installed
runner continues to reject every `project_volume_*`, `project_workspace_*` and `project_files_*`
request. Desktop isolation,
existing task/shell sandboxes and production configuration are unchanged.

## Ownership and disk identity

- State lives below the runner-owned private `project-volumes/p<id>` directory.
  The caller supplies a project ID, owner and operation, never a host pathname,
  loop device, mount options, filesystem type or command.
- `volume.json` is atomically replaced and fsynced. It binds the project to the
  instance, hashed owner, filesystem UUID, backing-file device/inode and intended
  operation. There is one serialized helper journal, not another business DB.
- Images are private regular files with one hard link. Missing or replaced
  images, symlinks, unknown partial formats and foreign mounts fail closed.
  Missing data is never silently replaced with an empty project.
- Every observation rediscovers the loop mapping by backing path and identity.
  Its backing device/inode, offset, size limit and read-only flag are verified.
  Mount verification includes device number, exact target, filesystem root,
  ext4 and `rw,nosuid,nodev`. A remembered `/dev/loopN` is not sufficient.
- Docker consumers include stopped containers. Their references must be removed
  before filesystem maintenance; restarting one with an old bind is not safe.
  The project driver now reserves its binding in the helper journal before
  Docker admission. Maintenance rejects even a pending creation with no
  container yet. Returning a verified path alone does NOT close that race.

## Fixed RPC operations

The existing authenticated local Unix socket accepts `project_volume_status`,
`create`, `mount`, `unmount`, `resize`, `fsck`, `trim` and `delete` with the common
`project_volume_` prefix. Capacity is 64 MiB to 100 GiB in MiB increments.
Returned status contains capacity/allocation, attachment, mount and pending
operation state, not private image paths or filesystem contents.

Mutations require a positive, monotonically increasing per-project `fence`.
Retrying the same fence requires the same operation and arguments. A higher
fence cannot overtake an unfinished mutation. The caller also reuses its RPC
request ID for transport retries; these are two different identities:

1. RPC receipts prevent accidental duplicate transport execution.
2. The durable volume intent lets a new request reconcile an interrupted
   operation after a receipt becomes uncertain.

The application persists and allocates a fence before dispatch in
`project_lifecycle_steps`. A job's attempt number is NOT an adequate
project-wide fence. The lifecycle journal owns that allocator. The private OCI
executor below consumes these steps; production registration stays disabled.

Native utilities run in a worker thread. Cancellation waits for the bounded
utility to finish before releasing the runner lock. It is not rollback and does
not report cancellation as proof that no filesystem change occurred.

## Recovery and destructive operations

- Create writes intent before creating/formatting. A known completed format is
  checked, not formatted again, when the helper resumes.
- Mount can resume after loop attachment, mount or ownership initialization.
  A new mount never hides unexpected files in the underlying directory.
- Expansion extends the file, refreshes loop capacity, resizes ext4 and verifies
  actual size before committing the new quota. The helper can resize a mounted
  volume, but an attached project workspace must first be stopped and removed;
  a live application resize is not exposed by this checkpoint.
- Shrink requires no container references, no mount and no loop attachment.
  It checks the filesystem, shrinks ext4 and verifies size before truncating the
  backing file. A failed/ambiguous resize remains pending, never silently accepted.
- Unmount is neither forced nor lazy. Detach must be observed before success.
  `fsck` and deletion require a detached image; trim requires an idle mounted one.
- Delete persists intent before unlinking. Retrying that same pending deletion
  can finish after the image has already disappeared. The tombstone prevents
  recreating the same workspace generation. A status request in this interrupted interval can
  fail because the image is missing; the persisted intent, not a generic retry
  with a new fence, is the recovery source.

Unknown format damage or an unrecoverable pending resize requires operator
inspection. Do not erase the journal, advance the fence or reformat to clear an
error. A future compensator must independently prove a safe recovery action.
Helper deletion is not a replacement for application-level retention, running
task, undelivered-artifact or accepted-snapshot guards.

## Workspace generations

`generation` identifies the materialized workspace, not the project or RPC.
All volume operations accept it; omission means generation 1 for old callers.
An absent journal can only create generation 1. Replacing a completed workspace
tombstone requires exactly the next generation and a strictly higher fence.
The new UUID, generation and first pending intent are published atomically;
the old fence high-water mark is never reset. An interrupted format resumes
with the same generation/UUID/fence instead of formatting a third workspace.

Reads, bindings and mutations reject a superseded generation even if its caller
supplies a higher fence. This prevents delayed old work from deleting the new
workspace after restore. The application still distinguishes archive/restore
from permanent project deletion; the helper does not authorize either policy.

## Docker project-workspace adapter

`ProjectWorkspaces` adds fixed `project_workspace_start`, `stop`, `remove` and
`status` Unix operations. They require an explicit generation; mutations also
require a fence. No caller chooses an image, path, mount option or host command.
The ordinary `create` RPC still accepts only task/shell purposes.

- Before creating Docker resources, the helper persists the sandbox ID,
  random creation token, pinned image and mounted root device/inode. Interrupted
  calls retain this reservation. Reconciliation uses the same container name;
  a new transport receipt does not allocate another workspace.
- The per-instance allocation lock covers project and task capacity admission.
  Project containers share the existing per-owner and global running limits.
  File transfers now hold the same lock as commands; project stop/remove cannot
  interrupt an admitted transfer. A pending physical operation blocks new I/O.
- The project workspace is a bind of the private quota mount with `rprivate`,
  not a temporary named volume. Container startup checks its root identity
  before admitting application commands. A separate Docker exec probe verifies
  the running container sees the same root as the helper.
- Inspection checks instance/owner/project/generation/token labels, pinned
  image, mount source/type/access, readonly Nix cache, user, entrypoint, resource
  limits, network and confinement. Changed mount identity, daemon observation
  failure or altered isolation cannot be reported as ready.
- Stop/start/remove of an existing container uses its full immutable Docker
  ID. Removal refuses a running container and never uses force. Ordinary
  destroy/start/stop and task reclamation cannot manage a project container.
  Removing the container retains project files and permits later maintenance.
- A new transport request replaying an acknowledged fence observes the result instead of re-executing
  side effects. An externally stopped container needs a new start fence; a
  missing referenced container is reported as `missing`, not verified success.
- Readonly imports use only the current project bind and cache, with an
  independent root-identity check before the short-lived helper can write.
  Public responses never contain host paths or the private creation token.

Both filesystem and Docker observations must use the same local daemon. A
namespace or isolation mismatch deliberately retains the journal for recovery,
rather than deleting a possibly useful workspace. A change to configured
resource limits can also quarantine an older container until reconciliation;
automatic compensation/reconfiguration is not implemented yet. This adapter
does not turn fixture evidence into native acceptance and is not enabled by
the default runner configuration.

### Host reboot reconciliation

Bindings now retain the kernel boot UUID as well as the mounted root identity.
After reboot, status reports `needs_recovery`, not a ready empty directory.
Ordinary commands and file transfers remain blocked until reconciliation.

- Recovery requires a different, readable canonical boot UUID, the same
  backing-file identity, filesystem UUID, capacity and workspace generation.
  A missing mount during the same boot is not permission to replace a binding.
  Older bindings without a boot UUID require explicit operator reconciliation.
- Start retains its pending fence across reboot. It removes only the verified,
  stopped old Docker container, observes its absence and independently checks
  that no other container references the mount. A running container blocks
  recovery; the helper never force-removes it.
- With no mount, the helper checks ext4, attaches the existing image and mounts
  it. It refuses to hide unexpected files in the underlying directory. It does
  not reformat, resize or copy project data.
- The new container identity/token and root identity are published atomically
  before creation. The original start remains pending until Docker and an exec
  probe confirm the correct mount. Interrupted mount/publication/start stages
  resume the same fence rather than allocating another project generation.
- Backing-device/inode changes, corrupt filesystems or changed isolation still
  fail closed. This is reconciliation on a requested start, not a boot-time
  scheduler or a promise to recover arbitrary filesystem damage.

`tools/verify_project_workspace_native.py` exercises the real Unix RPC, quota
filesystem and Docker driver in a marked disposable VM. Prepare fills one
64 MiB project to ENOSPC, checks a second project remains writable, and leaves a
start completion unacknowledged. After a real poweroff and second boot, recover
checks both original files and filesystem UUIDs, replacement container identity,
and final removal of owned containers, loops, mounts and cache volume.

Use `tools/verify_project_native_vm.py --product-helper` with the existing
Ubuntu/NixOS fixture assets. The ISO needs `python-worker.tar` (a local exported
`python:3.12-slim`) and `workspace.tar` containing `src/__init__.py`,
`src/storage_admission.py`, `src/project_content.py`, `src/local_runtime/`, `src/sandbox_runtime/`,
`verify_project_workspace_native.py` and `project-worker/` from
`tools/fixtures/project-worker/`. Exclude bytecode caches. The tar preserves
module filenames and permissions even with ISO9660 readers. The fixture image
has only the seed script required to exercise storage; it does not validate Nix
package installation or an application dependency rebuild.

## Durable lifecycle journal

Migration 0044 and `project_lifecycle.py` add a private control-plane checkpoint:

- Requests validate scope, membership, editor rights and optimistic revision.
  The existing `DurableJobStore` queue, frozen transition plan, workspace
  generation and project lock commit together. Repeated request keys return
  the same operation; changing its actor/action is rejected.
- Supported transition plans are wake, stop, hibernate and archive. New projects
  materialize on first wake; archive restore creates the next generation.
  Pinned projects cannot archive and an archived project without a retained
  version cannot restore as an empty directory.
- Each step records its index, operation, frozen arguments, project-wide fence,
  RPC request ID, dispatch lease and bounded evidence before advancing. The
  next step cannot overtake an unacknowledged step.
- Reclaiming an expired queue lease preserves the physical effect fence and
  arguments but rotates the uncertain transport receipt. The former worker
  cannot acknowledge, snapshot or complete after losing its lease.
- Ordinary metadata/version writers cannot bypass the lifecycle lock. The
  private snapshot continuation requires the current lease and an actually
  pending `snapshot_workspace` step; expiry is checked again before publishing.
- Linked non-completed tasks, unresolved file receipts, missing required file
  delivery, and artifacts without current-revision acknowledgements block
  transitions. Checks are intentionally conservative, including historical
  unresolved outputs. Reconciliation must release these references explicitly.
- Snapshot acknowledgement must reference the real latest retained version.
  Final acknowledgement must identify the expected state and workspace.
  Finishing changes the state, settles the queue job, releases the lock and
  writes the event in one transaction. Failure leaves the lock and journal
  intact; no timeout handler automatically deletes a workspace.

The journal itself does not execute physical operations. `verified` evidence is
accepted only from trusted host adapters, never a model, tool input or HTTP
request. The private OCI executor below verifies helper observations; journal
unit tests still use explicitly labelled fixture observations. End-to-end native
acceptance and atomic coordination of task attachment with maintenance are
mandatory before enabling a production worker. Generic queue retries alone must
not be mistaken for a complete compensator.

Remaining lifecycle integration includes physical adapters, automatic idle
scheduling, dependency reconstruction, retention sampling, trash/purge, and
recovery UI. Hibernated snapshots now have a private offline export path without
starting an application writer. Locked classification and pruning are available
through the explicit file adapter; dependency reconstruction remains unavailable.
Do not register `project.lifecycle` as a worker
kind until these adapters honor every persisted step and independently verify
the resulting state.

## Reserved OCI lifecycle executor

`project_executor.py` explicitly composes a `ProjectLifecycleExecutor` with a
host-resolved access callback and an OCI backend over the authenticated Unix
client. Nothing is registered by importing it. The installed runner and ordinary
Bot startup still do not enable this project executor.

- New frozen plans use protocol 2 and a random private maintenance reservation.
  Public requests and journal views strip that credential. The helper stores
  only its hash and exposes only `maintenance_active`, never the credential.
- `project_workspace_reserve` and `release` use the same generation/fence rules
  as other effects. Reservation changes acquire the allocation and I/O locks
  before publishing the private journal. New commands/transfers and unrelated
  starts/stops/volume maintenance are then refused across all subsequent steps,
  including after runner restart. Existing active file transfers block entry.
- Reservation does not stop background writers. The following quiesce step
  stops and verifies the owned container before snapshot work. For archive from
  hibernation, no container exists: mount the retained volume for the snapshot,
  then unmount/delete. Never start an application just to export its files.
- Only the owning reservation may continue or release maintenance. Release is
  replayable; deletion retains the reservation in the tombstone until its owner
  releases it. A fresh generation cannot be created while it remains held.
  Expiration alone never clears ownership or permits data deletion.
- Before each step and after a potentially long effect, the executor checks
  the current host-resolved actor, live membership, scope/editor rights, durable
  lease, generation and project task/delivery guards. A missing identity is an
  error, not an anonymous privileged continuation.
- Lost acknowledgements preserve the effect fence while lease takeover obtains
  a fresh transport receipt. Already performed effects are reconciled. Final
  verification uses uncached helper observations of actual container/mount/
  quota state and reservation release; exit status alone cannot complete a job.
- Domain completion atomically settles the queue. The generic worker recognizes
  same-generation settlement rather than cancelling a just-completed handler or
  reporting a false lost lease. Different results/generations remain errors.

**Available now:** private OCI create/reserve/mount/start/release/verify dispatch,
including durable takeover. Stop/remove/trim/unmount/delete primitives are wired,
but a whole plan is preflighted before its first mutation. The explicit file
adapter below enables stopped-workspace snapshots and retained-version restore.
Without it, plans needing files are refused before touching containers.
The file adapter also enables locked dependency pruning for hibernate. Any wake
plan requiring dependency reconstruction still fails preflight before mutation;
an incomplete recovery cannot be reported as active. VM plans,
old protocol-1 plans and malformed reservations also fail preflight; old pending
plans must be explicitly reconciled, not silently rewritten or unlocked.

The 100-test focused checkpoint uses real PostgreSQL and Unix sockets with
explicit Docker/kernel fixtures. It proves control-plane composition and race
handling, not native filesystem semantics or completed M2 acceptance. Previous
native reboot evidence covers the helper before this reservation addition.

## Offline retained-file checkpoint

`DockerProjectFiles` is an explicitly supplied adapter, not a registered tool or
startup handler. It implements `snapshot_workspace`, `restore_version` and
`prune_rebuildable`.
The private `project_files_*` RPCs require the current owner, generation, fence
and maintenance reservation; they accept file indexes, never host paths.

- Snapshot requires a stopped, identity-verified container or no container at
  all. The helper enumerates the mounted tree with descriptor-relative access,
  rejects symlinks, hard links, special files and nested filesystems in retained
  data, and records hashes plus file identities. It ignores an empty root
  `lost+found` and the explicitly classified rebuildable roots described below.
- Manifests are canonical and paginated (128 entries); binary payloads are at
  most 1 MiB and obey the smaller configured transfer limit. Limits remain
  256 MiB per file, 20,000 files and the project quota. No archive extraction,
  writable container or temporary full-tree copy is used for export.
- Migration 0045 records private staged snapshot references. Bytes stream to
  immutable objects without holding the queue job row, allowing heartbeat
  renewal. Each completed file is checksum-verified and rechecks the lease
  before its reference commits. Staging counts toward scope quota and protects
  objects from GC. Takeover verifies and reuses completed objects.
- Before version publication the helper freshly checks the complete source
  tree; the store rechecks access, lease, manifest and staged object metadata.
  Publishing the version and removing its staging rows is one transaction.
- Restore accepts only an empty, never-started volume and a pinned manifest.
  Chunks append or replay identical bytes, never silently overwrite different
  bytes. A restart resumes the same generation/fence and verifies every final
  file hash. Missing/extra files prevent completion; unrelated files are kept.
- `restore_finish` records the complete-tree check, then `restore_check` repeats
  the observation as an uncached read before container startup. A replayed
  mutation receipt alone is not proof the tree still matches. Empty files and
  executable modes are preserved; arbitrary ownership/special modes are not.
- Partial restores remain reserved and unstarted. The private upload contains
  only the bounded manifest, removed on completion. Unexpected linked upload
  files are refused before truncation. Failure does not delete source files,
  discard reservations or pretend a version was published.

The local file/RPC/PostgreSQL tests use simulated mounts and Docker; they are not
native Linux acceptance. No project tools, UI or production worker are enabled
by these changes.

## Locked classification and cleanup

The helper recognizes these bounded, root-project recipes:

| Recipe | Required inputs | Dependency directory |
| --- | --- | --- |
| `npm ci` | `package.json`, `package-lock.json` (lockfileVersion 1-3) | `node_modules` |
| `uv sync --frozen` | `pyproject.toml`, `uv.lock` | `.venv` |
| `cargo build --locked` | `Cargo.toml`, `Cargo.lock` | `target` |

Only explicitly supported flags are accepted. The helper parses JSON/TOML and
pins input hashes; it does not claim that installation has succeeded. It never
executes the command on the host. Arbitrary shell commands, missing or malformed
inputs keep dependency files as retained data and attach a warning to snapshot
evidence. Retained symlinks/special files still stop the snapshot without deleting
anything. Monorepo command routing and other package-manager recipes are not yet
supported. Default dependency directories apply only at the project root;
nested projects keep their dependencies unless explicitly declared rebuildable.

- With a recognized recipe, ordinary cache directory names and the three exact
  `.gaoji/browser/{Cache,Code Cache,GPUCache}` paths are rebuildable. Browser
  cookies, notes, Git metadata and source files remain retained.
- `.gaoji/rebuildable` may add up to 64 exact relative directory paths, one per
  line; blank lines and `#` comments are ignored. Traversal, wildcards, protected
  metadata, overlapping roots and paths hiding locked inputs are rejected. The
  control file itself is retained and hash-pinned. Only observed real directories
  are excluded; the helper records their filesystem/inode identities.
- Format-2 version manifests bind retained hashes, recipe and observed excluded
  paths together. Format-1 manifests remain readable and byte-compatible.
  Rollback preserves the specification. Wake/archive use the retained version's
  recipe even if mutable project metadata was subsequently changed.
- Hibernate requires an acknowledged, published current snapshot. The lifecycle
  step freezes its digest and source fence. The helper rechecks retained content
  and writes a private cleanup journal before beginning deletion. No glob, model
  path or host shell is accepted by the cleanup RPC.
- Cleanup is descriptor-relative and refuses replaced roots, nested filesystems
  and special files. Symlinks inside excluded directories are unlinked, never
  followed. Traversal is bounded by count, depth and time. A crash preserves the
  same fence/reservation, manifest and remaining roots for takeover.
- A separate uncached `prune_check` confirms retained content still matches and
  all selected roots are absent before container removal/hibernate completion.
  Missing acknowledgements do not cause another snapshot or uncontrolled delete.

### Isolated dependency reconstruction

Wake from hibernation, or restoration of a version carrying a locked recipe,
uses `rebuild_dependencies` before `start_workspace`. A plain stopped workspace
keeps its dependencies and is not reinstalled. Versions without a supported
recipe retain their dependency files; mutable metadata alone does not authorize
an installer. Old frozen plans with an unsafe ordering fail whole-plan preflight.

- Private `project_rebuild_run/status` RPC accepts only project/generation/fence,
  reservation and manifest digest. The recipe comes from the retained manifest,
  never from an RPC shell argument, and is classified again before execution.
- One named Docker container uses a pinned image, non-root user, read-only root
  and Nix store, bounded CPU/memory/PIDs/logs, the existing network policy and
  exactly the reserved project bind. Root identity is checked inside the
  container before running. npm uses the image toolchain; uv/cargo toolchains
  are resolved through the existing isolated package preparation mechanism.
- A private journal records immutable container identity, recipe, original
  deadline and outcomes. `docker create` and `docker start` are separate:
  a lost creation acknowledgement can recover a never-started container,
  while a missing already-dispatched container is not recreated. There is no
  automatic container restart or auto-remove that could erase the exit result.
- The installer has a fixed 900-second deadline, including toolchain preparation,
  enforced by in-container timeout and later reconciliation. Worker cancellation
  or runner restart does not silently replay it or extend the deadline. Host
  reboot/mount identity changes require inspection rather than guessed recovery.
- Completion requires zero exit, no OOM, unchanged retained files/input hashes,
  removal of the observed stopped container and a completed volume journal.
  A separate uncached status rechecks the retained tree before acknowledgement.
  Failed installation preserves project files, version and maintenance reservation
  with durable failure evidence. Compensation/retry UX is a remaining M2 gate.
- The database stores running/failure progress without advancing the step. Only
  its current lease may publish; takeover keeps the same effect fence. The
  ordinary workspace starts only after verified reconstruction. A completed
  archive removes validated same-generation internal journals, so a new volume
  generation is not blocked by stale manifests; unknown files are never swept.

The private executor has PostgreSQL/Unix RPC coverage for this path with explicit
Docker/kernel fixtures. A production worker remains disabled pending native
full-lifecycle acceptance. No automatic scheduler is installed.

## Private VM provisioning

`VmProjectProvisioner` constructs an independent system overlay and cloud-init
seed around a claimed `VmProjectDisks` data disk. It remains a private host API;
normal temporary-sandbox creation still rejects `purpose="project"`. A completed
boot does not enable project tools or register a lifecycle worker.

One maintenance reservation and one effect fence cover the entire boot. The
host journal freezes the sandbox ID, libvirt UUID, base-image path/hash/identity,
resource limits and generated-file identities before the corresponding effects:

```text
allocated -> preparing -> prepared -> defining -> defined -> starting
          -> started -> ready
ready -> resuming -> defined -> starting -> started -> ready
```

- Only `preparing`, with a fresh libvirt inventory proving domain absence, may
  regenerate known pre-boot system files. Unknown files, replaced directories
  or prepared artifacts stop recovery. The independent project disk is never
  removed by provisioning. A missing previously defined/booted workspace needs
  explicit recovery, not a silent empty replacement.
- A lost define/start acknowledgement reconciles the same immutable domain.
  If the host restarted after VM startup, retry may restart that exact stopped
  VM after checking capacity again. An already running VM is not started twice.
  Invalid, absent or conflicting domain definitions cannot count as success.
- Every start shares the VM allocation file lock, including ordinary temporary
  VMs. Different manager instances cannot both pass admission at once. This is
  separate from the per-VM operation lock described below.
- Project cloud-init creates only the unprivileged `sandbox` account, avoiding
  competition with a distribution's default user. The guest helper also verifies
  that `sandbox` exclusively owns UID/GID 1000 before changing ownership or
  reporting workspace usage; an incompatible image is rejected, not rewritten.
- Completion requires finished cloud-init, matching live and inactive libvirt
  definitions, the expected project mount and a sealed initialization receipt.
  Updating host defaults does not silently change a retained workspace's resource
  contract. Normal IO remains blocked until maintenance is explicitly released.
- Cancellation waits for local image/seed writes to finish before unlocking.
  Host subprocess cancellation, including cancellation during process creation,
  kills and reaps the child. It does not claim to undo an already dispatched
  libvirt effect; replay inspects the journal and actual domain instead.

`tests/test_project_vm_provision.py` covers boot recovery and failure boundaries
with explicit libvirt/guest fixtures, plus real local QEMU overlay creation and
read/write checks. The latter uses a fixture seed and does **not** boot Linux.
Native cloud-init, guest-agent, quota, discard and reboot acceptance remain
required, alongside the VM-specific database lifecycle, native writer-quiescence acceptance,
retained-file adapters and hibernate/archive retirement path. Do not substitute
the OCI lifecycle order: a stopped VM cannot run guest file/trim commands.

## VM execution epochs and writer quiescence

The private VM adapter now uses two distinct forms of exclusion:

1. A private host file lock, shared by provisioning, command execution, reads,
   imports, exports and maintenance. It sits outside the VM directory so domain
   retirement cannot replace its inode. An active operation in another process
   prevents maintenance admission. Cancellation waits for lock acquisition and
   any journal write to settle before releasing owned resources.
2. A root-owned guest gate in `/usr/local/lib/gaoji/project_gate.py`. Its journal
   persists on the system disk; a missing journal is closed, not implicitly
   active. Every project command carries the host's settled release fence as
   its execution epoch. Delayed commands from an older epoch are rejected even
   after the project is reopened. Guest execution is serialized independently
   of host process lifetime.

Commands, package installation and fixed root file import/export helpers run
in transient systemd services in `gaojiproject.slice`. `RuntimeMaxSec`,
`TasksMax`, `KillMode=control-group`, no-new-privileges and read-only cgroup
control apply to the command tree. User code still runs through `runuser` as
`sandbox`; the execution gate does not grant it root. Shell dollar signs are
escaped for systemd's environment expansion before the original arguments reach
the shell, as specified by the [systemd-run manual](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html).

The private lifecycle sequence is:

```text
reserve host workspace -> quiesce guest at current reservation fence
  -> reserved guest file snapshot / restore / prune
  -> activate guest at next release fence -> release host reservation
```

Quiescence persists `closing` before stopping command cgroups. It acknowledges
`quiesced` only while holding the exclusive guest IO lock, after observing an
empty cgroup and no remaining sandbox-UID processes. A failed stop, busy IO,
unexpected process or unreadable cgroup leaves the gate closed. The return code
of `systemctl stop` alone is not evidence. First boot and resumed provisioning
also drain the gate before completing their boot journal.

Guest activation precedes host reservation release. A lost guest reply retains
the reservation; a retry uses the same epoch. A lost host journal reply is
reconciled using the original release fence, without starting another command.
Stopped guests remain inaccessible and are not implicitly activated by release.

`tests/test_project_vm_gate.py` exercises real private files, fsynced epoch
journals and locks across independent OS processes. Guest mount, systemd and
cgroup actions are fixtures. Native detached-process termination, guest reboot,
QGA disconnect and end-to-end lifecycle acceptance are still required. QGA file
RPCs stage uploads outside `/workspace`; a leaked guest file descriptor must
not be worked around with forced/lazy unmount. This checkpoint does not expose
the private VM project adapter in production or complete its lifecycle wiring.

## VM retained-file transfer

`VmProjectFiles` supplies the private `project_files` transport consumed by
`ProjectFileExecutor`. It retains the OCI manifest, hashing, CAS staging and
restoration contract; the filesystem engine runs inside the VM instead of
mounting untrusted guest storage on the host. No request selects a guest or
host root. The only workspace is the verified project data disk at `/workspace`.

Project cloud-init installs a deterministic, root-only `project_files.pyz` with
an explicit stdlib-only module list. It includes the shared retained-file engine
and validation code, not the Bot, host Docker/libvirt drivers or credentials.
The host invokes it with isolated Python (`-I`). Normal temporary VMs are
unchanged. Existing frozen project seed images are not rewritten in place.

Every call holds the same host IO lock and the guest execution gate's control
and IO locks. The guest must already be quiesced, with no user processes or
populated execution cgroup, and its mount identity must still match. The file
journal owns successive operation fences inside a reservation; failed file
validation does not advance the execution epoch and strand an earlier pending
restore. All state remains private to the guest administrator.

The host journals snapshot/restore/prune intent before QGA delivery. A missing
reply leaves maintenance reserved. Guest-side retries use fsynced manifests,
file identities, exact offsets and prefix-byte comparisons. Restore completion
requires a fresh scan of the complete retained tree. Host acknowledgement is
bound to the same project, generation, domain, fence and manifest. Source
snapshot identity is checked before pruning frozen rebuildable roots.

The QGA transport caps binary chunks at 48 KiB because its input is nested
base64 inside a `virsh` command argument. Larger files use successive chunks,
not larger command-line arguments. Manifests are paginated; transfer has no
automatic overwrite or fallback to the guest system disk. Throughput and
maximum-size deadline acceptance on native guests remain open gates, not
inferred from the fast in-process test transport.

`tests/test_project_vm_files.py` covers actual files, hashes, journals, chunk
retries, executable modes, empty files, path/link rejection and a deterministic
bundle imported by a fresh isolated Python process. Guest mounts, cgroups and
libvirt are fixtures. Full lifecycle composition, native crash/reboot/hibernate
acceptance and product registration are still pending; this private transport
alone does not enable project VMs.

## VM dependency reconstruction

Retiring a VM removes its system disk, including the private guest file journal,
while preserving its project data disk. A wake must not restore a snapshot over
that nonempty workspace merely to recreate metadata. The private VM file client
instead imports the exact canonical manifest from the PostgreSQL version store.
The lifecycle executor checks its frozen version digest and rebuild recipe;
each upload chunk rechecks the current lifecycle lease and project membership.
The guest compares both lockfiles and every retained file against this manifest
before allowing an installer to start.

`dependency_begin` freezes the manifest size/digest and the project's process
limit at its current reservation and fence. `dependency_manifest` uploads
prefix-verified 48 KiB chunks. These actions do not write to the project tree.
Private staging is durable before fence admission, so interrupted preparation
can resume. Source, resource or reservation changes cannot reuse that operation.

`dependency_run` and `dependency_status` drive a dedicated systemd supervisor,
independent of the QGA client's lifetime. Its unique unit, guest boot ID,
deadline and phase are journalled before dispatch. The worker journals `running`
before executing scripts, holds the closed project gate/IO lock, and uses the
existing cgroup-limited command runner. Status and repeated preparation do not
wait for that worker's IO lock. A lost start reply reuses the original unit;
an absent unit may be started only if the journal proves it never entered
execution, the boot ID is unchanged and the deadline has not expired.

Supported locked recipes remain `npm ci`, `uv sync --frozen` and
`cargo build --locked`, with the existing flag allowlist. Guest toolchains are
installed on demand from fixed administrator commands; uv uses the repository's
CI pin. Project commands execute as `sandbox` with a clean environment, never
as root or on the host. Admission rejects dependency installation at 95% quota.
The whole operation has a 900-second deadline, including toolchain installation.

Successful exit alone is insufficient. The worker must drain its command
cgroups, prove no sandbox-user processes remain, and recheck retained files.
The controller then stops the supervisor, checks the tree again, commits guest
completion and acknowledges the host journal. A lost completion receipt resumes
these checks, not the installer. Reboot, timeout, changed source, unexpected unit
identity or missing execution result keeps maintenance reserved and reports an
error. No uncertain installer is automatically replayed; recovery needs a new,
explicitly reconciled lifecycle operation. Failed scripts can leave dependency
files behind, but this path never silently restores or deletes retained data.

`tests/test_project_vm_dependencies.py` exercises real metadata, source files and
host/guest journals with explicit systemd/libvirt fixtures, including interrupted
uploads, lost dispatch/completion, quota, changed locks, leftover processes and
reboot. `tests/test_project_file_executor.py` additionally checks authoritative
PostgreSQL version preparation. These checks do not constitute native installer,
power-loss or end-to-end VM lifecycle acceptance.

## VM shutdown and retirement

VM lifecycle plans use protocol 3; OCI plans retain protocol 2. A guest cannot
snapshot or trim its disk after it has shut down. The VM sequence therefore
boots the reserved guest when necessary and finishes guest operations first:

```text
stop:      reserve -> quiesce -> snapshot -> shutdown -> release -> verify
hibernate: reserve -> boot closed -> snapshot -> prune -> trim -> unmount
             -> shutdown / undefine / remove system files -> release -> verify
archive:   reserve -> boot closed -> snapshot -> unmount -> retire VM
             -> delete project disk -> release -> verify
wake:      [create disk] -> reserve -> boot closed -> [restore] -> [rebuild]
             -> release / activate -> verify
```

`VmProjectMaintenance` journals quiesce, trim, unmount, stop and VM retirement
against the current reservation, generation and effect fence. It does not
delete the project data disk. Snapshot publication and active-task/delivery
protection remain the responsibility of the durable lifecycle executor.

Disk maintenance uses the root-owned guest execution gate. It requires closed
admission, exclusive guest IO and disk locks, an empty command cgroup and no
sandbox-UID processes. Trim acknowledges the verified mount and usage; unmount
acknowledges the expected filesystem and absent mount. Repeating unmount after
a lost QGA reply does not require the disk to still be mounted. Boot invalidates
previous unmount evidence before another retirement can use it.

Graceful shutdown is requested only after the guest gate closes. The host
observes the original immutable libvirt domain before recording `stopped`.
If the journal survives but the shutdown request was never sent, a retry may
repeat the idempotent request after rechecking that same running domain and
guest gate. An already stopped VM does not receive another shutdown request.
Timeouts never force-destroy the guest, delete disks or release its reservation.

Retirement additionally requires a verified unmount. Undefine must be observed
before system-file cleanup starts. The cleanup allowlist is the frozen boot
artifacts and recorded agent socket, not a recursive directory deletion. File,
directory and socket identities, immutable-file hashes, and other VM consumers
are checked before unlinking. Progress and each deletion are fsynced; retries
allow only files already absent in the recorded deletion phase. Replaced files,
unexpected contents or a reappeared domain fail closed. The independent data
disk and its first-initialization seal survive retirement and subsequent boot.

`tests/test_project_vm_maintenance.py` covers private journals, real file/socket
identities and interrupted effects, including shutdown intent without delivery,
lost QGA replies, partial deletion and reboot with the same data. Guest disk
receipts also run through `ProjectGuestWorkspace` and `ProjectGuestGate` rather
than only hand-built JSON. OS mount, cgroup and libvirt effects are still fixtures;
native lifecycle acceptance is not claimed.

## Private VM lifecycle composition

`VmProjectRuntime` combines disk creation/deletion, reservation, boot and guest
maintenance. `VmProjectBackend` connects it and `ProjectFileExecutor` to the
existing PostgreSQL `ProjectLifecycleExecutor`; it accepts only protocol 3 and
valid ordered transition plans. No import registers a production worker or
exposes these operations to model calls. Host composition must supply the
current-membership resolver, the VM manager/disks, and the VM file transport.

Each step uses its persisted effect fence. A replacement job lease changes the
transport identity, not the effect identity. Snapshot publication must precede
pruning/retirement; dependency and restore retries preserve the same generation
and frozen manifest. Errors keep the lifecycle job and maintenance reservation
available for reconciliation rather than deleting the workspace.

Final verification is a fresh external observation, not just the previous
command's success flag. The private host reader holds allocation and IO locks,
checks the immutable domain, pinned bootstrap files, data-disk consumers and
unchanged journal before/after inspection. Running projects additionally require
a read-only guest receipt proving mounted data, the exact active admission
epoch and no writers. Stopped projects require the original stopped domain;
retired projects require the original domain and system directory to be absent.
Archive additionally requires the data disk to be gone. A stale fence, changed
guest epoch, revived retired VM or replacement directory prevents completion.
Reservation release validates stopped domains too, not only running ones.

Project VM serial output uses a PTY rather than creating an unbounded host-side
`console.log` outside the retirement journal. Nonproject VMs retain their
existing console-log configuration. Guest task output still uses QGA.

`tests/test_project_vm_executor.py` exercises PostgreSQL leases and CAS versions
with real retained files and guest journals, including stop, retained-volume
hibernation, dependency reconstruction, archive and restore into a new disk
generation. Lost snapshot/chunk/activation/retirement/dependency acknowledgements,
lease takeover, revoked membership and contradictory final observations are
covered. Libvirt, mounts and systemd remain explicit fixtures: this is local
composition evidence, not native KVM or production acceptance.

## Evidence and remaining gates

### VM accounting and idle scheduling

The private `VmProjectBackend` now implements `sample_usage` and
`maintenance_ready` for the existing `ProjectUsageSampler` and
`ProjectMaintenance`. Host registration and live membership resolution remain
explicit and default-disabled. This does not enable automatic deletion.

Running guests report actual filesystem capacity, with optional locked-recipe
rebuildable-data accounting. Sampling uses the same admission/IO locks and disk
identity checks as execution. Busy writers or an incomplete directory scan
leave rebuildable bytes unknown, not zero. A missing recipe retains dependencies.
The guest helper remains stdlib-only and never executes the rebuild command to
measure usage.

Stopped and hibernated VMs are not booted or mounted merely for accounting.
The host reads exactly the first 2 KiB through `qemu-img dd`, without force-share,
then validates the ext4 superblock's UUID, quota geometry, clean/recovery state,
features and CRC32C. QEMU locking and unchanged file/domain/journal identity are
required before and after the read. Dirty, unsupported or unverifiable storage
produces an unknown sample; it is never repaired automatically. Host qcow2
allocation remains a separate number from filesystem usage. Offline rebuildable
usage remains unknown until a guest scan; this estimate never authorizes cleanup.

Sampling failures preserve last-known usage but block new quota admission.
Confirmed archived data disks report zero workspace bytes; retained version
blobs remain separately charged. Idle scheduling uses current VM observations,
rechecks live ownership, and lets the PostgreSQL transaction reject active tasks,
failed work and unacknowledged deliveries before queueing a durable transition.
Sampling and automatic snapshots do not reset real user activity.

`tests/test_project_vm_accounting.py` exercises the composed PostgreSQL sampler
and scheduler across active, stopped, hibernated and archived states, plus
changed VM/disk identities, missing receipts, revoked access, live writers and
undelivered task outputs. `tools/verify_project_vm_usage.py` compares actual
qcow2/ext4 reads with `dumpe2fs`, checks populated files and reserved blocks,
rejects dirty filesystems, and proves source-image bytes are unchanged. It runs
only in a disposable local container without capabilities or active networking;
it is not a native KVM or crash-recovery acceptance test.

### Docker Desktop soft quota

The Desktop runner uses `DirectoryProjectVolumes` inside its existing private
data volume. Only an exact `volume-subpath` is exposed to each sandbox, with
copy-up disabled. Immutable directory identity, owner, generation and operation
fences are verified; the sandbox cannot access sibling projects or controller
credentials. No loop mount or Docker VM host path is assumed.

The existing reserved snapshot, restore, rebuild and lifecycle operations work
on this directory backend. Accounting and the runner capability report explicitly
say `quota_mode=soft`. Data is charged by allocated blocks; it may exceed the
declared limit. This is not a hard filesystem quota or a guarantee against a
fast writer filling storage between samples.

The runner inspects projects every 30 seconds, including dependency rebuild
containers and writers recovered from private journals after a runner restart.
New executions, writes and readonly imports check capacity before dispatch;
known dependency installation is refused at 95%. Overfull or unreadable usage
stops only the identity-verified project container by immutable ID, without
waiting for its active execution lock. Detached children stop with it. No
retained files are deleted. A failed stop is unverified, not reported successful.
The instance-level capacity guard still applies outside the project quota.

`tools/verify_project_desktop.py` verifies actual volume-subpath isolation and
writer termination using existing local Docker Desktop images. Its only fixture
is an empty readonly Nix cache; it does not install packages or contact servers.
It writes 80 MiB into a 64 MiB project, checks the writer was stopped, checks a
second project remains usable and retained files survive, then verifies restart
after test-only removal of the fill file. All fixture containers and volumes
are removed. Windows, Desktop-engine crashes, controller-loss enforcement and
complete App/workbench usage reporting remain acceptance work, not covered by
this local macOS/Linux-container proof.

### Opt-in application runtime

`AI_PROJECTS_ENABLED` remains false by default. When explicitly enabled,
`ProjectRuntime` is installed by OneBot assembly and starts before the shared
durable worker. It composes the actual file adapter, lifecycle executor,
30-second usage sampler, idle scheduler and version retention. Shutdown drains
the application's workers before closing the project store. Cancelled startup
does not leak a database connection or install a half-ready executor.

The application requires sandbox permission controls, the ledger, turn journal,
durable jobs and Outbox. OCI must use the private runner: its authenticated health response
must report project volumes, workspace support and hard/soft quota mode. Direct
host Docker is not an unlimited fallback. Native runner mount-namespace
activation is still gated on the acceptance work below. Desktop's directory
backend explicitly reports soft quota even before the first workspace exists.

VM composition binds the existing VM manager's project gate to a separate
`project-volumes` directory under its owned root. `AI_PROJECT_INSTANCE_ID` must
be a stable 32-character lowercase hexadecimal installation identifier; App
instances may inherit `AI_APP_INSTANCE_ID` when that setting is empty. Do not
generate a different identifier on each restart. An explicit instance ID is
also checked against an OCI runner's health identity when supplied.

Migration `0047_project_runtime_owner` preserves each new project's original bot
account. Old unbound projects are not guessed or rebound automatically; an
operator must explicitly migrate their account association before execution.
Even another online bot in the same group is not a fallback. Recovery resolves
the actor's current principal identity, checks the group is enabled, fetches
fresh membership (including the bot), and rechecks execution permission. Private
projects remain creator-only. The worker revalidates access after helper effects
and before acknowledgement. Offline accounts/temporarily unavailable membership
defer without spending a retry; confirmed revocation blocks further mutations
while retaining the journal and data.

`AI_PROJECT_IDLE_STOP_MINUTES`, `AI_PROJECT_HIBERNATE_DAYS`,
`AI_PROJECT_ARCHIVE_DAYS` and `AI_PROJECT_VERSION_KEEP_RECENT` configure the
existing policy. Invalid/nonincreasing thresholds are rejected before stores
open. This composition does not enable automatic purge or provide a new host
shell API. Task baseline publication, shared-platform adapters and the workbench
still require M3-M7 integration. Live QQ membership
in these tests is a fixture, not a production validation.

### Initial project chat interface (M3 in progress)

With the opt-in application runtime ready, the tool registry exposes
`project_create`, `project_list`, `project_open`, `project_status`,
`project_versions`, `project_snapshot` and `project_set_rebuild`. The same handlers serve these QQ
commands; a disabled tool cannot be bypassed through its command:

```text
/项目
/项目 新建 班级网站
/项目 切换 prj#3
/项目 状态
/项目 状态 prj#3
/项目 版本 prj#3
/项目 保存
/项目 保存 Release one
/项目 保存 prj#3 Release one
```

Creation stores the project and current-user focus, then enqueues a durable
wake. A queued/failed wake is not advertised as an active workspace. Open
changes only the requesting user's focus; group members without editor or
sandbox permission can view without waking a workspace. Membership is checked
live. Unbound legacy records remain hidden pending explicit account migration.

Migration `0048_project_creation_key` adds a scoped, account/principal-bound
creation identity with a unique database index. It is derived from the received
message and normalized title, not model arguments. Concurrent deliveries and
retries after application restart reuse the project and cannot reset a newer
focus. The original creation parameters remain in its audit event. A different
message can intentionally create another project with the same title.

Status reports configuration `revision` separately from saved-content `vN`,
measured versus unknown/stale usage, hard/soft quota, retained version bytes and
pending lifecycle progress. Failure details and helper reservations are not
exposed. Labels/titles are sent as QQ text, never interpreted as CQ actions.
`project_set_rebuild` records metadata only; the physical adapter still checks
locked recipes before executing a reconstruction.

`project_snapshot` queues a reserved, fenced lifecycle job without changing the
final active/stopped state. The host exports and hashes actual retained files;
the model cannot supply a fictional manifest. OCI briefly stops its idle
container, saves, then restarts it only if it was active. An active VM closes
guest execution admission without rebooting; a stopped VM temporarily boots for
file export and returns to stopped. Neither path prunes dependencies or deletes
workspace data. Active/unknown project IO, task locks and unresolved delivery
still block admission. Unmaterialized, hibernated and archived workspaces must
be opened and prepared before explicitly saving another version.

Migration `0051_project_snapshot` adds this action to the lifecycle journal.
The host event/tool-call identity is resolved before current-project focus, so
retries cannot save a different project. Changed arguments and revoked editor
rights reject replay. Labels are frozen in the plan and propagated to version
publication. Equal content reuses the latest version, optionally updating its
label. `project_status.snapshot` distinguishes pending/running/needs-attention
from `saved=true` with a verified `vN`; only publication plus final workspace
verification completes the receipt. A crash after publication retains the lock
and staging for the next lease, rather than publishing again or claiming success.
Migration `0052_project_autosave` associates each new project mutation with its
host-owned journal turn and atomically records a pending save at admission.
Writes require a running turn with an inbound trigger belonging to the same
scope, actor and current message; model arguments cannot supply that identity.
Existing receipts remain replayable without fabricating historical turn links.

The supervised automatic-save worker scans terminal turns every ten seconds.
Startup journal recovery makes interrupted turns eligible. Reserved-but-never-
dispatched operations are safely aborted; recorded execution results can settle
only after observing quiescence of the exact workspace generation and fence.
An unknown result is never rerun or unlocked on a timeout. Completed commands
with nonzero exit codes may still have changed files and therefore trigger saves.
Only-aborted admissions are recorded as skipped, without making an empty version.

Live membership, original account, original editor and execution permission are
rechecked. Running project turns, unresolved IO, task locks and unconfirmed file
deliveries delay the save. Idle lifecycle transitions wait for pending automatic
saves. The snapshot job and its turn association commit together; lost replies
and repeated scans cannot enqueue another job. The existing fenced lifecycle
worker owns export, publication and restart recovery.

`project_status.autosaves` exposes the latest five turn/save receipts with safe
reason codes and exact update timestamps. `/项目 状态` summarizes the latest one.
Queued work is not saved; `saved=true` requires final workspace verification and
successful job completion. Closely spaced edits can share the same content-
deduplicated version once all writers finish: a turn link identifies the save
trigger, not an assertion that the snapshot contains exclusively that turn's
changes. No background LLM call or group notification is needed for saving.

The ordinary chat prompt now includes the requesting user's current project,
its configuration revision, state, viewer/editor role, pending task and latest
three saved versions, immediately before the current question. Titles, labels
and notes are quoted evidence, never system instructions. The stable system and
group-history prefix is unchanged. Switching/creating a project also returns
the fresh current-project context for subsequent calls in that same turn.

Active projects read `.gaoji/NOTES.md` from the **current workspace** using a
host-only, fixed-path reader. OCI and Desktop use the private helper's bound
volume; VM uses the same stdlib reader through the owned guest execution gate.
The read is limited to an 8 KiB UTF-8 prefix, refuses symlinks/hardlinks/special
files and mount crossings, and detects replacement during reading. It checks
the current generation, execution fence and quiescence. A fresh IO watermark
and membership/focus checks discard evidence raced by another tool or task.
The workspace read has a 1.5-second context budget; errors report unavailable
notes, never substitute an old saved note as current. No workspace is woken,
version saved, focus TTL renewed or idle clock reset just to build a prompt.

Inactive projects use notes from the latest **saved version**, with the same
8 KiB prefix limit plus full-object checksum validation using bounded memory.
Their source/version and truncation are explicit. Missing, invalid or corrupt
notes do not fall back to older versions.
Without a focus, chat makes no extra membership API calls. With a focus,
membership is checked before/after reading and concurrent focus/revision
changes discard the stale result; unavailable membership reports unavailable
context without revealing project data.

### Durable project tool admission

`ProjectRuntime.io` composes `ProjectIOStore` over the same project/usage store.
Migration `0049_project_io` records the actor, argument digest, physical
workspace identity and a private host grant before any dispatch. Only one
operation may be active per project, enforced in PostgreSQL. Lifecycle changes
and snapshots refuse an active or unconfirmed operation. A restart or elapsed
timeout never counts as proof that a command stopped.

Admission requires current focus, account binding and membership, plus editor
rights and fresh usage for writes. Shell execution reserves remaining project
capacity; file writes reserve their expected bytes. Scope locking serializes
admission, cancellation and completion across projects so two workers cannot
spend the same capacity. Snapshot staging/publication and quota reductions use
the same reservations. Completing a write invalidates usage and retains its
capacity estimate until a successful measurement atomically replaces it;
measurement failure does not falsely release that space.

Dispatch is once-only. Pre-dispatch reservations can be aborted, but dispatched
effects require matching quiescent backend evidence to settle. Exact completion
replay is harmless and cannot unlock a newer operation. Migration downgrade
refuses to discard even completed/aborted operation receipts.

The registered `sandbox_read_file`, `sandbox_write_file` and `sandbox_exec`
tools now use this store when `sandbox_id` is omitted in a project-enabled
conversation. Explicit sandbox IDs keep their existing temporary-task behavior.
Group and private callers resolve their current project through live account and
membership checks. Sub-Agent/routine callers cannot inherit it implicitly.
Small current-project edits can use the main loop; full new projects still use
the existing execution entry. Project operations never enter task cleanup.

The host binds each call to its transport event and tool-call ID. Replays find
their original receipt before resolving current focus, so switching projects
cannot redirect a previously admitted command. Same arguments with a different
call ID are separate operations; a later read does not reuse a stale read result.
Inputs and dependency names are checked before dispatch. Writes require editor
rights, sandbox admission and current quota measurements.

Returned results are persisted before testing quiescence. OCI observations also
inspect the immutable container's process list, rather than trusting process-local
locks after runner restart. VM observations verify the mounted generation, guest
epoch and guest process/cgroup state. Known results can settle on subsequent
project use without rerunning the command. A lost result or cancelled wait keeps
its reservation and blocks new work; it is not unlocked merely because no
process is visible. `project_status.tool_operation` exposes this state without
commands, private grants or sandbox paths. Background project execution currently
returns an explicit unsupported result instead of creating an unrelated sandbox.

PostgreSQL, RPC, registry and loop tests use fixture Docker/libvirt/QQ effects.
They do not prove native command cancellation, full helper-loss reconciliation
or actual QQ file delivery. Projects remain default-disabled.

This is not the complete M3 interface. Delete/preview tools,
project routing of browser tools, Sub-Agent baselines and accepted-version publication, shared
Matrix/iMessage ownership, and console management remain pending. Routine and
Sub-Agent contexts do not receive these management tools until their project
ownership is wired explicitly. No production enablement is implied.

### Project file delivery

`send_file_from_sandbox` and `send_image_from_sandbox` without `sandbox_id` read the current project
under its durable I/O grant. It does not activate a temporary task sandbox or
schedule project cleanup. The exact bytes are hashed into the private CAS;
`0050_project_files` atomically records their reference, Outbox entry and tool
receipt. The returned `queued` flag is not a claim of delivery. Inspect
`project_status.file_deliveries` for the latest five records; `committed` means
there is a transport receipt. Each record includes `media_kind` to distinguish
files from images. Explicit temporary sandbox IDs keep their old route.

The existing Outbox dispatches through the original bot, after rechecking live
membership, target scope, tool enablement, the read receipt and file checksum.
An operation-specific filename permits strict group-file reconciliation using
name, size, uploader, upload time and file ID. A successful upload API response
without a file receipt remains ambiguous. Private uploads also require a file
ID; an adapter returning only `{}` cannot be declared delivered, and private
receipt recovery remains an unverified platform limitation.

Images have a 10 MiB limit (or the lower configured transfer limit), and their
bytes must have a recognized PNG/JPEG/GIF/WebP/BMP signature. They are sent as
image message segments, not group-file uploads, including in private chats.
Their authoritative kind comes from the persisted host tool operation, not a
model-controlled delivery argument. A nonzero signed OneBot message ID confirms
the send. A lost or invalid response remains ambiguous; matching an old image
by content or the generic self-echo heuristic cannot prove this send occurred.
There is no blind retry. Such images retain their immutable copy and block
cleanup until the outcome is resolved; automatic image lost-ack reconciliation
is not claimed. Image recognition here is a format signature check, not a
pixel-decoding or content-quality guarantee.

The independent `project-files` supervised loop settles saved read receipts,
resumes claims that crashed before upload dispatch, and looks up uncertain group
uploads without resending. It does not block ordinary message delivery while
looking up group files. Uncertain uploads remain protected even if cancelled;
only a verified receipt or cancellation before upload releases the reference.
CAS cleanup uses its normal 24-hour grace and preserves version references.
Pending files block project lifecycle reclamation, and their unique bytes count
toward scope quota. Project status reports `delivery_bytes`; the Prometheus
`deliveries` byte category excludes blobs already counted under versions.

These paths have local PostgreSQL/RPC tests with fake QQ uploads, including
process-loss boundaries and immutable-byte checks. No live QQ delivery, native
Linux/KVM acceptance, or production enablement is implied. Sending a file here
verifies its bytes and transport, not its semantic correctness or PDF typography.

### Recoverable project rollback

`project_restore` accepts a required `version_handle` (for example `v2`) and an
optional `project_handle`. `/项目 回滚 v2` uses the current project;
`/项目 回滚 prj#3 v2` selects it explicitly. The caller must still be an editor
with current membership and sandbox admission. An active task, unfinished
automatic save, active/uncertain IO or unconfirmed delivery blocks rollback.
Replay resolves the original project and version before consulting current focus.

Migration `0053_project_restore` adds a durable lifecycle action, not an in-place
overwrite command. The host verifies the target manifest and every retained blob
checksum first. It then reserves the old workspace and saves its current retained
files, including unsaved edits, before removing its volume. Failed verification
or backup cannot authorize deletion. A new workspace generation receives the
target version through the existing empty-workspace, chunked, checksummed import.
This also supports stopped, hibernated and archived projects, and saved projects
that have never had a materialized workspace. Successful rollback wakes the project.

After the helper verifies the imported tree, one transaction publishes a new
version referencing the target's immutable objects. Old versions are not rewritten
or removed. The target version's frozen dependency recipe is then rebuilt, if
present; the current project's newer recipe is not substituted. Completion requires
the actual workspace to pass active-state verification. Only then do the lifecycle
journal and durable job complete together, and `project_status.restore.restored`
become true. Publication before a failed dependency rebuild is not completion.

Lost replies after volume deletion, version publication or dependency installation
resume through the persisted step receipts under a new worker lease. They do not
create another rollback version or rerun already-confirmed installs. Failed jobs
keep their reservation, recovery journal and retained versions; they are not
unlocked by a timeout. Schema downgrade refuses to discard rollback journals.
`/项目 状态` distinguishes queued/running, attention-needed and verified results.

Local tests inspect actual fixture workspace files, PostgreSQL records and CAS
objects through the product adapters. Kernel mount/libvirt and QQ effects remain
fixtures. This is not native OCI/KVM acceptance or production enablement.

### Portable browser core (project transport pending)

`src/browser_runtime.py` contains the existing browser engine without importing
NoneBot, the bot configuration or a database. The plugin renderer continues to
export its old browser class names. The optional host-supplied `bound_owner`
restricts a worker to one owner and uses `profile_root` as the actual Chromium
profile directory, suitable for a future `/workspace/.gaoji/browser` mount.
It does not itself resolve project membership or provide a sandbox boundary.

Pending/active page operations prevent idle eviction. Explicit close/clear waits
for the operation lock; uncertain close results retain the context and prevent
profile reuse or deletion until close is confirmed. Shutdown rejects new work.
These are in-process lifecycle protections, not durable project receipts.

Before launch, bounded non-following traversal checks the profile size (default
256 MiB), rejects foreign filesystem boundaries and unsafe files, and passes
32 MiB disk/media cache hints to Chromium. Service workers and downloads are
disabled. Neither the scan nor Chromium's cache hints constitute a hard quota;
the project quota volume and external process accounting remain necessary.

The shared retained-manifest policy now recognizes `Default/Cache`, `Default/Code
Cache` and `Default/GPUCache` beneath `.gaoji/browser`, in addition to the old
top-level cache paths. Whole browser profiles, `Default`, cookies and login files
are not rebuildable roots. Existing cleanup still requires a supported locked
dependency recipe; browser-only cache cleanup without a recipe is not yet wired.

A local real Chromium test checks persistent cookies and localStorage across
process restart, isolation between two profile directories, and explicit clearing.
It does not use personal browser data or a real online account. Session cookies
and website-side login expiration retain their normal semantics.

### Private project browser worker (launcher integration pending)

`src/sandbox_runtime/project_browser_worker.py` now runs the portable engine in a
separate process using inherited input/output pipes. It opens no control port.
The trusted launcher supplies a bounded bootstrap with instance, owner, project,
generation and fence; the profile is always `.gaoji/browser` beneath that
workspace. Bootstrap is not a model tool. Real workspace and metadata directory
checks reject symlink redirection; the launcher must additionally prove that the
workspace is the intended quota-backed mount and use the sandbox's unprivileged
identity. The worker is not itself that isolation/authorization boundary.

The wire format reuses the private runner's length-prefixed JSON framing, now
factored into `sandbox_runtime/wire.py`; existing runner imports remain valid.
Worker frames are capped at 256 KiB, reject duplicate JSON fields and non-finite
numbers, and accept only the nine existing browser methods. Requests cannot
supply JavaScript, shell commands, another profile path or owner.

Every worker incarnation generates a fresh ID. Host-owned sequence numbers must
advance by one; replaying the latest sequence with identical arguments returns
the saved reply without another click. Changed arguments, stale/skipped
sequences and another incarnation's requests are rejected. Only the latest
reply is held in memory: this is deliberately not cross-restart deduplication.
Once an action starts, cancellation, an exception or an oversized observation
leaves it `needs_attention` and prevents subsequent actions in that worker.
Exception text is not returned because it can contain entered credentials.

EOF, malformed frames, idle expiry (30 minutes) and operation timeout lead to
browser shutdown; failed or timed-out shutdown is a nonzero process exit. A
successful close response still does not prove the container/cgroup has no
remaining processes. The host must persist request/receipt ownership before
dispatch, reconcile uncertainty and independently observe quiescence before
snapshot, restore or reclamation. It must never relaunch an uncertain click in
a fresh worker merely because the pipe broke.

`project_browser_bundle.build_bundle()` produces a deterministic small zipapp of
the worker, browser engine, framing and error type only. It carries no Bot
configuration, database, API credentials or browser binaries. Playwright and
Chromium still have to be supplied by the selected sandbox runtime. A local
test actually runs this zipapp with installed Chromium through private pipes:
it writes form data, checks a visible click counter stays at one on replay,
closes via EOF, restarts and recovers persistent cookie/localStorage, then
checks a different project receives neither. It uses temporary loopback pages
and profiles, not personal browser data or online accounts.

Project `browser_*` routing, OCI/KVM launcher integration, application-side receipt
publication, physical browser draining across snapshot and restore, standalone
cache cleanup and native OCI/KVM acceptance remain open. This worker is not enabled in
production; ordinary browser tools retain their conversation owner until the
full integration is complete.

### Host browser receipts and maintenance exclusion

`project_browser_journal.py` persists launch and action intent before dispatch in
private host-owned files, outside the workspace. Each receipt freezes instance,
owner, project, generation, fence, session, worker and immutable container/domain
identity. Action arguments are not stored: only their digest, method and bounded
observation. Password inputs no longer expose their value in browser snapshots.
Other page observations may still be private and must not be treated as public
logs. Atomic replacement, file/directory fsync and per-session process locks
protect recovery; a timeout does not expire ownership.

`project_browser_host.py` separates observation from application publication.
Its idempotent `persist(session_id, sequence, response)` callback must commit to
the application's durable store before the host acknowledges the receipt.
Failure or lost acknowledgement repeats only publication, never the click.
A late reply may be read from the original pipe, but a missing/truncated pipe
does not authorize relaunch. An unconfirmed action remains blocked even after
its process is stopped. Actual application mapping and callback integration
remain to be implemented.

Normal drain refuses an unfinished launch/action. Explicit cancellation can
stop its processes but cannot manufacture a successful result. A live controller
holds the cross-process session lock through launch and each exchange; another
controller cannot race it with a false absence/close acknowledgement. The driver
must stop the frozen runtime and independently verify its processes are gone.
Worker stdout alone is never shutdown evidence. A closed session with an
unpublished receipt still blocks reuse and maintenance.

Native OCI, Desktop directory and VM disk stores now own a
`ProjectVolumeBrowserJournal`. Browser admission and the existing maintenance
intent share lock order `volume -> browser`; exact generation/fence, sandbox,
backend and (for VM) initialized domain binding are checked. Lifecycle
reservations, stop/remove, disk mutations and reserved file maintenance cannot
pass an active/unacknowledged browser. Workspace inspections include this
durable busy state even if no browser process has started or the control
process restarted. Guest file bundles remain independent of the host receipt
implementation and retain their own execution-gate checks.

This journal layer is independent of the native launcher below. Its local
Chromium test uses a temporary loopback page and real worker process, but its
OCI identity is explicitly a fixture; it does not prove container/KVM isolation.

### OCI project browser driver

`project_browser_oci.py` launches a dedicated helper container on the owning
Docker daemon. It shares the verified project's `/workspace` mount (native
bind or Desktop volume-subpath) and the instance's read-only Nix cache, not Bot
configuration, secrets or the Docker socket. Chromium and a Python environment
containing Playwright are prepared on demand from the existing pinned Nix
catalog. The source-only worker bundle is not taken from project files.
The bootstrap uses isolated Python mode and checks the workspace device/inode
before importing the worker or creating its profile.

The container uses UID 1000, read-only root, dropped capabilities,
no-new-privileges, the configured sandbox network, IPv6 disabled, no exposed
host ports, bounded tmpfs, at most 1 GiB RAM/one CPU/128 processes, and no Docker
logs of browser protocol data. Created/stopped browser containers reserve owner
and instance sandbox capacity until reconciled and removed; generic task
creation and restart also count them. They use the existing instance helper
inventory naming without masquerading as a second normal project container.

Private driver records freeze the container specification before `docker
create`, then its immutable ID before `docker start -a -i`. Each new effect
rechecks project generation/fence, parent container, both actual workspace
mounts, sidecar isolation and network policy. Saved result publication remains
possible after process loss without repeating the browser effect. Long inspect
JSON is bounded separately from user-facing text truncation.

Drain validates and stops only that immutable browser sidecar, observes its
stopped state and zero PID, records removal intent, removes it, then observes
absence. A lost removal acknowledgement can resume that original intent; an
absent unconfirmed creation cannot become proof of shutdown. Foreign/replaced
containers are not stopped. Session/allocation locks use the same order for
dispatch and drain. Desktop soft-quota enforcement can stop this independent
writer even while an action is blocked or its parent has already exited; the
unknown action receipt remains fenced for reconciliation.

The opt-in `tests/test_project_browser_oci_process.py` ran against an existing
local image with Python/Playwright/Chromium, `network=none`, and a temporary HTTP
fixture inside that same container. It exercised real pipes, browser clicks,
replay (visible count stayed one), retained profile and confirmed container
shutdown/removal. It deliberately uses a fixture parent identity and a bind
directory, not the product Nix dependency preparation or a mounted hard-quota
volume. Temporary containers, the test cache volume and profiles are removed.

Still required: native Nix dependency/loop-volume acceptance, the KVM launcher,
application session/sequence publication, project tool routing and automatic
lifecycle drain. The driver alone does not enable project browser tools in
production. Browser-only cache cleanup also remains open.

### Private browser RPC and two-phase publication

The existing authenticated Unix socket now exposes the following operations
when project workspaces are enabled. Every request includes `owner`,
`project_id`, `generation` and `fence`; the runner supplies its own instance ID.
It never accepts a workspace path, executable, mount, network-policy override,
JavaScript expression or replacement worker configuration from the caller.

| Operation | Additional identity/arguments | Meaning |
| --- | --- | --- |
| `project_browser_open` | `launch_id` equal to RPC request ID | Create one isolated session |
| `project_browser_status` | Exactly one of `launch_id`, `session_id` | Inspect the original bound session |
| `project_browser_execute` | `session_id`, `sequence`, `action`, `arguments` | Execute one of the nine existing browser actions |
| `project_browser_receive` | `session_id`, `sequence` | Recover that action's saved or late observation |
| `project_browser_ack` | `session_id`, `sequence`, `response_sha256` | Confirm application-side durable publication |
| `project_browser_drain` | `session_id`, optional `cancel_pending` | Stop and verify only the owned browser sidecar |

The application must commit the launch ID before opening, then persist the
session/sequence/action identity before executing. Opening uses the runner's
durable mutation receipt and non-expiring tombstone. A retry reuses its launch
ID even after restart; it cannot quietly allocate another session. The browser
journal records the launch ID before any process creation, so an interrupted
open can be inspected by that ID. An absent status is only "not observed", not
proof that a pending RPC cannot still launch. A cached open response describes
the original launch, not current liveness; check status separately.

Execution and recovery return an **unacknowledged** observation. After committing
the exact response to PostgreSQL, the application sends `ack` with the SHA-256
of `wire.encoded(response)`. Until then, the next action and workspace maintenance
are refused. These operations use the browser's own fenced journal rather than
the generic one-hour RPC result cache, so a late result remains recoverable.
Wrong owners, epochs, sequence reuse, changed arguments and altered hashes are
rejected. Capacity admission applies to new launches/effects, not replay, result
recovery, acknowledgement or shutdown.

Partial-frame timeouts/cancellation preserve the original pending read; recovery
does not restart in the middle of a JSON frame or resend an effect. Graceful
runner shutdown drains only browser launches owned by that runner process and
reaps its attach clients. It preserves unacknowledged/unknown results and leaves
foreign or unverified containers alone. Forced process death still needs
journal-based reconciliation; it is not an automatic acknowledgement. Launch,
execution and drain all take session locks before allocation locks, with live
workspace/parent/capacity rechecks immediately before creation.

`SandboxClient.project_browser()` is the typed operation wrapper, not the
application's durable publisher. PostgreSQL session mapping, tool selection and
turn-end lifecycle integration are still required before activating these
calls through ordinary project `browser_*` tools. KVM transport remains separate.

### Remaining acceptance

`tests/test_project_volumes.py` uses actual private files and Unix RPC but an
explicit fake host adapter. It covers owner/instance isolation, concurrent
create, stale/changed fences, identity replacement, crash points, cancellation,
busy consumers and default-disabled RPC. It does not prove a kernel mount.

`tools/verify_project_volume_image.py` additionally ran the product helper with
real ext4 utilities in a disposable local container, with no capabilities,
network, host mounts or Docker socket. A 64 MiB image grew to 96 MiB, shrank to
64 MiB, passed fsck, preserved a sentinel, resumed an interrupted growth to
80 MiB and was deleted. This validates regular-file utility behavior, not a
mounted quota or native server installation.

Before enabling this helper, M2 still needs:

- Native Ubuntu/NixOS acceptance of the reserved executor and complete lifecycle,
  including interruption during file operations and power-loss reconciliation.
  Earlier native checks already cover basic quota, mounts and reboot recovery.
- A host mount-namespace design that makes the mount visible to Docker without
  weakening the existing runner sandbox; systemd `PrivateTmp`/`ProtectSystem`
  must not accidentally make a successful mount private to the helper.
- Product-container binding/start and maintenance have journal/Unix RPC coverage
  for cancellation, lost acknowledgements, transfers, quota and identity checks.
  The base binding/reboot path has native evidence; the new cross-step
  reservation and delayed-execution composition still require native acceptance.
- PostgreSQL lifecycle jobs/fences, active-task/delivery protection, snapshot
  publication, generation journals and reserved OCI dispatch have local
  PostgreSQL/Unix RPC coverage. Offline retained-file snapshot/restore now has
  local coverage too, including locked classification, cleanup and reconstruction.
  Complete task admission, compensation and version retention need integration;
  the assembled reconstruction path still needs native acceptance. Do not clear
  a tombstone or reset a fence
  to work around a failed restore.
- Desktop Windows/engine-loss acceptance, VM project-disk/discard full native
  lifecycle acceptance, metrics and user-facing usage reporting.

The M0 experiments remain evidence for the storage design, not substitutes for
these product gates. M1 is complete locally; M2 through M7 are not complete.
