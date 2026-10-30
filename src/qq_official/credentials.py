"""Protected credentials and narrowly scoped endpoint validation."""
from __future__ import annotations

import ctypes
import errno
import os
import re
import stat
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

DEFAULT_API_BASE = "https://api.bot.qq.com"
# Explicit compatibility address; target-account sandbox behavior is unverified.
# Never infer sandbox from an authentication error or switch environments.
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
_API_HOSTS = frozenset({"api.bot.qq.com", "api.sgroup.qq.com", "sandbox.api.sgroup.qq.com"})
_LOOPBACK = frozenset({"127.0.0.1", "::1"})


class CredentialError(ValueError):
    """Safe diagnostic; intentionally excludes filesystem paths and values."""


def _verify_posix_acl(fd: int) -> bool | None:
    """Inspect the opened object, never re-open a potentially replaced path.

    Mode 0600 is insufficient on macOS: an extended allow ACL can still grant
    read access. Reject all extended ACLs (including harmless deny/masked rules)
    rather than implementing an incomplete platform-specific ACL evaluator.
    """
    if sys.platform == "darwin":
        try:
            native = ctypes.CDLL(None, use_errno=True)
            get_acl = native.acl_get_fd_np
            get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
            get_acl.restype = ctypes.c_void_p
            free_acl = native.acl_free
            free_acl.argtypes = (ctypes.c_void_p,)
            free_acl.restype = ctypes.c_int
            ctypes.set_errno(0)
            acl = get_acl(fd, 0x00000100)  # macOS sys/acl.h: ACL_TYPE_EXTENDED
            error = ctypes.get_errno()
            if acl:
                free_acl(acl)
                raise CredentialError("Private file has an extended ACL")
            # On macOS an already-open, fstat-verified file with no ACL
            # returns ENOENT (unlike Linux's ENODATA xattr result).
            if error != errno.ENOENT:
                raise CredentialError("Private file ACL could not be verified")
        except (OSError, AttributeError):
            raise CredentialError("Private file ACL could not be verified") from None
        return
    if sys.platform.startswith("linux"):
        # A remote/NFSv4/Windows/FUSE filesystem may have permissions which are
        # not represented by system.posix_acl_access. Limit this native checker
        # to local filesystem contracts with POSIX ACL semantics. Unsupported
        # stores fail closed instead of silently trusting st_mode.
        local_posix_filesystems = {
            0xEF53,       # ext2/3/4
            0x58465342,   # XFS
            0x9123683E,   # Btrfs
            0x01021994,   # tmpfs (including /run service credentials)
            0x794C7630,   # overlayfs
            0x2FC12FC1,   # ZFS with POSIX ACLs
            0xF2F52010,   # F2FS
            0xCA451A4E,   # bcachefs
        }
        try:
            native = ctypes.CDLL(None, use_errno=True)
            fstatfs = native.fstatfs
            fstatfs.argtypes = (ctypes.c_int, ctypes.c_void_p)
            fstatfs.restype = ctypes.c_int
            # f_type is the first native long in Linux struct statfs. Reserve
            # more than the native struct size without guessing later fields.
            filesystem = ctypes.create_string_buffer(512)
            if fstatfs(fd, filesystem) != 0:
                raise CredentialError("Private file filesystem could not be verified")
            filesystem_type = ctypes.c_long.from_buffer(filesystem).value & 0xFFFFFFFF
            if filesystem_type not in local_posix_filesystems:
                raise CredentialError("Private file filesystem ACL model is unsupported")
            attributes = os.listxattr(fd)
            if any(name in {"system.nfs4_acl", "system.richacl", "security.NTACL", "user.NTACL"}
                   for name in attributes):
                raise CredentialError("Private file has a non-POSIX ACL")
            try:
                acl = os.getxattr(fd, "system.posix_acl_access")
            except OSError as error:
                if error.errno == errno.ENODATA:
                    return
                # Native tmpfs can be compiled without TMPFS_POSIX_ACL. On
                # that verified local filesystem, ENOTSUP means this ACL
                # namespace is disabled and Unix mode is authoritative. Keep
                # requiring successful descriptor xattr enumeration, and do
                # not generalize this to remote/FUSE/unknown filesystems.
                if (error.errno == errno.ENOTSUP and filesystem_type == 0x01021994
                        and 'system.posix_acl_access' not in attributes):
                    return
                raise CredentialError("Private file ACL could not be verified") from None
            # systemd LoadCredential uses root-owned 0440 tmpfs files with
            # exactly one read-only grant to the service UID, and no group access.
            read_acl = struct.pack('<I', 2) + b''.join(
                struct.pack('<HHI', *entry) for entry in (
                    (1, 4, 0xFFFFFFFF), (2, 4, os.geteuid()),
                    (4, 0, 0xFFFFFFFF), (16, 4, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF),
                )
            )
            if filesystem_type == 0x01021994 and acl == read_acl:
                return True
            raise CredentialError("Private file has an extended ACL")
        except (OSError, AttributeError):
            raise CredentialError("Private file ACL could not be verified") from None
    raise CredentialError("Private file ACL verification is unsupported on this platform")


def verify_private_file(
    fd: int, *, acl_validator: Callable[[int], bool] | None = None,
) -> None:
    """Verify a private regular file by descriptor before reading or writing.

    Reusable for credentials and protected probe reports, including newly
    created files with inherited ACLs. No content is read. Windows uses native
    descriptor-based DACL checks; an explicit validator remains injectable.
    """
    try:
        info = os.fstat(fd)
    except OSError:
        raise CredentialError("Private file descriptor could not be verified") from None
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError("Private storage must be a regular file")
    if os.name == "nt":
        if acl_validator is None:
            from .windows_private import verify_windows_private_file
            acl_validator = verify_windows_private_file
        try:
            valid = acl_validator(fd)
        except (OSError, AttributeError, ValueError):
            valid = False
        if not valid:
            raise CredentialError("Private file ACL could not be verified")
        return
    mode = stat.S_IMODE(info.st_mode)
    uid = os.geteuid()
    private_read_acl = _verify_posix_acl(fd)
    permitted = info.st_uid in {uid, 0} and mode & ~0o600 == 0
    if info.st_uid == 0 and mode == 0o440 and private_read_acl is True:
        permitted = True
    if not permitted and info.st_uid == 0 and mode in {0o440, 0o640}:
        import grp
        import pwd

        # Preserve restricted root + service-group access, provided there are
        # no extra effective ACL grants and no unrelated group members.
        try:
            group = grp.getgrgid(info.st_gid)
            username = pwd.getpwuid(uid).pw_name
            primary_members = {p.pw_name for p in pwd.getpwall() if p.pw_gid == info.st_gid}
            members = set(group.gr_mem) | primary_members
            permitted = (
                info.st_gid in set(os.getgroups()) | {os.getegid()}
                and members <= {"root", username}
            )
        except KeyError:
            permitted = False
    if not permitted:
        raise CredentialError("Private file owner or permissions are unsafe")


def _read_secret(path: str | Path, acl_validator: Callable[[int], bool] | None) -> str:
    try:
        if os.name == "nt":
            from .windows_private import open_windows_private_readonly
            fd = open_windows_private_readonly(path)
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise CredentialError("Credential file could not be securely opened") from None
    try:
        verify_private_file(fd, acl_validator=acl_validator)
        if os.fstat(fd).st_size > 16384:
            raise CredentialError("Credential must be a small regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(16385)
        if len(value) > 16384:
            raise CredentialError("Credential file is too large")
        try:
            return value.decode("utf-8").strip()
        except UnicodeError:
            raise CredentialError("Credential file is not UTF-8") from None
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Credentials:
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.app_id, self.app_secret):
            if not isinstance(value, str) or not value or len(value) > 16384 or re.search(r"\s", value):
                raise CredentialError("Credential value is missing or invalid")

    @classmethod
    def from_files(
        cls,
        app_id_file: str | Path,
        app_secret_file: str | Path,
        *,
        acl_validator: Callable[[int], bool] | None = None,
    ) -> Credentials:
        return cls(_read_secret(app_id_file, acl_validator), _read_secret(app_secret_file, acl_validator))

    @property
    def is_test(self) -> bool:
        return self.app_id.startswith("TEST_") and self.app_secret.startswith("TEST_")


def _validate_url(
    url: str, *, websocket: bool, allow_test_server: bool, credentials: Credentials | None,
) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise CredentialError("Invalid platform endpoint") from None
    if (
        not parsed.hostname or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or "\\" in url or re.search(r"\s", url)
    ):
        raise CredentialError("Invalid platform endpoint")
    if allow_test_server:
        if credentials is None or not credentials.is_test:
            raise CredentialError("Local test transport requires TEST_ credentials")
        if parsed.hostname not in _LOOPBACK or parsed.scheme != ("ws" if websocket else "http"):
            raise CredentialError("Test transport only permits literal loopback endpoints")
    elif (
        parsed.scheme != ("wss" if websocket else "https")
        or parsed.hostname not in _API_HOSTS or port not in {None, 443}
    ):
        raise CredentialError("Untrusted platform endpoint")
    if not websocket and parsed.path not in {"", "/"}:
        raise CredentialError("API base must not contain a path")
    return url.rstrip("/") if not websocket else url


def validate_api_base(
    url: str, *, allow_test_server: bool = False, credentials: Credentials | None = None,
) -> str:
    return _validate_url(url, websocket=False, allow_test_server=allow_test_server, credentials=credentials)


def validate_gateway_url(
    url: str, *, allow_test_server: bool = False, credentials: Credentials | None = None,
) -> str:
    return _validate_url(url, websocket=True, allow_test_server=allow_test_server, credentials=credentials)
