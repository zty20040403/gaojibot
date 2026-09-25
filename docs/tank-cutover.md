# Gaoji tank cutover

This is a gated production runbook, not a claim that the migration has run.
The current bot and NapCat remain on h610. Tank is a PostgreSQL secondary and
the staged tank bot is disabled. Keep the existing OCI sandbox for the first
cutover; enable the KVM backend only after the bot, database and file-delivery
path have passed acceptance.

## Prepare without interrupting service

1. Fetch both Git origins and compare with local branches before merging. Do
   not rebuild either shared host from an older nix-config revision. Pin the
   bot input to the merged commit and evaluate h610 and tank configurations.
2. Keep one active QQ login. NapCat stays on h610; only the bot process moves.
   Neither host may run a second bot consumer during the switch.
3. Provision the tank runtime environment, admin authorization key and fleet
   token through root-controlled files or re-encrypted SOPS secrets. The
   existing `account-auth.yaml` and `control.yaml` are not encrypted for the
   tank host; editing `.sops.yaml` alone does not update those ciphertexts.
   Never print their values in logs, chat or command arguments. Check owner,
   mode and presence only.
4. Prepare `/data/services/gaoji/media` and the archive directory on tank.
   Warm-copy h610's bot state and media without treating the live copy as
   authoritative. Record counts, sizes and hashes, and confirm the bot user
   can write the final paths.
5. Confirm the tank PostgreSQL node is healthy and caught up with h610. Check
   current primary, replication lag, latest successful backup and restore
   check. Do not switch roles while replication is behind or backup is bad.

## Maintenance window

1. Stop **only** the h610 `gaoji.service`. Leave h610 NapCat, PostgreSQL,
   Max and other users' services untouched. Record the last processed QQ
   message and unfinished job/delivery IDs.
2. Run the final state/media delta copy from h610 to tank. Recheck file counts,
   hashes and permissions. Do not copy the PostgreSQL data directory with
   rsync; PostgreSQL already has its own replication mechanism.
3. Change the live pg_auto_failover candidate priorities (tank 100, h610 50),
   perform one controlled switchover, and verify **tank is read-write primary**
   while h610 is a healthy secondary. Nix option values alone do not change
   an already registered node's live priority. Bot DSN must list tank first,
   h610 second, with `target_session_attrs=read-write`.
4. Prepare one cutover revision: tank bot enabled with its QQ transport
   disabled, h610 bot disabled (`runBot = false`) while NapCat stays enabled,
   and h610 NapCat WebSocket/admin proxy pointed at tank's Tailscale address.
   **Rebuild h610 first, then tank.** The manually stopped h610 bot must not
   restart during its rebuild. Tank starts only after that is verified. A
   short gap in replies is preferable to two bot consumers. Check that only
   one bot process and one OneBot connection exist.
5. Verify a group reply, a file/PDF delivery receipt, unfinished task replay,
   admin login, model request, metrics and database writes. Confirm the
   delivery queue is not dropping or duplicating messages. Check tank storage
   and PostgreSQL replication again after real traffic.

## Rollback

If tank bot startup or QQ delivery fails, stop tank bot before restarting
h610 bot. Restore NapCat's h610 WebSocket endpoint and the admin proxy. Do not
flip PostgreSQL back blindly: inspect which node has accepted writes and
ensure the would-be new primary has caught up before a second switchover.
Preserve tank state, failed deliveries and logs for reconciliation; never run
both bot instances against one QQ session. Changes to shared services must be
scoped to Gaoji and PostgreSQL's planned role change.

The optional KVM sandbox is a separate rollout. Its VM disks belong on
`/data/services/gaoji/vms`, not the tank system partition. Before enabling it
for QQ tasks, validate libvirt from the actual systemd service identity and
repeat file-delivery acceptance with one disposable VM.
