"""The Windows policy is portable; actual ACL checks run only on Windows."""
import os
import ctypes
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from src.qq_official.windows_private import AccessEntry, private_acl


USER = "S-1-5-21-1-2-3-1001"
OTHER = "S-1-5-21-1-2-3-1002"
SYSTEM = "S-1-5-18"
ADMINS = "S-1-5-32-544"
EVERYONE = "S-1-1-0"
FULL = 0x1F01FF


class PrivateAclPolicyTests(unittest.TestCase):
    def test_private_user_system_and_administrators_can_read(self):
        entries = tuple(AccessEntry(0, FULL, sid) for sid in (USER, SYSTEM, ADMINS))
        for owner in (USER, SYSTEM, ADMINS):
            self.assertTrue(private_acl(owner, USER, entries))

    def test_unrelated_owner_is_not_private_even_with_restricted_dacl(self):
        self.assertFalse(private_acl(OTHER, USER, (AccessEntry(0, FULL, USER),)))

    def test_any_effective_allow_for_everyone_users_or_other_user_is_rejected(self):
        for sid in (OTHER, EVERYONE, "S-1-5-32-545", "S-1-5-11"):
            for mask in (1, 2, 0x20000, 0x40000, 0x10000000):
                with self.subTest(sid=sid, mask=mask):
                    self.assertFalse(private_acl(USER, USER, (
                        AccessEntry(0, FULL, USER), AccessEntry(0, mask, sid))))

    def test_deny_does_not_mask_an_unsafe_allow(self):
        self.assertFalse(private_acl(USER, USER, (
            AccessEntry(1, FULL, EVERYONE), AccessEntry(0, FULL, USER),
            AccessEntry(0, 1, OTHER))))

    def test_unknown_callback_and_object_aces_are_not_treated_as_denies(self):
        for kind in range(2, 22):
            self.assertFalse(private_acl(USER, USER, (AccessEntry(kind, FULL, USER),)))

    def test_inherit_only_allow_does_not_grant_access_to_regular_file(self):
        self.assertTrue(private_acl(USER, USER, (
            AccessEntry(0, FULL, USER), AccessEntry(0, FULL, EVERYONE, True))))

    def test_null_empty_or_oversized_dacl_cannot_be_accepted(self):
        self.assertFalse(private_acl(USER, USER, ()))
        self.assertFalse(private_acl(USER, USER, (AccessEntry(0, FULL, USER),) * 257))

    def test_missing_identity_and_invalid_masks_fail_closed(self):
        for owner, user, sid, mask in (("", USER, USER, 1), (USER, "", USER, 1),
                                      (USER, USER, "", 1), (USER, USER, USER, -1),
                                      (USER, USER, USER, 0x100000000)):
            self.assertFalse(private_acl(owner, user, (AccessEntry(0, mask, sid),)))


class WindowsLocalVolumePolicyTests(unittest.TestCase):
    def api(self, guid_path):
        from src.qq_official.windows_private import _Api, DWORD
        api = object.__new__(_Api)

        def path(handle, buffer, size, flags):
            # The DOS form can look local even when the handle is remote.
            value = guid_path if flags == 1 else "\\\\?\\Z:\\synthetic-credential"
            if value is None:
                return 0
            buffer.value = value
            return len(value)

        def volume(handle, name, size, serial, component, flags, fs, fs_size):
            ctypes.cast(flags, ctypes.POINTER(DWORD)).contents.value = 0x8
            fs.value = "NTFS"
            return 1

        api.kernel = SimpleNamespace(GetFileType=lambda handle: 1,
            GetFileInformationByHandleEx=lambda *args: 1,
            GetFinalPathNameByHandleW=path, GetVolumeInformationByHandleW=volume)
        return api

    def test_same_handle_local_volume_guid_is_required(self):
        self.api("\\\\?\\Volume{12345678-abcd-1234-5678-abcdef123456}\\credential").regular_local_file(1)

    def test_drive_letter_without_local_volume_guid_is_rejected(self):
        for path in (None, "\\\\?\\Z:\\credential", "\\\\?\\UNC\\server\\share\\credential"):
            with self.subTest(path=path), self.assertRaises(OSError):
                self.api(path).regular_local_file(1)


@unittest.skipUnless(os.name == "nt", "Actual Windows DACL filesystem checks")
class WindowsPrivateFileTests(unittest.TestCase):
    def setUp(self):
        from src.qq_official.windows_private import _Api
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic-credential"
        self.path.write_text("TEST_SECRET", encoding="utf-8")
        self.user = _Api().user()
        self.acl("/inheritance:r", "/grant:r", f"*{self.user}:(F)")

    def acl(self, *arguments):
        subprocess.run(["icacls", str(self.path), *arguments], check=True, capture_output=True)

    def test_real_restricted_acl_is_readable_and_broad_grant_is_denied(self):
        from src.qq_official.credentials import _read_secret, CredentialError
        self.assertEqual(_read_secret(self.path, None), "TEST_SECRET")
        self.acl("/grant", "*S-1-1-0:(R)")
        with self.assertRaises(CredentialError) as caught:
            _read_secret(self.path, None)
        self.assertNotIn("TEST_SECRET", str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))

    def test_acl_change_on_open_descriptor_is_checked_again(self):
        from src.qq_official.credentials import verify_private_file, CredentialError
        with self.path.open("rb") as stream:
            verify_private_file(stream.fileno())
            self.acl("/grant", "*S-1-1-0:(R)")
            with self.assertRaises(CredentialError):
                verify_private_file(stream.fileno())

    def test_symlink_is_rejected_when_symlinks_are_available(self):
        from src.qq_official.credentials import _read_secret, CredentialError
        link = self.path.with_name("linked-credential")
        try:
            link.symlink_to(self.path)
        except OSError as error:
            if error.winerror == 1314:
                self.skipTest("Windows runner lacks symbolic-link privilege")
            raise
        with self.assertRaises(CredentialError):
            _read_secret(link, None)
