# Gaoji tank cutover

This records the production cutover performed on 2026-09-26 (HKT) and the
remaining acceptance gates. The bot runs on tank; the only QQ/NapCat process
remains on h610. Tank is the PostgreSQL primary and h610 is its secondary.
Tank uses the KVM sandbox backend, not Podman. The active Nix revision at
cutover was `d9df721`, with bot revision `4149bc5`.

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

## Preparation procedure for a future cutover

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

## Rollback

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
