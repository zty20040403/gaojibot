"""Only fake metadata and private temporary files; no network or real identity."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.plugins.ai_chat.platform_types import AccountKey, EndpointKey

from src.plugins.ai_chat.adapters.qq_official.native_media import fingerprint, parse_upload_receipt
from src.plugins.ai_chat.adapters.qq_official.native_vault import (
    NativeUploadBinding,
    NativeUploadRef,
    NativeUploadVault,
    NativeVaultError,
)


class NativeVaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "native"
        self.now = 1000
        self.vault = self.open_vault()
        self.account = AccountKey("qq-official", "TEST_APP_NATIVE", "test")
        self.endpoint = EndpointKey(self.account, "group", "TEST_OPAQUE:群")
        self.artifact = fingerprint([b"%PDF-TEST_ONLY"], filename="test.pdf", file_type=4)
        self.binding = NativeUploadBinding("1" * 32, "2" * 32, self.endpoint, self.artifact, "application/pdf")
        self.media = parse_upload_receipt(200, {
            "file_info": "TEST_PRIVATE_FILE_INFO", "file_uuid": "TEST_PRIVATE_UUID", "ttl": 300,
            "raw_url": "https://TEST-NEVER-PERSIST.invalid/private",
            "unused_body": "TEST_NEVER_PERSIST_RESPONSE",
        }, endpoint=self.endpoint, artifact=self.artifact, now=self.now)

    def open_vault(self, **kwargs):
        vault = NativeUploadVault(self.directory, clock=lambda: self.now, **kwargs)
        self.addCleanup(vault.close)
        return vault

    def path(self, reference):
        return self.directory / (reference.credential_ref + ".json")

    def stage_path(self, reference):
        return self.directory / (".upload-" + reference.credential_ref + ".tmp")

    def save(self):
        return self.vault.save(self.binding, self.media)

    def assert_safe_error(self, function, *args, **kwargs):
        with self.assertRaises(NativeVaultError) as error:
            function(*args, **kwargs)
        self.assertRegex(str(error.exception), r"^[a-z_]+$")
        self.assertNotIn(self.media.file_info, repr(error.exception))
        self.assertNotIn(self.account.app_id, repr(error.exception))

    def test_success_uses_minimal_protected_evidence_and_exact_original_expiry(self):
        reference = self.save()
        self.assertEqual(reference.expires_at, 1295)
        self.assertEqual(self.vault.read(reference, self.binding), self.media)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path(reference).stat().st_mode), 0o600)
        data = json.loads(self.path(reference).read_bytes())
        self.assertEqual(set(data["receipt"]), {"file_info", "file_uuid", "received_at", "remote_ttl_seconds", "reusable_until", "contract"})
        self.assertEqual(data["binding"]["account_key"], self.account.key)
        self.assertEqual(data["binding"]["endpoint_key"], self.endpoint.key)
        self.assertEqual(data["binding"]["artifact"]["sha256"], self.artifact.sha256)
        raw = self.path(reference).read_text()
        self.assertNotIn("NEVER_PERSIST", raw)
        self.assertNotIn("raw_url", raw)
        self.assertNotIn("https:", raw)
        for value in (self.vault, reference, self.binding, self.media):
            self.assertNotIn(self.media.file_info, repr(value))
            self.assertNotIn(self.account.app_id, repr(value))

    def test_restart_recovers_same_reference_and_never_renews_ttl(self):
        reference = self.save()
        self.vault.close()
        self.now = 1200
        restarted = self.open_vault()
        recovered, media = restarted.recover_stage(self.binding)
        self.assertEqual(recovered, reference)
        self.assertEqual(media, self.media)
        self.assertEqual(restarted.save(self.binding, self.media), reference)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_twenty_concurrent_saves_across_instances_have_one_immutable_winner(self):
        vaults = [self.open_vault() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            refs = list(executor.map(lambda i: vaults[i % 4].save(self.binding, self.media), range(20)))
        self.assertEqual(len(set(refs)), 1)
        self.assertEqual(len(list(self.directory.iterdir())), 1)
        self.assertEqual(self.path(refs[0]).stat().st_nlink, 1)

    def test_conflicting_same_stage_never_overwrites_original_evidence(self):
        reference = self.save()
        original = self.path(reference).read_bytes()
        for updates in ({"file_info": "TEST_OTHER_INFO"}, {"file_uuid": "TEST_OTHER_UUID"},
                        {"reusable_until": 1294}, {"received_at": 999}, {"remote_ttl_seconds": 301}):
            with self.subTest(fields=tuple(updates)):
                self.assert_safe_error(self.vault.save, self.binding, replace(self.media, **updates))
                self.assertEqual(self.path(reference).read_bytes(), original)
        self.assertEqual(self.vault.read(reference, self.binding), self.media)

    def test_binding_isolates_account_environment_endpoint_artifact_and_stage(self):
        reference = self.save()
        variants = [
            replace(self.binding, endpoint=replace(self.endpoint, account=replace(self.account, app_id="TEST_OTHER_APP"))),
            replace(self.binding, endpoint=replace(self.endpoint, account=replace(self.account, environment="production"))),
            replace(self.binding, endpoint=replace(self.endpoint, kind="private")),
            replace(self.binding, endpoint=replace(self.endpoint, native_conversation_id="TEST_OTHER_GROUP")),
            replace(self.binding, artifact_id="3" * 32),
            replace(self.binding, stage_attempt_id="4" * 32),
            replace(self.binding, artifact=replace(self.artifact, sha256="f" * 64)),
            replace(self.binding, artifact=replace(self.artifact, size=self.artifact.size + 1)),
            replace(self.binding, artifact=replace(self.artifact, filename="renamed.pdf")),
            replace(self.binding, artifact=replace(self.artifact, filename="test.zip"), mime="application/zip"),
        ]
        for binding in variants:
            with self.subTest(binding=repr(binding)):
                self.assert_safe_error(self.vault.read, reference, binding)
                if binding.endpoint != self.endpoint or binding.artifact != self.artifact:
                    self.assert_safe_error(self.vault.save, binding, self.media)
                other = replace(self.media, endpoint=binding.endpoint, artifact=binding.artifact)
                other_ref = self.vault.save(binding, other)
                self.assertNotEqual(other_ref.credential_ref, reference.credential_ref)
                self.assertNotEqual(other_ref.binding_digest, reference.binding_digest)

    def test_reference_requires_both_persisted_digest_and_original_expiry(self):
        reference = self.save()
        for other in (replace(reference, binding_digest="f" * 64), replace(reference, expires_at=reference.expires_at + 1),
                      replace(reference, credential_ref="f" * 32)):
            self.assert_safe_error(self.vault.read, other, self.binding)
            self.assert_safe_error(self.vault.delete, other, self.binding)
        self.assertTrue(self.path(reference).exists())

    def test_tampered_secret_cannot_match_persisted_commitment_even_with_rehashed_file(self):
        reference = self.save()
        data = json.loads(self.path(reference).read_bytes())
        data["receipt"]["file_info"] = "TEST_TAMPERED"
        canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        data["binding_digest"] = hashlib.sha256(canonical({"binding": data["binding"], "receipt": data["receipt"]})).hexdigest()
        self.path(reference).write_bytes(canonical(data))
        self.assert_safe_error(self.vault.read, reference, self.binding)
        self.assert_safe_error(self.vault.delete, reference, self.binding)

    def test_raw_tamper_or_noncanonical_body_is_rejected(self):
        reference = self.save()
        original = self.path(reference).read_bytes()
        for raw in (original + b"\n", b"{}", original.replace(b"TEST_PRIVATE_FILE_INFO", b"TEST_PRIVATE_OTHER_INF"), b"[1]", b"not json"):
            self.path(reference).write_bytes(raw)
            self.assert_safe_error(self.vault.read, reference, self.binding)
        self.path(reference).write_bytes(original)
        self.assertEqual(self.vault.read(reference, self.binding), self.media)

    def test_fsynced_staging_record_recovers_without_upload_or_new_receipt(self):
        with patch("src.plugins.ai_chat.adapters.qq_official.native_vault.os.link", side_effect=OSError("TEST_UNKNOWN_LINK_RESULT")):
            self.assert_safe_error(self.save)
        self.assertEqual(len(list(self.directory.iterdir())), 1)
        self.assertTrue(next(self.directory.iterdir()).name.endswith(".tmp"))
        restarted = self.open_vault()
        reference, media = restarted.recover_stage(self.binding)
        self.assertEqual(media, self.media)
        self.assertEqual(reference.expires_at, self.media.reusable_until)
        self.assertTrue(self.path(reference).exists())
        self.assertFalse(self.stage_path(reference).exists())

    def test_crash_after_link_before_unlink_repairs_only_known_same_inode_alias(self):
        unlink = os.unlink
        def interrupted(name, *args, **kwargs):
            if str(name).startswith(".upload-"):
                raise OSError("TEST_SIMULATED_CRASH")
            return unlink(name, *args, **kwargs)
        with patch("src.plugins.ai_chat.adapters.qq_official.native_vault.os.unlink", side_effect=interrupted):
            self.assert_safe_error(self.save)
        self.assertEqual(len(list(self.directory.iterdir())), 2)
        reference, media = self.open_vault().recover_stage(self.binding)
        self.assertEqual(media, self.media)
        self.assertEqual(self.path(reference).stat().st_nlink, 1)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_directory_fsync_unknown_keeps_original_receipt_for_restart(self):
        fsync = os.fsync
        def interrupted(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("TEST_FSYNC_UNKNOWN")
            return fsync(fd)
        with patch("src.plugins.ai_chat.adapters.qq_official.native_vault.os.fsync", side_effect=interrupted):
            self.assert_safe_error(self.save)
        reference, media = self.open_vault().recover_stage(self.binding)
        self.assertEqual(media, self.media)
        self.assertEqual(reference.expires_at, 1295)

    def test_partial_unknown_file_never_recreates_or_overwrites_a_handle(self):
        reference = self.save()
        path = self.path(reference)
        stage = self.stage_path(reference)
        path.rename(stage)
        stage.write_bytes(b'{"incomplete":')
        self.assert_safe_error(self.vault.recover_stage, self.binding)
        self.assert_safe_error(self.vault.save, self.binding, self.media)
        self.assertEqual(stage.read_bytes(), b'{"incomplete":')
        self.assertFalse(path.exists())
        self.now = 100000
        self.assertEqual(self.vault.collect_expired()["skipped"], 1)
        self.assertTrue(stage.exists())

    def test_expiry_uses_original_time_and_process_clock_rollback_cannot_revive(self):
        reference = self.save()
        self.now = reference.expires_at
        self.assert_safe_error(self.vault.read, reference, self.binding)
        self.assert_safe_error(self.vault.recover_stage, self.binding)
        self.now -= 10
        self.assert_safe_error(self.vault.read, reference, self.binding)
        self.assertEqual(self.vault.collect_expired()["removed"], 1)
        self.assertFalse(self.path(reference).exists())
        self.assert_safe_error(self.vault.save, self.binding, self.media)

    def test_corrupt_private_staging_cleanup_has_a_bounded_retention_without_renewal(self):
        reference = self.save()
        stage = self.stage_path(reference)
        self.path(reference).rename(stage)
        stage.write_bytes(b'{"file_info":"TEST_PARTIAL_PRIVATE_INFO')
        os.utime(stage, (1000, 1000))
        self.now = 1000 + 86400 - 1
        self.assertEqual(self.vault.collect_expired()["skipped"], 1)
        self.assert_safe_error(self.vault.recover_stage, self.binding)
        self.now += 1
        self.assertEqual(self.vault.collect_expired()["removed"], 1)
        self.assertFalse(stage.exists())

    def test_zero_provider_ttl_still_has_original_bounded_local_expiry(self):
        media = parse_upload_receipt(200, {"file_info": "TEST_FOREVER_REMOTE", "file_uuid": "TEST_UUID", "ttl": 0},
                                     endpoint=self.endpoint, artifact=self.artifact, now=self.now)
        reference = self.vault.save(self.binding, media)
        self.assertEqual(reference.expires_at, 1000 + 86400 - 5)
        self.now = reference.expires_at - 1
        self.assertEqual(self.open_vault().read(reference, self.binding), media)
        self.now += 1
        self.assert_safe_error(self.open_vault().recover_stage, self.binding)

    def test_collection_pages_past_live_prefix_and_cleans_expired_stage_alias(self):
        pairs = []
        for index in range(1, 5):
            binding = replace(self.binding, stage_attempt_id=f"{index:032x}")
            reference = self.vault.save(binding, self.media)
            pairs.append((reference, binding))
        pairs.sort(key=lambda pair: pair[0].credential_ref)
        live_ref, live_binding = pairs[0]
        self.vault.delete(live_ref, live_binding)
        live = replace(self.media, reusable_until=1500, remote_ttl_seconds=600)
        self.vault.save(live_binding, live)
        # One expired complete record stopped before final link.
        last_ref = pairs[-1][0]
        self.path(last_ref).rename(self.stage_path(last_ref))
        self.now = 1300
        page = self.vault.collect_expired(limit=1)
        self.assertEqual((page["scanned"], page["removed"]), (1, 0))
        removed = 0
        while page["next_ref"]:
            page = self.vault.collect_expired(after_ref=page["next_ref"], limit=1)
            removed += page["removed"]
        self.assertEqual(removed, 3)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_delete_is_exact_namespace_idempotent_and_allowed_after_expiry(self):
        reference = self.save()
        self.assert_safe_error(self.vault.delete, reference, replace(self.binding, stage_attempt_id="f" * 32))
        self.now = reference.expires_at
        self.vault.delete(reference, self.binding)
        self.vault.delete(reference, self.binding)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_symlink_and_arbitrary_hardlink_are_denied(self):
        reference = self.save()
        path = self.path(reference)
        outside = Path(self.temporary.name) / "external"
        path.rename(outside)
        path.symlink_to(outside)
        self.assert_safe_error(self.vault.read, reference, self.binding)
        path.unlink()
        path.hardlink_to(outside)
        self.assert_safe_error(self.vault.read, reference, self.binding)
        self.assertEqual(self.vault.collect_expired()["skipped"], 1)
        self.assertTrue(outside.exists())

    def test_private_modes_are_rechecked_for_reads_and_directory_reuse(self):
        reference = self.save()
        self.path(reference).chmod(0o640)
        self.assert_safe_error(self.vault.read, reference, self.binding)
        self.path(reference).chmod(0o600)
        self.directory.chmod(0o750)
        self.assert_safe_error(self.vault.read, reference, self.binding)
        self.assert_safe_error(self.open_vault)
        self.directory.chmod(0o700)
        self.assertEqual(self.vault.read(reference, self.binding), self.media)

    @unittest.skipUnless(sys.platform == "darwin", "macOS effective ACL regression")
    def test_mode0600_extended_acl_read_is_rejected(self):
        reference = self.save()
        path = self.path(reference)
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(path)], check=True, capture_output=True)
        try:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assert_safe_error(self.vault.read, reference, self.binding)
        finally:
            subprocess.run(["/bin/chmod", "-N", str(path)], check=True, capture_output=True)
        self.assertEqual(self.vault.read(reference, self.binding), self.media)

    @unittest.skipUnless(sys.platform == "darwin", "macOS directory ACL regression")
    def test_directory_extended_acl_is_rejected_before_secret_write(self):
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(self.directory)], check=True, capture_output=True)
        try:
            self.assert_safe_error(self.save)
            self.assert_safe_error(self.open_vault)
            self.assertEqual(list(self.directory.iterdir()), [])
        finally:
            subprocess.run(["/bin/chmod", "-N", str(self.directory)], check=True, capture_output=True)

    def test_closed_vault_and_bad_clock_errors_are_fixed_safe(self):
        reference = self.save()
        self.vault.close()
        self.assert_safe_error(self.vault.read, reference, self.binding)
        for now in (True, -1, float("nan"), float("inf"), 2**63, "1000"):
            self.now = now
            self.assert_safe_error(self.open_vault().read, reference, self.binding)

    def test_invalid_configuration_reference_and_collection_parameters_are_strict(self):
        for ttl in (True, 0, 604801, "100", float("inf")):
            self.assert_safe_error(self.open_vault, max_private_ttl=ttl)
        for reference in ("../outside", "A" * 32, 1, None):
            self.assert_safe_error(NativeUploadRef, reference, "a" * 64, 1000)
        for expiry in (True, -1, 2**63, 1.5):
            self.assert_safe_error(NativeUploadRef, "a" * 32, "b" * 64, expiry)
        for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 101}, {"after_ref": 1}, {"after_ref": "../outside"}):
            self.assert_safe_error(self.vault.collect_expired, **kwargs)

    def test_unvalidated_metadata_never_reaches_disk(self):
        for updates in ({"file_info": ""}, {"file_info": "x\nsecret"}, {"file_info": "x" * 65537},
                        {"file_uuid": ""}, {"remote_ttl_seconds": True}, {"received_at": True},
                        {"reusable_until": float("nan")}, {"reusable_until": 1000},
                        {"reusable_until": 1000 + 86401}, {"remote_ttl_seconds": 1},
                        {"contract": "TEST_OTHER_CONTRACT"}, {"received_at": 1001}):
            with self.subTest(fields=tuple(updates)):
                self.assert_safe_error(self.vault.save, self.binding, replace(self.media, **updates))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_native_type_and_root_artifact_size_mime_filename_policy_are_strict(self):
        for artifact, mime in ((replace(self.artifact, file_type=1), "application/pdf"),
                               (replace(self.artifact, size=104857601), "application/pdf"),
                               (replace(self.artifact, filename="x" * 177 + ".pdf"), "application/pdf"),
                               (self.artifact, "text/plain"), (self.artifact, "application/zip")):
            self.assert_safe_error(NativeUploadBinding, "1" * 32, "2" * 32, self.endpoint, artifact, mime)
        zip_artifact = fingerprint([b"PK_TEST_ZIP"], filename="test.zip", file_type=4)
        binding = replace(self.binding, artifact=zip_artifact, mime="application/zip")
        media = replace(self.media, artifact=zip_artifact)
        reference = self.vault.save(binding, media)
        self.assertEqual(self.vault.read(reference, binding), media)


if __name__ == "__main__":
    unittest.main()
