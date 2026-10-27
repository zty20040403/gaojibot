# Project volume helper checkpoint

M2 work in progress. This is a private helper primitive, not a public project
feature. `serve()` does not construct `ProjectVolumes`, so an ordinary installed
runner continues to reject every `project_volume_*` request. Desktop isolation,
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
  The later project driver must serialize container start/binding with volume
  maintenance. Returning a verified path alone does NOT close that race.

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
project-wide fence. The lifecycle journal now owns that allocator; its physical
backend adapters and worker registration remain unimplemented/default-disabled.

Native utilities run in a worker thread. Cancellation waits for the bounded
utility to finish before releasing the runner lock. It is not rollback and does
not report cancellation as proof that no filesystem change occurred.

## Recovery and destructive operations

- Create writes intent before creating/formatting. A known completed format is
  checked, not formatted again, when the helper resumes.
- Mount can resume after loop attachment, mount or ownership initialization.
  A new mount never hides unexpected files in the underlying directory.
- Online expansion extends the file, refreshes loop capacity, resizes ext4 and
  verifies actual size before committing the new quota.
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

This is **not a physical executor**. `verified` evidence is accepted only from
trusted future adapters, never a model, tool input or HTTP request. The tests
use explicitly labelled fixture observations. Native observations, command
quiescence and atomic coordination of task attachment/container start with
maintenance are mandatory before enabling a worker. The journal does not yet
prove those operations succeed on a real host. Generic queue retries alone
must not be mistaken for a complete compensator.

Remaining lifecycle integration includes physical adapters, automatic idle
scheduling, rebuild/lockfile rules, retention sampling, trash/purge, and recovery
UI. In particular, hibernated snapshots need a read-only export path without
starting an application writer. Do not register `project.lifecycle` as a worker
kind until these adapters honor every persisted step and independently verify
the resulting state.

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

- Native Ubuntu/NixOS product-helper checks, including actual loop metadata,
  simultaneous writers, reboot and power-loss reconciliation.
- A host mount-namespace design that makes the mount visible to Docker without
  weakening the existing runner sandbox; systemd `PrivateTmp`/`ProtectSystem`
  must not accidentally make a successful mount private to the helper.
- Atomic project-container binding/start and maintenance coordination.
- PostgreSQL lifecycle jobs/fences, active-task/delivery protection, snapshots,
  and incarnation journaling now have local PostgreSQL/Unix RPC coverage.
  Their concrete backend execution, cross-process admission coordination,
  rebuildable-data cleanup, compensation and version retention still need
  integration and native acceptance. Do not clear a tombstone or reset a fence
  to work around a failed restore.
- Desktop soft-quota monitoring and cancellation, plus VM project-disk/discard
  integration, metrics and user-facing usage reporting.

The M0 experiments remain evidence for the storage design, not substitutes for
these product gates. M1 is complete locally; M2 through M7 are not complete.
