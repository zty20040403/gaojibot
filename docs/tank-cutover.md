# Gaoji tank cutover

This records the staged production cutover on 2026-09-26/27 (HKT) and the
remaining acceptance gates. Bot, QQ/NapCat, primary PostgreSQL, media and KVM
sandboxes now run on tank. h610 remains the PostgreSQL secondary and monitor;
shared ingress, model services and cluster control also remain external
dependencies on h610. The initial cutover below kept QQ on h610; the final
QQ move is recorded first to distinguish current placement from that history.

## QQ transport cutover on 2026-09-27

Nix revision `6d3a8ac` moves the sole Gaoji QQ transport to tank without a
client upgrade: QQ `3.2.29-2026-05-28`, NapCat `4.18.19`, same pinned native
package and bubblewrap isolation. Bot code remains `3296d82`.

- Stopped h610's Gaoji QQ service and both its login/health timers before
  copying. Rebuilt h610 first; those three units are now absent, so an ordinary
  rebuild from the current configuration cannot resurrect the old login.
- Copied `QQ`, `config` and `outbox` into tank's `/var/lib/napcat-gaoji`:
  2,129 regular files, 1,418,227,663 bytes. A checksum rsync dry run found no
  differences before tank started. The source data was not deleted.
- Transferred the OneBot credential and password privately with root-only
  permissions; compared the OneBot credential with the live tank bot in memory.
  The persisted login-attempt/recovery state was copied without resetting its
  limits. No credential values were printed or committed.
- Rebuilt tank and verified its bot and native QQ services running, its two
  timers active, and no failed units. WebUI listens only on `127.0.0.1:6100`;
  the local SSH forward `127.0.0.1:16100` now goes to `kenneth@tank`.
- QQ initially required the owner's phone confirmation. At 00:19 HKT the
  tank client became online: `CheckLoginStatus` reported `isLogin=true` and
  `isOffline=false`, and OneBot `get_status` reported `online=true` and
  `good=true`. A fresh check after the group reply confirmed it stayed online.
- The owner's real group message `1906185096` (group 611798505, 00:19:30 HKT)
  produced successful turn 1738 and committed delivery 3781 with one attempt.
  OneBot `get_msg` independently retrieved QQ reply `899215867`, including
  the reference to that original message. This proves group reception, a
  model reply, the database write and QQ delivery through tank's own client.
  It does not prove a new file task or running-service crash recovery.

The h610 rollback copy `/var/lib/napcat-chat-bot` is root-owned and mode 0700
at its root after its service account was removed. For a deliberate rollback,
stop tank QQ and its timers first, restore the h610 declaration, and restore
ownership to the declared h610 QQ service user before starting it. Never run
both copies. Max's independent QQ reminder credential was not copied to tank;
that cross-account password-login notification is disabled. The existing
Prometheus QQ health checks remain available from tank, while h610's obsolete
textfile is removed by its tmpfiles configuration.

Deployed systems after this switch:

- h610: `/nix/store/pgbr6sn7774na3z5yh7iips9vzzzd59w-nixos-system-h610-26.05.20260911.21a67dc`
- tank: `/nix/store/69mwzxj2gpi47yirwrn05cb7aqykfa5r-nixos-system-tank-26.05.20260911.21a67dc`

## Initial bot cutover

At the initial cutover, tank was the PostgreSQL primary and h610 its secondary;
the later network incident below changed those live roles before controlled
recovery. Tank uses the KVM sandbox backend, not Podman. The initial Nix
revision was `d9df721`, with bot revision `4149bc5`.

## Verified after cutover

- h610 has no active `gaoji.service`; tank has one active bot process. The
  OneBot reverse WebSocket connected from h610 to tank, and tank received QQ
  notice events after the user reauthenticated QQ.
- Direct SQL and the pg_auto_failover monitor agreed: tank was writable
  primary with priority 100; h610 was healthy secondary with priority 50.
  Both nodes loaded HBA rules allowing the tank bot client, and PostgreSQL
  reported no HBA parse errors.
- Tank's bot started its PostgreSQL and durable workers. The admin endpoint
  and `/metrics` returned HTTP 200; the media, archive and VM roots were
  writable by the bot user.
- A disposable KVM guest was created, executed `id && pwd` as its unprivileged
  sandbox user, and was destroyed with no remaining test domain or directory.
- A real group message in group 611798505 reached the tank bot through h610
  NapCat. The bot answered that its process runs on tank; turn 1717 succeeded,
  and delivery 3752 was committed once with QQ message ID 2032948770.
- A second disposable tank KVM guest generated a PDF with the Chinese title
  `tank 迁移验收`. The host read back 2,412 bytes with a PDF header and EOF
  marker, then destroyed the guest and verified its domain and directory were
  gone. This checks sandbox artifact creation, not QQ file delivery.
- On tank's PostgreSQL 17 HA node, the two `test_file_outbox_process_loss`
  tests passed in a separate disposable database. They cover recovery after
  database unavailability and process death at each file-delivery boundary,
  with a simulated QQ receipt. The test database and temporary files were
  removed afterward; this does not replace a live QQ receipt.

## Live PDF delivery on 2026-09-26

Group 611798505 submitted task#175 to the tank bot. A tank KVM sandbox
generated `tank_migration_acceptance.pdf` (one A4 page, 111,021 bytes). An
independent sandbox imported the immutable snapshot and checked the page count,
Chinese title extraction, embedded Noto Sans CJK fonts, PDF structure, rendered
page and OCR. All four acceptance criteria and the artifact review passed.
The durable file outbox recorded one acknowledged QQ group-file delivery:
`kb-175-r1-5266d170f4-tank_migration_acceptance.pdf`, file ID
`32516537cdca40f0ad53703b96305e91`, with no retry. This proves the live
KVM-to-QQ file path for that task; it does not prove process-loss recovery.

The historical task remains `partial` because the verifier's scratch
`review.pdf` was incorrectly auto-recovered as a new deliverable. Its final
text also reused a pre-delivery draft. Bot revisions `2756609` and `87d704c`
removed those paths for future file tasks and passed focused regression tests;
the old task and QQ file receipt were not rewritten or replayed. A new live
file task is still needed to confirm the corrected final status and wording.
Running-service process-loss recovery and no-duplicate delivery after a real
restart remain separate acceptance gates. Isolated recovery tests do not
establish either gate in production.

On 2026-09-26 at 19:25 HKT, the configured restore-check unit restored the
newest `qq_bot-20260925T195159Z.dump` into its own network-disabled temporary
PostgreSQL instance. It verified schema revision `0029_native_ssh_operations`,
93 business tables and 29,725 messages, then removed the temporary instance.
The tank bot and primary database remained active, with h610 still replicating.
This proves that backup was restorable at that time, independently of the
earlier archive-read check.

## Network interruption and empty-process startup on 2026-09-26

Around 19:57 HKT, tank lost reliable connectivity to the monitor on h610.
The monitor promoted h610; tank rewound and rejoined as a read-only node but
remained in `catchingup`. Do not force tank writable while the monitor or
replication is unavailable. At 20:45, h610 was still `wait_primary` and tank
was still catching up. The migrated topology was therefore not healthy.

Short probes at 20:41-20:45 observed 50-58% loss between their Tailscale IPs,
70% loss to h610's public IPv4 and 90% to one of its public IPv6 addresses.
Tank's local gateway and each host's independent probe to 223.5.5.5 had no
loss in ten-packet samples. Tank to h310 had 90% loss, while h610 to h310 had
none. Tank's router also lost six of eight probes to h610's public IPv4.
These are samples, not proof of an ISP root cause, but they show that the
failure is not confined to PostgreSQL or the Tailscale tunnel. NFS also
logged timeouts. No database promotion was forced during diagnosis.

The outage exposed a separate application bug: at 20:39, `ai_chat` failed
to import after a database transaction timed out. NoneBot caught the import
exception and the entrypoint still started Uvicorn. Systemd reported
`active`, OneBot accepted connections, but `/metrics` returned 404 because
the core plugin was absent. The entrypoint now exits unsuccessfully if the
required plugin cannot load, allowing systemd to retry rather than leaving
an empty server running.

The shared database pool now supplies connection defaults for a three-second
connection attempt, TCP keepalives (idle 10s, interval 5s, count 3), and
`tcp_user_timeout=15000`. Explicit DSN values take precedence. The last
option bounds unacknowledged transmitted data on supported platforms; it is
not a 15-second SQL execution deadline. These settings help detect broken
sockets and do not repair a lossy route or prove database recovery.

## Recovery and controlled return to tank on 2026-09-26

At 23:39-23:44 HKT, both nodes reported healthy, h610 was primary and tank
was a streaming secondary with zero measured replay lag. A fresh 25-packet
probe from tank to h610 had no packet loss (10-15 ms RTT). This establishes
recovery during the observation window, not a diagnosis or permanent fix of
the earlier network failure.

Before switching, tank created `qq_bot-20260926T154131Z.dump` successfully
(218,321,338 bytes). There were no running subagent tasks. Only the tank
`gaoji.service` was stopped; h610's QQ client and other services were left
running. The monitor's `qq-bot-postgres-prefer-tank` command completed the
controlled switchover at 23:45:41. Direct SQL and the monitor agreed that
tank was the read-write primary and h610 the read-only secondary on timeline
16, with matching LSNs. The CLI emitted a `get_nodes` display-query error
before the state notifications; it did not prevent the switchover, whose
outcome was verified independently rather than inferred from its exit code.

At 23:46, tank restarted the deployed bot revision `3296d82`: the required
plugin loaded, startup completed, and OneBot connected. The bot process's
PostgreSQL sockets connected to tank (`100.64.0.4:55432`). Its metrics and
the HTTPS admin page both returned HTTP 200. The h610 bot unit remains
absent, preventing a second consumer. Both hosts had no failed systemd units.

At 23:47, the isolated restore check successfully restored the new backup:
schema `0029_native_ssh_operations`, 93 business tables, 30,172 messages.
Its temporary restore instance was cleaned up by the existing check.

QQ account authentication was still outstanding at this database recovery:
the health probe reported `login_required`, and the live WebUI API returned
`isLogin=false` with a QR login URL. The reverse WebSocket connection was
not evidence of an online QQ account. The later complete QQ transport move
and real group reply are recorded at the top of this document. Corrected
live file-task reports and running-service process-loss delivery acceptance
remain separate unverified gates. No old task was replayed or marked
delivered during this recovery. No NixOS rebuild or new application revision
was needed for this database role switch; both repositories were fetched
and had no newer upstream commits before the operation.

## Initial bot-only cutover procedure (historical)

These steps describe the earlier bot-only move. They intentionally kept QQ
on h610 and must not be used as the current QQ placement or rollback plan.

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
   Both hosts publish a non-secret summary at
   `/run/qq-bot-postgres-health/health.json`; require fresh `healthy` results
   with h610 `primary` and tank `secondary` before the switch. This file does
   **not** measure WAL lag. Query both database nodes for receive/replay LSN
   and verify catch-up separately. Check the latest successful
   `qq-bot-postgres-backup.service` and
   `qq-bot-postgres-restore-check.service` runs on tank, plus the actual
   restore-check marker under `/data/backup/postgresql/qq-bot-ha`. A green
   systemd unit alone is not evidence that the newest backup was restored.

## Cutover procedure (performed)

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
   Re-read both health summaries and query the database nodes directly after
   the switchover; do not infer success from `systemctl is-active`.
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
   and PostgreSQL replication again after real traffic. The group reply and
   first live PDF receipt have been observed; the remaining gates are listed
   above and must not be inferred from that one successful upload.

## Bot-only rollback (historical)

If tank bot startup or QQ delivery fails, stop tank bot before restoring the
older h610 configuration that contains `gaoji.service`; the current h610
generation deliberately omits that unit, so `systemctl start gaoji` alone
cannot restore it. Restore NapCat's h610 WebSocket endpoint and the admin
proxy. Do not flip PostgreSQL back blindly: inspect which node has accepted
writes and ensure the would-be new primary has caught up before a second
switchover.
Preserve tank state, failed deliveries and logs for reconciliation; never run
both bot instances against one QQ session. Changes to shared services must be
scoped to Gaoji and PostgreSQL's planned role change.

KVM is enabled on tank and its VM disks belong on `/data/services/gaoji/vms`,
not the tank system partition. Creation, execution and destruction were
verified as the bot's Unix user, but QQ file-delivery acceptance through the
running service was only established by the later task#175, not by this
Unix-user smoke test.
