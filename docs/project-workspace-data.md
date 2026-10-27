# Project workspace data contract

Milestone M1, local implementation. This document describes persistence, not an
enabled workspace product. The approved M0-M7 plan remains a separate input.
The base schema is `0043_project_workspace`; the current M2 head is
`0045_project_file_staging`, following `0044_project_lifecycle`.

## Boundaries

- `ProjectStore` owns project metadata, scope limits, grants, current-project
  focus, version manifests, blob references and project audit events.
- `ProjectBlobStore` owns a private, service-controlled content store. It is not
  a writable sandbox directory and must never be mounted into a user workspace.
- The existing `RoutineStore` owns both legacy cron definitions and new pending
  schedule/watch definitions. Its durable queue, budget reservations, takeover
  receipts and Outbox remain the only scheduling foundation.
- `WorkbenchAuthStore` owns independent member login codes and sessions. It does
  not grant administrator access and does not reuse administrator cookies.
- All stores are synchronous. Async handlers must use the existing thread-offload
  boundary rather than execute database/file operations on the event loop.

## Identity and access

New project ownership, grants, version authors, audit actors and workbench
sessions reference ledger principals, not QQ numbers. Existing routine native
address fields remain for backwards-compatible delivery; migration backfills the
new principal reference when a corresponding identity already exists. New
definitions resolve/create their principal through the ledger identity API,
without manufacturing a chat message.

`ProjectAccess` is constructed by trusted host code after checking live
conversation membership and administrator status. Its flags are never tool
arguments or user-supplied HTTP fields. The store verifies the actor's platform
identity against the principal, and every lookup includes the conversation
scope. An administrator flag cannot bypass another group's scope or another
person's private identity. A viewer can read a group project; an editor can
modify it; only its owner or a scoped administrator can change grants or prune
versions. Quota changes and project pinning require administrator status.

The data layer does not contact QQ to verify membership. M3/M5 must perform the
live check at their entry points, including after a user leaves a group. A valid
workbench session establishes identity and scope only; it is not proof of current
group membership.

## Revisions and concurrency

There are two different version counters:

- `projects.version`: optimistic revision for edits to project metadata, grants
  and version history. Stale writers receive `ProjectConflict`.
- `project_versions.version_no`: immutable content history. Restoring v2 makes a
  new version with v2's content, rather than overwriting history.

Scope advisory locks serialize project-count checks and retained-data accounting
across workers. Project row locks and expected revisions prevent lost updates.
A database-wide blob advisory lock serializes object publication and collection;
all processes sharing the object root must use the same database/schema and
lock protocol. This intentionally conservative M1 implementation holds that lock
during bounded file copies. It is not a high-throughput distributed object store.

Group defaults are five projects and 5 GiB per scope; private defaults are three
projects and 2 GiB. A project defaults to 2 GiB. The limits are not reservations.
Scope usage includes sampled workspace usage plus each project's distinct blob
references, including manifests and pending snapshot files. Staging and retained
versions referencing the same project/blob are counted once. Two projects sharing a blob are charged twice
at scope level, while the object is physically stored once. M2 still needs to
wire workspace sampling, physical instance admission and hard quota volumes.
The `soft` database default must not be presented as an implemented quota monitor.

Current-project focus is per principal and scope, expires after 24 hours without
use, and is not renewed by UI polling. Opening a project or writing its version
renews the relevant user's activity, not every member's focus.

## Version publication and recovery

1. Check scope, editor permission, task lock and expected revision.
2. Stream files into private staging, bound each file to 256 MiB and each version
   to 20,000 files. Reject traversal, symlinks at the object boundary, unsafe
   modes, case-fold collisions and file/directory prefix conflicts.
3. Hash and publish each object by atomic hard link. Fsync file contents and
   directory entries; an existing object must pass checksum and size verification.
4. Publish a canonical, sorted manifest and all references in one PostgreSQL
   transaction. Check scope quota before commit.
5. Increment the project revision and record the version audit event.

Unchanged content returns the existing latest version. Supplying a new label
updates that version's metadata with a revision check and audit event. Explicit
restore always creates a new history entry, even for identical content.

Format-2 manifests also retain a rebuild specification: the exact recipe,
required input hashes and excluded directory roots. Required inputs must be
present in retained files at those hashes; excluded roots cannot overlap retained
files or protected project metadata. The specification participates in the
manifest digest and is preserved by version rollback. Format-1 manifests remain
supported without changes to their canonical representation. No schema migration
is needed because the specification resides in the immutable manifest object.

The private OCI adapter currently classifies locked npm/uv/cargo recipes and
can clean only their observed rebuildable roots after version publication.
Missing locks or unsupported commands preserve dependency files and emit a
warning, not permission to delete. Reconstruction is a separate, still-pending
adapter; a wake needing it is refused before starting any physical operation.
See [the helper contract](project-volume-helper.md#locked-classification-and-cleanup).

A failed transaction may leave unreferenced files, never a partially published
version. Collection waits at least 24 hours after the last reference disappears;
filesystem-only orphans and interrupted staging uploads also get that grace
period. Re-referencing an object cancels its collection eligibility. Collection
is bounded and runs under the same database lock as publication. It never accepts
a model-provided path or digest for arbitrary deletion. Shared, current,
labelled, pinned, preview-referenced and task-baseline versions remain protected.
M2 is responsible for selecting old versions according to the retention policy.

Version source task/turn IDs must belong to the same conversation. M3 must add
the accepted-artifact and task/project binding checks before its pipeline can
publish a task result; a same-scope ID by itself is not acceptance evidence.

### Leased offline snapshots

Migration 0045 adds `project_snapshot_staging`, keyed by job and canonical path,
with project, manifest digest, blob digest, size and mode. It is private host
state, not a tool/HTTP write interface. A matching pending snapshot step, live
membership and current job lease are required before and after streaming.

Streaming holds scope/blob locks but not the queue job row; heartbeats continue.
Completed objects survive lease takeover and are verified before reuse. A final
uncached helper tree check precedes publication. The store then verifies private
object metadata and the full manifest identity, atomically publishes the version
and removes that job's staging references. No bulk rehash runs while the job row
is locked. A missing object or lost lease aborts publication.

GC protects all staged objects. Staging for terminal jobs becomes collectible
only after the 24-hour grace period; active jobs are not expired just because a
file was staged yesterday. Downgrade refuses remaining staging data. This is
local lifecycle plumbing, not a complete automatic retention/cleanup worker.

## Routines

`create_pending` persists a disabled draft and, for watches, its checkpoint row.
Only the creator can confirm the exact revision. Project-bound drafts additionally
require editor permission both at creation and confirmation. Cancelling a draft
prevents later confirmation or reactivation through the legacy setter.

The existing dispatcher only materializes active schedule definitions. Watch
definitions are not accidentally treated as cron Agent runs. Old and new creation
paths share conversation limits and locks. The legacy setter refuses project,
watch, pending and structured definitions that need the newer confirmation/update
flow. Existing ordinary cron occurrences keep their once-only execution/delivery
identities.

M4 still owns public confirmation commands, source validation, watch polling,
minimum intervals, failure auto-pause and project-aware dispatch. These are not
enabled merely by creating the M1 tables.

## Member login credentials

- Login codes contain 32 random bytes, expire after ten minutes and are consumed
  with an atomic update. Only one concurrent consumer can create a session.
- A new code revokes the previous unused code for the same principal/scope.
- Sessions last seven days and have a separate CSRF secret. Database rows store
  domain-separated SHA-256 hashes, not plaintext credentials; object reprs hide
  the returned secrets.
- Principal-wide revocation and code redemption share a lock, so revocation
  cannot miss a session being created concurrently.
- M5 must enforce Origin, required CSRF on writes, live membership, secure cookie
  attributes and private delivery of login links. The store does not expose an
  HTTP endpoint or turn off existing administrator authentication.

## Migration and operational scope

The migration preserves legacy routines and adds nullable project/baseline
columns to Sub-Agent tasks. A clean downgrade is reversible. Downgrade refuses
retained project, workbench or new routine state rather than silently dropping
user data. Migration is operator-controlled; constructors never migrate schemas.

M1 does not enable project tools, resize disks, start a worker, publish previews,
open network ports or modify the Desktop App. Deployment and live QQ/provider
acceptance are outside this local milestone. Test evidence and the M0-M7
checklist are recorded in `improvement-roadmap-progress.md`.
