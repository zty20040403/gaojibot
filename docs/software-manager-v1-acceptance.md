# Software manager V1 acceptance

The full V1 goal is a beginner-facing server software manager with two curated
applications, Gaoji and a static website. Arbitrary repositories, a drive, photos
and notes are not supported applications yet. This checklist supersedes the
narrower deployment-platform phase-one checklist; checked foundations are not
proof that the complete product goal is finished.

- [x] Saved SSH connections, host identity checks and privilege detection.
- [x] Curated catalog, multiple isolated instances and legacy Gaoji migration.
- [x] Clean Ubuntu dependency installation and durable original task recovery.
- [x] Gaoji database, settings, model configuration and QR login entry points.
- [x] Status, start/stop, update, rollback and scoped uninstall for both apps.
- [x] Gaoji backup/restore and encrypted export/import implementation.
- [x] Website independent backup, restore, deletion and encrypted export/import.
- [x] Backup corruption, failed restore, replay and cross-instance isolation checks.
- [x] Isolated Ubuntu end-to-end backup/recovery and uninstall acceptance.
- [x] Updated UI, packaging, public release and updater verification.
- [x] Remove this iteration's temporary test resources and obsolete builds.
- [x] Final requirement-by-requirement audit with evidence and explicit limits.

No production SSH or deployment is authorized for this work. Use local temporary
fixtures and an isolated VM only. Real QQ group delivery is deferred by the user;
actual Windows runtime testing remains unperformed, not implicitly passed.

## Website backup contract

Server backups are immutable, instance-bound snapshots of validated static files,
independent of the two retained release versions. They include no credentials or
host paths. Creation/deletion/restoration reuse persistent revision-checked jobs.
Restoration verifies a candidate HTTP response before promotion. Export produces
a password-encrypted desktop file; import authenticates it first, stages only
static content, and requires a separate deployment confirmation. Import can target
a new website instance on a replacement server. Uninstall retains backups; purge
explicitly deletes this instance's releases and backups, never other applications.
Backups are manual in V1; no scheduled backup or offsite durability is implied.

## 0.1.36 local evidence

- 37 targeted Python checks passed for the website adapter, backups and shared
  local runtime configuration; public permissions are checked before replacement.
- 42 targeted Node checks passed for requests, immutable task dispatch, encrypted
  envelopes and backup transport (including real loopback SSH channels for the
  existing bot transfer). No production connections or account use.
- Actual Electron with fixture IPC passed import/export and password confirmation
  controls, restore selection surviving live polling, import without automatic
  deployment and 1280/390px layouts. Screenshots inspected. This is UI evidence,
  not a substitute for the isolated server acceptance below.
- A fresh marker-verified Ubuntu 24.04 x86_64 VM installed Docker and served a
  hash-verified website. Real backup creation, SSH streaming export, authenticated
  import into another instance, changed-content update, corrupt-backup rejection,
  same-job recovery, retained uninstall/export and isolated purge passed.
- The first run exposed an existing public-status permission window: rename
  published a private file before chmod made it readable. Publishing now sets
  permissions before the atomic replacement; the complete real sequence then
  passed again. Earlier phase-one reboot/coexistence evidence remains in
  `deployment-platform-v1.md`; it was not silently rerun or claimed here.
- Both Mac and Windows packages match source and runtime checksum
  `f663edaf51b14d9b0bdc825c78cb3fa7443f6975fdd04f99412253af6ac124c1`.
  Runtime receipts are preserved in `desktop/release/0.1.36/acceptance.json`.

## Requirements audit

| Requirement | Implementation / evidence |
| --- | --- |
| Enter a server and select software | SSH host-key confirmation, saved connection, catalog and instance list; Electron flow verified |
| Install dependencies on a clean server | Fixed privileged bootstrap and persistent systemd jobs; fresh isolated Ubuntu acceptance |
| Gaoji database and QQ components | Existing per-instance PostgreSQL/NapCat installation and setup; prior VM coexistence evidence |
| User-owned model and QQ | Private configuration, model enumeration/testing and QR entry point; no publisher account copied; real QQ intentionally deferred |
| Multiple applications without collisions | Instance-bound storage, containers, revisions and loopback ports; two real websites coexist and isolate deletion |
| Status, start/stop and updates | Read-only monitor, candidate HTTP verification, rollback and durable original-task recovery |
| Recoverable backups | Gaoji existing database transactions/encrypted transfer; website verified snapshots, encrypted export and independent import/restore |
| Reliable uninstall | Separate retention and purge, resource ownership checks, no shared-image/engine deletion; tested real website isolation |
| Beginner installation package | Mac arm64 and Windows x64 preview installers; final publication verification tracked below |

V1 deliberately means two curated applications. Drive/photo/notes adapters,
arbitrary GitHub installation, public-domain/TLS provisioning and scheduled
backups are future work, not features represented as delivered. Production
signing, actual Windows runtime and user-deferred QQ/account acceptance are not
certified by this preview release.

## Publication and cleanup

Published [App 0.1.36](https://github.com/zty20040403/gaoji-app/releases/tag/app-v0.1.36)
after successful assembly run `35444436709` and exact eight-asset SHA-256/size
verification. Public AppUpdater checks on Mac and Windows 0.1.35 return
`available:0.1.36`; Mac 0.1.36 returns `up_to_date`. Public DMG and EXE endpoints
return HTTP 200 with the expected lengths. No source push or production switch.

The marker-verified Ubuntu VM shut down normally with exit 0 and was removed.
Unpacked test applications, generated screenshots, an empty interrupted-test
directory and old 0.1.35 installers were removed. The four 0.1.36 installers and
small manifests/acceptance notes remain (about 588 MiB); prior release evidence
remains without its binaries. User App configuration and server data were not
deleted. All V1 items above are fulfilled within the stated preview scope; the
future adapters and deferred platform/account acceptance are not claimed complete.
