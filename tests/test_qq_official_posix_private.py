"""Narrow Linux tmpfs ACL-disabled contract; real Linux probe is separate."""
import ctypes
import errno
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
            credentials._verify_posix_acl(17)

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
