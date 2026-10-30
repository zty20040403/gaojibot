"""Narrow Linux tmpfs ACL-disabled contract; real Linux probe is separate."""
import ctypes
import errno
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.qq_official import credentials


class LinuxPrivateACLTests(unittest.TestCase):
    def check(self, filesystem, *, error=errno.ENOTSUP, attributes=(), enumeration_error=None, value=None):
        def fstatfs(_fd, buffer):
            ctypes.c_long.from_buffer(buffer).value = filesystem
            return 0
        with patch.object(credentials.sys, 'platform', 'linux'), \
             patch.object(credentials.ctypes, 'CDLL', return_value=SimpleNamespace(fstatfs=fstatfs)), \
             patch.object(credentials.os, 'listxattr', create=True, return_value=list(attributes), side_effect=enumeration_error), \
             patch.object(credentials.os, 'getxattr', create=True, return_value=value,
                          side_effect=OSError(error, 'TEST ACL unavailable') if error else None):
            return credentials._verify_posix_acl(17)

    def test_systemd_read_acl_allows_only_the_current_service_user(self):
        entries = [(1, 4, 0xFFFFFFFF), (2, 4, 1004), (4, 0, 0xFFFFFFFF),
                   (16, 4, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
        def encoded(values):
            return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *x) for x in values)
        with patch.object(credentials.os, 'geteuid', return_value=1004):
            self.assertIs(self.check(0x01021994, error=None, value=encoded(entries)), True)
            for index, entry in ((1, (2, 4, 1005)), (2, (4, 4, 0xFFFFFFFF)),
                                 (4, (32, 4, 0xFFFFFFFF))):
                changed = entries.copy()
                changed[index] = entry
                with self.subTest(entry=entry), self.assertRaises(credentials.CredentialError):
                    self.check(0x01021994, error=None, value=encoded(changed))
            with self.assertRaises(credentials.CredentialError):
                self.check(0xEF53, error=None, value=encoded(entries))

    def test_root_read_only_credential_accepts_verified_service_acl(self):
        info = SimpleNamespace(st_mode=0o100440, st_uid=0, st_gid=0)
        with patch.object(credentials.os, 'fstat', return_value=info), \
             patch.object(credentials.os, 'geteuid', return_value=1004), \
             patch.object(credentials, '_verify_posix_acl', return_value=True):
            credentials.verify_private_file(17)

    def test_verified_tmpfs_acl_disabled_uses_checked_mode(self):
        self.check(0x01021994)

    def test_no_acl_on_supported_local_fs_remains_valid(self):
        self.check(0xEF53, error=errno.ENODATA)

    def test_enotsup_not_generalized_to_other_filesystems(self):
        for filesystem in (0xEF53, 0x794C7630, 0x6969, 0x65735546):
            with self.subTest(filesystem=filesystem), self.assertRaises(credentials.CredentialError):
                self.check(filesystem)

    def test_tmpfs_acl_or_foreign_acl_evidence_always_denied(self):
        for attributes in (('system.posix_acl_access',), ('system.nfs4_acl',), ('security.NTACL',)):
            with self.subTest(attributes=attributes), self.assertRaises(credentials.CredentialError):
                self.check(0x01021994, attributes=attributes)
        with self.assertRaises(credentials.CredentialError):
            self.check(0x01021994, error=None, value=b'TEST ACL')

    def test_tmpfs_other_failures_and_failed_enumeration_are_denied(self):
        for error in (errno.EACCES, errno.EIO, errno.EBADF):
            with self.subTest(error=error), self.assertRaises(credentials.CredentialError):
                self.check(0x01021994, error=error)
        with self.assertRaises(credentials.CredentialError):
            self.check(0x01021994, enumeration_error=OSError(errno.ENOTSUP, 'TEST no enumeration'))
