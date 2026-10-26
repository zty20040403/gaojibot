from __future__ import annotations

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from src.local_runtime.backups import BackupError, BackupStore, verify_archive
from src.local_runtime.files import read_json, write_json
from src.local_runtime.instances import InstanceStore
from src.local_runtime.maintenance import Maintenance, write_bytes
from src.local_runtime.manager import Controller
from src.local_runtime.setup_clients import SetupError
from src.local_runtime.runtime import compose_document, runtime_environment


CHECKSUM = "a" * 64
IMAGE = "gaoji-app-runtime:" + "b" * 64
DEPENDENCY = "example.invalid/image@sha256:" + "c" * 64


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = InstanceStore(self.root / "data")
        config = self.store.create("Backup fixture")
        settings = config["settings"]
        settings.update(bot_qq="12345678", admin_qq="87654321", models=[{"id": "mine", "model": "test", "base_url": "https://test.invalid/v1"}], default_model="mine")
        self.config = self.store.update(config["id"], settings, expected_revision=1, model_keys={"mine": "private-test-model-key"})
        self.identifier = config["id"]
        self.directory = self.store.directory(self.identifier)
        config, private = self.store.snapshot(self.identifier)
        self.private = private
        (self.directory / "napcat/QQ").mkdir()
        (self.directory / "napcat/config").mkdir()
        for name, content in (("state/notes.txt", "群聊记忆".encode()), ("files/report.pdf", b"fixture-pdf"),
                ("napcat/QQ/session", b"private-qq-session"), ("napcat/config/webui.json", b"{}"),
                ("config/admin.secret", secrets.token_bytes(32)), ("config/postgres.password", private["database_password"].encode()),
                ("database/original", b"original database")):
            write_bytes(self.directory / name, content)
        write_json(self.directory / "config/runtime.json", {"schema": 1, "environment": runtime_environment(config, private)})
        write_json(self.directory / "compose.json", compose_document(config, self.directory, runtime_image=IMAGE,
            images={"postgres": DEPENDENCY, "napcat": DEPENDENCY}, bot_enabled=True))
        self.dump = self.root / "pg.dump"
        self.dump.write_bytes(b"PGDMP-not-a-real-database-unit-fixture")
        self.backups = BackupStore(self.directory)
        self.original_secret = (self.directory / "config/admin.secret").read_bytes()

    def publish(self, identifier="1" * 32):
        return self.backups.publish(identifier, self.config, self.private, self.dump, CHECKSUM, database_bytes=4096)


class ArchiveTests(Fixture):
    def test_roundtrip_preserves_data_keys_and_redacts_listing(self):
        (self.directory / "assets/ggml-base.bin").write_bytes(b"reproducible-public-model")
        record = self.publish()
        archive = self.backups.path(record["id"]) / "snapshot.tar"
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        result = verify_archive(archive, expected_checksum=CHECKSUM, max_bytes=record["bytes"], destination=self.root / "extracted")
        self.assertEqual((self.root / "extracted/state/notes.txt").read_text(), "群聊记忆")
        self.assertEqual(result["snapshot"]["private"]["model_key_sets"]["2"], {"mine": "private-test-model-key"})
        self.assertNotIn("private-test-model-key", json.dumps(self.backups.list()))
        self.assertNotIn("private-qq-session", json.dumps(self.backups.list()))
        self.assertFalse((self.root / "extracted/assets").exists())
        self.assertEqual(self.publish(), record)

    def test_corruption_wrong_instance_version_and_space_fail_closed(self):
        record = self.publish()
        archive = self.backups.path(record["id"]) / "snapshot.tar"
        with self.assertRaises(BackupError):
            self.backups.check(record["id"], "f" * 64)
        with self.assertRaises(BackupError):
            verify_archive(archive, expected_checksum=CHECKSUM, max_bytes=32, destination=self.root / "small")
        self.assertFalse((self.root / "small").exists())
        with archive.open("r+b") as stream:
            stream.seek(2000); stream.write(b"tampered")
        with self.assertRaisesRegex(BackupError, "摘要"):
            self.backups.check(record["id"], CHECKSUM)
        for identifier in ("../outside", "", "a" * 31):
            with self.assertRaises(BackupError):
                self.backups.path(identifier)

    def test_symlinks_do_not_read_external_files(self):
        outside = self.root / "private"
        outside.write_text("do not read")
        (self.directory / "state/leak").symlink_to(outside)
        with self.assertRaises(BackupError):
            self.publish()
        self.assertEqual(self.backups.list(), [])
        self.assertFalse(list(self.backups.root.rglob("*.partial")))

    def test_malicious_archive_members_are_never_extracted(self):
        for index, (name, kind) in enumerate((("../outside", tarfile.REGTYPE), ("/outside", tarfile.REGTYPE),
                ("state/link", tarfile.SYMTYPE), ("state/device", tarfile.CHRTYPE), ("config/control.json", tarfile.REGTYPE))):
            archive = self.root / f"bad-{index}.tar"
            with tarfile.open(archive, "w") as tar:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "../../outside"
                member.size = 0
                tar.addfile(member, io.BytesIO())
            destination = self.root / f"bad-{index}"
            with self.assertRaises(BackupError):
                verify_archive(archive, expected_checksum=CHECKSUM, max_bytes=10000, destination=destination)
            self.assertFalse(destination.exists())
        self.assertFalse((self.root.parent / "outside").exists())


class FakeController(Controller):
    """Exercises the real store/journal/commit code; Docker commands are recorded."""
    def __init__(self, job, root):
        super().__init__(job, root)
        self.running = {"bot", "database", "napcat"}
        self.calls = []
        # Keep real instance accounting and admission, replacing only Docker's
        # inventory with an isolated local filesystem in these unit fixtures.
        engine = root / "fixture-engine"
        engine.mkdir(exist_ok=True)
        self.capacity.engine_inventory = lambda: (engine, [], 0)

    def services(self):
        return {"services": [{"name": name, "state": "running"} for name in self.running]}

    def compose(self, arguments, **_kwargs):
        self.calls.append(arguments)
        names = set(arguments) & {"bot", "database", "napcat"}
        if arguments[0] == "stop":
            self.running -= names
        elif arguments[0] == "up":
            self.running |= names
        return ""


class FakeMaintenance(Maintenance):
    def command(self, arguments, *, compose=None, source=None, output=None, **_kwargs):
        self.c.calls.append(arguments)
        if "psql" in arguments:
            return b"4096"
        if "pg_dump" in arguments:
            output.write_bytes(b"PGDMP-not-a-real-database-unit-fixture")
        if "pg_restore" in arguments:
            (self.workspace / "candidate/database/restored").write_bytes(b"restored database")
        return b""


class Crash(BaseException):
    pass


class MaintenanceTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.root / "jobs").mkdir()
        self.job = "9" * 32
        write_json(self.root / "jobs" / (self.job + ".json"), {"instance_id": self.identifier, "checksum": CHECKSUM,
            "runtime_image": IMAGE, "docker": "/test/docker", "docker_host": "unix:///test/engine"})
        release = self.root / "releases" / CHECKSUM
        release.mkdir(parents=True)
        write_json(release / "images.lock.json", {"postgres": DEPENDENCY, "napcat": DEPENDENCY})
        write_json(self.directory / "config/control.json", {"port": 12345, "token": secrets.token_urlsafe(32)})
        self.c = FakeController(self.job, self.root)
        write_json(self.c.desired_path, {"bot_enabled": True, "revision": 2})

    def operation(self, kind, identifier, **extra):
        self.c.operation = {"id": identifier, "kind": kind, "revision": self.c.store.read(self.identifier)["revision"],
            "status": "queued", **extra}
        self.c.persist()
        return FakeMaintenance(self.c)

    def test_backup_restores_original_runtime_and_restores_data_without_auto_reply(self):
        public_model = self.directory / "assets/ggml-base.bin"
        public_model.write_bytes(b"reproducible-public-model")
        self.operation("backup", "1" * 32).run()
        self.assertEqual(self.c.operation["status"], "completed")
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})
        self.assertFalse((self.directory / "config/maintenance.json").exists())
        (self.directory / "state/notes.txt").write_text("newer state")
        self.operation("restore", "2" * 32, backup_id="1" * 32).run()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual((self.directory / "state/notes.txt").read_text(), "群聊记忆")
        self.assertEqual((self.directory / "database/restored").read_bytes(), b"restored database")
        self.assertEqual((self.directory / "recovery" / ("2" * 32) / "rollback/state/notes.txt").read_text(), "newer state")
        self.assertFalse(read_json(self.c.desired_path)["bot_enabled"])
        self.assertNotIn("bot", self.c.running)
        self.assertEqual(self.store.read(self.identifier)["revision"], 3)
        self.assertEqual((self.directory / "config/admin.secret").read_bytes(), self.original_secret)
        self.assertEqual(public_model.read_bytes(), b"reproducible-public-model")

    def test_insufficient_space_recovers_running_state_without_creating_backup(self):
        with patch("src.local_runtime.maintenance.shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 99, 1)):
            self.operation("backup", "1" * 32).run()
        self.assertEqual(self.c.operation["status"], "failed")
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})
        self.assertEqual(self.backups.list(), [])
        self.assertFalse((self.directory / "config/maintenance.json").exists())

    def test_sandbox_is_paused_before_dump_and_ready_before_bot_resumes(self):
        write_json(self.c.sandbox.enabled_path, {})
        events = []
        original_command = FakeMaintenance.command
        original_compose = self.c.compose
        def paused():
            self.assertNotIn("bot", self.c.running)
            self.assertTrue((self.directory / "config/maintenance.json").exists())
            self.c.sandbox.enabled_path.unlink(missing_ok=True)
            events.append("paused")
        def resumed():
            self.assertEqual(read_json(self.directory / "config/maintenance.json")["stage"], "resuming")
            self.assertNotIn("bot", self.c.running)
            write_json(self.c.sandbox.enabled_path, {})
            events.append("resumed")
        def command(maintenance, arguments, **kwargs):
            if "pg_dump" in arguments:
                self.assertEqual(events, ["paused"])
                events.append("dump")
            return original_command(maintenance, arguments, **kwargs)
        def compose(arguments, **kwargs):
            if arguments[0] == "up" and "bot" in arguments:
                self.assertEqual(events[-1], "resumed")
            return original_compose(arguments, **kwargs)
        with patch.object(self.c.sandbox, "pause", side_effect=paused), patch.object(self.c.sandbox, "start", side_effect=resumed), \
                patch.object(self.c, "compose", side_effect=compose), patch.object(FakeMaintenance, "command", command):
            self.operation("backup", "1" * 32).run()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        self.assertEqual(events, ["paused", "dump", "resumed"])
        self.assertTrue(self.c.sandbox.enabled_path.exists())

    def test_unconfirmed_sandbox_stop_never_produces_a_backup(self):
        write_json(self.c.sandbox.enabled_path, {})
        with patch.object(self.c.sandbox, "pause", side_effect=SetupError("sandbox still running")), \
                patch.object(FakeMaintenance, "command") as command:
            self.operation("backup", "1" * 32).run()
        self.assertEqual(self.c.operation["status"], "failed")
        self.assertEqual(self.c.operation["stage"], "recovery_required")
        self.assertFalse(any("pg_dump" in call.args[0] for call in command.call_args_list))
        self.assertEqual(self.backups.list(), [])

    def test_restore_remaps_component_credentials_to_new_revision(self):
        config = self.store.read(self.identifier)
        config["settings"]["components"]["embedding"]["base_url"] = "https://embedding.invalid/v1"
        self.store.update(self.identifier, config["settings"], expected_revision=config["revision"], service_keys={"embedding": "original-vector-secret"})
        self.operation("backup", "1" * 32).run()
        config = self.store.read(self.identifier)
        self.store.update(self.identifier, config["settings"], expected_revision=config["revision"], service_keys={"embedding": "newer-secret"})
        self.operation("restore", "2" * 32, backup_id="1" * 32).run()
        self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
        config, private = self.store.snapshot(self.identifier)
        self.assertEqual(config["revision"], 5)
        self.assertEqual(private["component_keys"], {"embedding": "original-vector-secret"})
        self.assertEqual(read_json(self.directory / "config/runtime.json")["environment"]["AI_EMBEDDING_API_KEY"], "original-vector-secret")

    def test_crashes_between_directory_renames_are_recovered(self):
        self.publish()
        original = os.rename
        for number in range(1, 9):
            with self.subTest(rename=number):
                operation_id = f"{number + 10:032x}"
                (self.directory / "state/notes.txt").write_text("before restore")
                maintenance = self.operation("restore", operation_id, backup_id="1" * 32)
                count = 0
                def crash_after(source, target):
                    nonlocal count
                    original(source, target)
                    count += 1
                    if count == number:
                        raise Crash("simulated process death")
                with patch("src.local_runtime.maintenance.os.rename", side_effect=crash_after), self.assertRaises(Crash):
                    maintenance.run()
                self.c = FakeController(self.job, self.root)
                FakeMaintenance(self.c).run(recovery_only=True)
                self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
                self.assertEqual((self.directory / "state/notes.txt").read_text(), "before restore")
                self.assertTrue((self.directory / "database/original").exists())
                self.assertEqual(self.c.running, {"bot", "database", "napcat"})
                self.assertFalse((self.directory / "config/maintenance.json").exists())

    def test_crashes_between_config_writes_do_not_lock_out_recovery(self):
        self.publish()
        from src.local_runtime.maintenance import CONFIG
        for number in range(1, len(CONFIG) + 1):
            with self.subTest(config_write=number):
                (self.directory / "state/notes.txt").write_text("before config crash")
                maintenance = self.operation("restore", f"{number + 30:032x}", backup_id="1" * 32)
                count = 0
                def crash_after(filename, value, **kwargs):
                    nonlocal count
                    write_bytes(filename, value, **kwargs)
                    if filename in [self.directory / name for name in CONFIG]:
                        count += 1
                        if count == number:
                            raise Crash("simulated config commit death")
                with patch("src.local_runtime.maintenance.write_bytes", side_effect=crash_after), self.assertRaises(Crash):
                    maintenance.run()
                self.c = FakeController(self.job, self.root)
                self.assertTrue(self.c.status()["maintenance_pending"])
                FakeMaintenance(self.c).run(recovery_only=True)
                self.assertEqual(self.c.operation["status"], "completed", self.c.operation)
                self.assertEqual((self.directory / "state/notes.txt").read_text(), "before config crash")
                self.assertEqual(self.store.read(self.identifier)["revision"], 2)
                self.assertEqual(self.store.snapshot(self.identifier)[1]["model_keys"], {"mine": "private-test-model-key"})

    def test_failed_database_validation_never_commits_candidate(self):
        self.publish()
        maintenance = self.operation("restore", "e" * 32, backup_id="1" * 32)
        original = maintenance.command
        def fail_restore(arguments, **kwargs):
            if "pg_restore" in arguments:
                raise BackupError("模拟数据库恢复失败")
            return original(arguments, **kwargs)
        with patch.object(maintenance, "command", side_effect=fail_restore):
            maintenance.run()
        self.assertEqual(self.c.operation["status"], "failed")
        self.assertTrue((self.directory / "database/original").exists())
        self.assertEqual(self.c.running, {"bot", "database", "napcat"})


@unittest.skipUnless(os.environ.get("GAOJI_TEST_PG_BIN"), "Set GAOJI_TEST_PG_BIN to test with a temporary local PostgreSQL")
class RealPostgresTests(Fixture):
    def test_custom_dump_archive_and_restore_with_unicode_data(self):
        binaries = Path(os.environ["GAOJI_TEST_PG_BIN"])
        with tempfile.TemporaryDirectory(prefix="gaoji-pg-", dir="/private/tmp" if os.sys.platform == "darwin" else None) as temporary:
            root = Path(temporary)
            data, socket = root / "data", root / "sock"
            socket.mkdir(mode=0o700)
            def command(name, *arguments, **kwargs):
                return subprocess.run([str(binaries / name), *arguments], check=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=60, **kwargs)
            command("initdb", "-D", str(data), "-U", "gaoji", "--auth=trust", "--no-locale", "--encoding=UTF8")
            command("pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"), "-o", f"-h '' -k {socket} -p 55439", "-w", "start")
            try:
                flags = ("-h", str(socket), "-p", "55439", "-U", "gaoji")
                command("createdb", *flags, "source")
                command("createdb", *flags, "restored")
                from src.bot_storage.cli import main as migrate
                from src.bot_storage.database import PostgresDatabase
                from src.bot_storage.schema import HEAD_REVISION
                source_dsn = f"user=gaoji dbname=source host={socket} port=55439"
                with patch.dict(os.environ, {"AI_POSTGRES_DSN": source_dsn, "AI_POSTGRES_SCHEMA": "qq_bot"}):
                    self.assertEqual(migrate(["upgrade"]), 0)
                command("psql", *flags, "-d", "source", "-c", "CREATE TABLE notes (id integer primary key, text text); INSERT INTO notes VALUES (1, '群聊记忆与中文 PDF');")
                dump = command("pg_dump", *flags, "-d", "source", "--format=custom", "--no-owner", "--no-acl").stdout
                self.dump.write_bytes(dump)
                record = self.publish()
                extracted = self.root / "restored"
                verify_archive(self.backups.path(record["id"]) / "snapshot.tar", expected_checksum=CHECKSUM, max_bytes=record["bytes"], destination=extracted)
                command("pg_restore", *flags, "-d", "restored", "--no-owner", "--no-acl", "--single-transaction", "--exit-on-error", str(extracted / "database.dump"))
                result = command("psql", *flags, "-d", "restored", "-Atc", "SELECT text FROM notes WHERE id = 1").stdout.decode().strip()
                self.assertEqual(result, "群聊记忆与中文 PDF")
                database = PostgresDatabase(f"user=gaoji dbname=restored host={socket} port=55439", schema="qq_bot")
                try:
                    database.require_revision(HEAD_REVISION)
                finally:
                    database.close()
                self.assertTrue(command("psql", *flags, "-d", "restored", "-Atc", "SELECT extversion FROM pg_extension WHERE extname = 'vector'").stdout.strip())
            finally:
                command("pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop")


if __name__ == "__main__":
    unittest.main()
