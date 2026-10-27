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
  -> retained-file maintenance / snapshot (adapter still pending)
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
the private VM project adapter in production or complete VM version IO.

## Evidence and remaining gates

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
- Desktop soft-quota monitoring and cancellation, plus VM project-disk/discard
  integration, metrics and user-facing usage reporting.

The M0 experiments remain evidence for the storage design, not substitutes for
these product gates. M1 is complete locally; M2 through M7 are not complete.
