"""Inspect Windows private-file ACLs on the opened handle, without pywin32.

Only the effective user, LocalSystem and built-in Administrators may have
allow entries. Unknown/callback/object ACEs fail closed. No names or file
contents appear in diagnostics. Native entry points are loaded only on Windows.

Contracts: Microsoft GetSecurityInfo, OpenThreadToken, GetAclInformation,
GetAce, and CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT).
"""
from __future__ import annotations

import ctypes as c
from dataclasses import dataclass
import os
import re


@dataclass(frozen=True)
class AccessEntry:
    kind: int
    mask: int
    sid: str
    inherit_only: bool = False


def private_acl(owner: str, effective_user: str, entries: tuple[AccessEntry, ...]) -> bool:
    """A deliberately narrower policy than a general Windows access checker."""
    if not effective_user or not entries or len(entries) > 256:
        return False
    trusted = {effective_user, "S-1-5-18", "S-1-5-32-544"}
    if owner not in trusted:
        return False
    for ace in entries:
        if ace.kind not in {0, 1} or not ace.sid or not 0 <= ace.mask <= 0xFFFFFFFF:
            return False
        # A deny never expands access. Inherit-only entries do not apply to
        # this object; this module only accepts regular files, not directories.
        if ace.kind == 0 and not ace.inherit_only and ace.mask and ace.sid not in trusted:
            return False
    return True


DWORD = c.c_uint32
BOOL = c.c_int32
PTR = c.c_void_p
PPTR = c.POINTER(PTR)


class _AclSize(c.Structure):
    _fields_ = [("count", DWORD), ("used", DWORD), ("free", DWORD)]


class _FileTag(c.Structure):
    _fields_ = [("attributes", DWORD), ("tag", DWORD)]


class _Api:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows private-file inspection is unavailable")
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.advapi = c.WinDLL("advapi32", use_last_error=True)
        signatures = (
            (self.kernel, "CreateFileW", PTR, [c.c_wchar_p, DWORD, DWORD, PTR, DWORD, DWORD, PTR]),
            (self.kernel, "CloseHandle", BOOL, [PTR]),
            (self.kernel, "LocalFree", PTR, [PTR]),
            (self.kernel, "GetCurrentThread", PTR, []),
            (self.kernel, "GetCurrentProcess", PTR, []),
            (self.kernel, "GetFileType", DWORD, [PTR]),
            (self.kernel, "GetFileInformationByHandleEx", BOOL, [PTR, c.c_int, PTR, DWORD]),
            (self.kernel, "GetFinalPathNameByHandleW", DWORD, [PTR, c.c_wchar_p, DWORD, DWORD]),
            (self.kernel, "GetVolumeInformationByHandleW", BOOL,
             [PTR, c.c_wchar_p, DWORD, c.POINTER(DWORD), c.POINTER(DWORD), c.POINTER(DWORD), c.c_wchar_p, DWORD]),
            (self.advapi, "OpenThreadToken", BOOL, [PTR, DWORD, BOOL, PPTR]),
            (self.advapi, "OpenProcessToken", BOOL, [PTR, DWORD, PPTR]),
            (self.advapi, "GetTokenInformation", BOOL, [PTR, c.c_int, PTR, DWORD, c.POINTER(DWORD)]),
            (self.advapi, "GetSecurityInfo", DWORD, [PTR, c.c_int, DWORD, PPTR, PPTR, PPTR, PPTR, PPTR]),
            (self.advapi, "IsValidSecurityDescriptor", BOOL, [PTR]),
            (self.advapi, "IsValidAcl", BOOL, [PTR]),
            (self.advapi, "GetAclInformation", BOOL, [PTR, PTR, DWORD, c.c_int]),
            (self.advapi, "GetAce", BOOL, [PTR, DWORD, PPTR]),
            (self.advapi, "IsValidSid", BOOL, [PTR]),
            (self.advapi, "ConvertSidToStringSidW", BOOL, [PTR, PPTR]),
        )
        for dll, name, result, arguments in signatures:
            function = getattr(dll, name)
            function.restype, function.argtypes = result, arguments

    def sid(self, pointer):
        if not pointer or not self.advapi.IsValidSid(pointer):
            raise OSError("Private file SID could not be verified")
        text = PTR()
        if not self.advapi.ConvertSidToStringSidW(pointer, c.byref(text)):
            raise OSError("Private file SID could not be verified")
        try:
            return c.wstring_at(text)
        finally:
            self.kernel.LocalFree(text)

    def user(self):
        token = PTR()
        if not self.advapi.OpenThreadToken(self.kernel.GetCurrentThread(), 0x0008, True, c.byref(token)):
            if c.get_last_error() != 1008:  # ERROR_NO_TOKEN; never ignore an impersonation failure.
                raise OSError("Private file effective identity could not be verified")
            if not self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, c.byref(token)):
                raise OSError("Private file effective identity could not be verified")
        try:
            size = DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))  # TokenUser
            if not c.sizeof(PTR) <= size.value <= 65536:
                raise OSError("Private file effective identity could not be verified")
            buffer = c.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
                raise OSError("Private file effective identity could not be verified")
            # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first field is PSID.
            return self.sid(PTR.from_buffer(buffer))
        finally:
            self.kernel.CloseHandle(token)

    def regular_local_file(self, handle):
        tag = _FileTag()
        if (self.kernel.GetFileType(handle) != 1
                or not self.kernel.GetFileInformationByHandleEx(handle, 9, c.byref(tag), c.sizeof(tag))
                or tag.attributes & (0x10 | 0x400)):  # Directory or reparse point.
            raise OSError("Private storage must be a regular local file")
        path = c.create_unicode_buffer(32768)
        # A drive letter can represent a mapped share. Windows does not create
        # volume GUID paths for network shares; verify the same opened handle.
        length = self.kernel.GetFinalPathNameByHandleW(handle, path, len(path), 1)
        volume = r"^\\\\\?\\Volume\{[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}\\"
        if not 0 < length < len(path) or not re.match(volume, path.value):
            raise OSError("Private file volume could not be verified")
        fs = c.create_unicode_buffer(32)
        serial, component, flags = DWORD(), DWORD(), DWORD()
        if (not self.kernel.GetVolumeInformationByHandleW(handle, None, 0, c.byref(serial),
                c.byref(component), c.byref(flags), fs, len(fs))
                or fs.value.upper() not in {"NTFS", "REFS"} or not flags.value & 0x8):
            raise OSError("Private file ACL model is unsupported")

    def private(self, handle):
        self.regular_local_file(handle)
        owner, acl, descriptor = PTR(), PTR(), PTR()
        result = self.advapi.GetSecurityInfo(handle, 1, 0x1 | 0x4, c.byref(owner), None,
                                            c.byref(acl), None, c.byref(descriptor))
        if result:
            raise OSError("Private file ACL could not be verified")
        try:
            # A NULL DACL grants everyone access; it is never a private file.
            if (not descriptor or not self.advapi.IsValidSecurityDescriptor(descriptor)
                    or not acl or not self.advapi.IsValidAcl(acl)):
                return False
            size = _AclSize()
            if not self.advapi.GetAclInformation(acl, c.byref(size), c.sizeof(size), 2):
                raise OSError("Private file ACL could not be verified")
            if not 1 <= size.count <= 256 or not 8 <= size.used <= 65535:
                return False
            entries = []
            for index in range(size.count):
                ace = PTR()
                if not self.advapi.GetAce(acl, index, c.byref(ace)) or not ace:
                    raise OSError("Private file ACL could not be verified")
                header = c.string_at(ace, 4)
                kind, flags, length = header[0], header[1], int.from_bytes(header[2:], "little")
                if kind not in {0, 1} or not 16 <= length <= size.used:
                    return False
                data = c.string_at(ace, length)
                # SID has an eight-byte header and at most 15 DWORD subauthorities.
                if data[8] != 1 or data[9] > 15 or 16 + 4 * data[9] > length:
                    return False
                entries.append(AccessEntry(kind, int.from_bytes(data[4:8], "little"),
                                           self.sid(PTR(ace.value + 8)), bool(flags & 0x08)))
            return private_acl(self.sid(owner), self.user(), tuple(entries))
        finally:
            if descriptor:
                self.kernel.LocalFree(descriptor)


def verify_windows_private_file(fd: int) -> bool:
    import msvcrt
    return _Api().private(msvcrt.get_osfhandle(fd))


def open_windows_private_readonly(path) -> int:
    """Return an owned CRT descriptor; reject reparse points before reading."""
    import msvcrt
    api = _Api()
    handle = api.kernel.CreateFileW(os.fspath(path), 0x80000000 | 0x20000, 1, None,
                                   3, 0x00200000, None)
    if handle == c.c_void_p(-1).value:
        raise OSError("Private file could not be securely opened")
    try:
        api.regular_local_file(handle)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.kernel.CloseHandle(handle)
        raise
    return fd  # os.close(fd) now owns and closes the native handle.
