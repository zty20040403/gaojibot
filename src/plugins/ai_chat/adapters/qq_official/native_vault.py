"""Private native-upload evidence vault, separate from the download-token vault.

Only the trusted uploader receipt callback may save. Structural data and a file
reference do not prove QQ authentication or physical network completion: root's
durable stage CAS supplies that authority. No raw response or URL is retained.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from src.plugins.ai_chat.platform_types import EndpointKey
from src.qq_official.credentials import _verify_posix_acl, verify_private_file

from .native_media import (
    CONTRACT,
    ArtifactFingerprint,
    UploadedMedia,
    _endpoint,
    _text,
)

_REF = re.compile(r"[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_FILE_LIMIT = 131072
_MAX_TIME = 2**63 - 1


class NativeVaultError(ValueError):
    """Fixed diagnostics; never includes file_info, namespaces or paths."""


def _time(value):
    if type(value) is not int or not 0 <= value <= _MAX_TIME:
        raise NativeVaultError("invalid_native_vault_time")
    return value


def _ref(value):
    if type(value) is not str or not _REF.fullmatch(value):
        raise NativeVaultError("invalid_native_vault_reference")
    return value


def _digest(value):
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise NativeVaultError("invalid_native_vault_digest")
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, repr=False)
class NativeUploadBinding:
    artifact_id: str
    stage_attempt_id: str
    endpoint: EndpointKey
    artifact: ArtifactFingerprint
    mime: str

    def __post_init__(self):
        try:
            _ref(self.artifact_id)
            _ref(self.stage_attempt_id)
            _endpoint(self.endpoint)
            if not isinstance(self.artifact, ArtifactFingerprint):
                raise TypeError
            replace(self.artifact)
            if (self.artifact.file_type != 4 or self.artifact.size > 104857600
                    or len(self.artifact.filename.encode()) > 180
                    or self.mime not in {"application/pdf", "application/zip"}
                    or not self.artifact.filename.lower().endswith(".pdf" if self.mime == "application/pdf" else ".zip")):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise NativeVaultError("invalid_native_upload_binding") from None

    def __repr__(self):
        return "NativeUploadBinding(<protected>)"


@dataclass(frozen=True, repr=False)
class NativeUploadRef:
    credential_ref: str
    binding_digest: str
    expires_at: int

    def __post_init__(self):
        _ref(self.credential_ref)
        _digest(self.binding_digest)
        _time(self.expires_at)

    def __repr__(self):
        return "NativeUploadRef(<protected>)"


def _binding(binding):
    if not isinstance(binding, NativeUploadBinding):
        raise NativeVaultError("invalid_native_upload_binding")
    binding = replace(binding, artifact=replace(binding.artifact))
    item = binding.artifact
    return {"version": 1, "contract": CONTRACT, "artifact_id": binding.artifact_id,
        "stage_attempt_id": binding.stage_attempt_id, "account_key": binding.endpoint.account.key,
        "endpoint_key": binding.endpoint.key, "mime": binding.mime,
        "artifact": {"filename": item.filename, "file_type": item.file_type,
            "size": item.size, "sha256": item.sha256, "md5": item.md5,
            "sha1": item.sha1, "md5_10m": item.md5_10m}}


def _locator(binding_payload):
    # Stable opaque locator for recovery when the receipt DB commit is unknown.
    # Authority is never inferred from this truncated hash.
    return hashlib.sha256(b"qq-native-upload-v1\0" + _canonical(binding_payload)).hexdigest()[:32]


def _staging_name(locator):
    return ".upload-" + _ref(locator) + ".tmp"


class NativeUploadVault:
    def __init__(self, directory, *, clock=time.time, max_private_ttl=86400):
        self._fd, self._last_now = -1, 0
        self._lock = threading.RLock()
        self._clock = clock
        if type(max_private_ttl) is not int or not 1 <= max_private_ttl <= 604800 or not callable(clock):
            raise NativeVaultError("invalid_native_vault_configuration")
        self.max_private_ttl = max_private_ttl
        try:
            directory = Path(directory)
            if not directory.is_absolute():
                raise ValueError
            directory.mkdir(mode=0o700, exist_ok=True)
            self._fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._verify_directory()
        except Exception:  # noqa: BLE001 — private filesystem/configuration boundary.
            self.close()
            raise NativeVaultError("native_vault_unavailable") from None

    def __repr__(self):
        return "NativeUploadVault(<protected>)"

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def _verify_directory(self):
        info = os.fstat(self._fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise NativeVaultError("native_vault_unavailable")
        _verify_posix_acl(self._fd)

    @contextmanager
    def _guard(self):
        # RLock protects shared descriptor use across threads; flock additionally
        # serializes independent vault instances/processes on this directory.
        with self._lock:
            try:
                self._verify_directory()
                fcntl.flock(self._fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except NativeVaultError:
                raise
            except Exception:  # noqa: BLE001 — keep host/I/O exceptions free of secrets.
                raise NativeVaultError("native_vault_unavailable") from None

    def _now(self):
        value = self._clock()
        if type(value) not in {int, float} or not 0 <= value <= _MAX_TIME or not math.isfinite(value):
            raise NativeVaultError("invalid_native_vault_time")
        self._last_now = max(self._last_now, int(value))
        return self._last_now

    def _media(self, binding, media):
        if (not isinstance(media, UploadedMedia) or media.endpoint != binding.endpoint
                or media.artifact != binding.artifact or media.contract != CONTRACT):
            raise NativeVaultError("native_receipt_binding_changed")
        try:
            _text(media.file_info, 65536, "invalid_file_info")
            _text(media.file_uuid, 1024, "invalid_file_uuid")
            _time(media.received_at)
            _time(media.reusable_until)
            if (type(media.remote_ttl_seconds) is not int or not 0 <= media.remote_ttl_seconds <= 2**31 - 1
                    or not media.received_at < media.reusable_until <= media.received_at + self.max_private_ttl
                    or (media.remote_ttl_seconds > 0
                        and media.reusable_until > media.received_at + media.remote_ttl_seconds)):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise NativeVaultError("invalid_native_upload_receipt") from None
        return {"file_info": media.file_info, "file_uuid": media.file_uuid,
            "received_at": media.received_at, "remote_ttl_seconds": media.remote_ttl_seconds,
            "reusable_until": media.reusable_until, "contract": media.contract}

    def _encode(self, binding, media):
        description = _binding(binding)
        receipt = self._media(binding, media)
        payload = {"binding": description, "receipt": receipt}
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        locator = _locator(description)
        encoded = _canonical({"version": 1, "credential_ref": locator,
                              "binding_digest": digest, **payload})
        if len(encoded) > _FILE_LIMIT:
            raise NativeVaultError("native_receipt_too_large")
        return NativeUploadRef(locator, digest, media.reusable_until), encoded

    def _read_bytes(self, locator, *, staging=False):
        fd = -1
        try:
            final, temporary = _ref(locator) + ".json", _staging_name(locator)
            name, other = (temporary, final) if staging else (final, temporary)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
            verify_private_file(fd)
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise NativeVaultError("native_receipt_unavailable")
            if info.st_nlink == 2:
                # A crash between link and unlink may leave this one known
                # staging alias. Arbitrary additional hard links stay denied.
                try:
                    counterpart = os.stat(other, dir_fd=self._fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise NativeVaultError("native_receipt_unavailable") from None
                if (counterpart.st_dev, counterpart.st_ino) != (info.st_dev, info.st_ino):
                    raise NativeVaultError("native_receipt_unavailable")
            elif info.st_nlink != 1:
                raise NativeVaultError("native_receipt_unavailable")
            if info.st_size > _FILE_LIMIT:
                raise NativeVaultError("native_receipt_unavailable")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                result = stream.read(_FILE_LIMIT + 1)
            if len(result) > _FILE_LIMIT:
                raise NativeVaultError("native_receipt_unavailable")
            return result
        finally:
            if fd >= 0:
                os.close(fd)

    def _restore(self, binding):
        description = _binding(binding)
        locator = _locator(description)
        staging = False
        try:
            raw = self._read_bytes(locator)
        except FileNotFoundError:
            raw, staging = self._read_bytes(locator, staging=True), True
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {"version", "credential_ref", "binding_digest", "binding", "receipt"}
                or type(data["version"]) is not int or data["version"] != 1
                or data["credential_ref"] != locator or data["binding"] != description
                or not isinstance(data["receipt"], dict)
                or set(data["receipt"]) != {"file_info", "file_uuid", "received_at", "remote_ttl_seconds", "reusable_until", "contract"}):
            raise NativeVaultError("native_receipt_binding_changed")
        receipt = data["receipt"]
        media = UploadedMedia(binding.endpoint, binding.artifact, receipt["file_info"], receipt["file_uuid"],
            receipt["received_at"], receipt["remote_ttl_seconds"], receipt["reusable_until"], receipt["contract"])
        reference, encoded = self._encode(binding, media)
        if (not hmac.compare_digest(reference.binding_digest, _digest(data["binding_digest"]))
                or not hmac.compare_digest(encoded, raw)):
            raise NativeVaultError("native_receipt_integrity_failure")
        now = self._now()
        if now < media.received_at or now >= media.reusable_until:
            # Expired entries are removed only by lifecycle maintenance, so this
            # method cannot accidentally recreate/replace the same stage ref.
            raise NativeVaultError("native_receipt_expired")
        if staging:
            # Complete staged evidence can survive before its final directory
            # entry or database reference exists. Verify bytes and fsync them
            # before promoting; partial evidence fails closed above.
            fd = os.open(_staging_name(locator), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._fd)
            try:
                verify_private_file(fd)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(_staging_name(locator), locator + ".json",
                    src_dir_fd=self._fd, dst_dir_fd=self._fd, follow_symlinks=False)
        try:
            pending = os.stat(_staging_name(locator), dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            pending = None
        if pending is not None:
            final = os.stat(locator + ".json", dir_fd=self._fd, follow_symlinks=False)
            if (pending.st_dev, pending.st_ino) != (final.st_dev, final.st_ino):
                raise NativeVaultError("native_receipt_integrity_failure")
            os.unlink(_staging_name(locator), dir_fd=self._fd)
        os.fsync(self._fd)
        return reference, media

    def save(self, binding: NativeUploadBinding, media: UploadedMedia) -> NativeUploadRef:
        with self._guard():
            reference, encoded = self._encode(binding, media)
            now = self._now()
            if not media.received_at <= now < reference.expires_at:
                raise NativeVaultError("native_receipt_expired")
            try:
                original, _ = self._restore(binding)
            except FileNotFoundError:
                pass
            else:
                if original != reference:
                    raise NativeVaultError("native_stage_receipt_conflict")
                return original
            temporary = _staging_name(reference.credential_ref)
            fd = -1
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self._fd)
                verify_private_file(fd)
                with os.fdopen(fd, "wb", closefd=False) as output:
                    output.write(encoded)
                    output.flush()
                    os.fsync(fd)
            finally:
                if fd >= 0:
                    os.close(fd)
                # Unknown completion keeps its same-stage evidence. Never
                # delete the only copy merely because the caller saw an error.
                os.fsync(self._fd)
            # Verify the actual committed inode and canonical bytes before any
            # caller records its opaque reference in the database.
            original, _ = self._restore(binding)
            return original

    def read(self, reference: NativeUploadRef, binding: NativeUploadBinding) -> UploadedMedia:
        with self._guard():
            if not isinstance(reference, NativeUploadRef):
                raise NativeVaultError("invalid_native_vault_reference")
            replace(reference)
            found, media = self._restore(binding)
            if found != reference:
                raise NativeVaultError("native_receipt_binding_changed")
            return media

    def recover_stage(self, binding: NativeUploadBinding) -> tuple[NativeUploadRef, UploadedMedia]:
        """DB commit unknown: recover same stage, never generate an upload handle."""
        with self._guard():
            return self._restore(binding)

    def delete(self, reference: NativeUploadRef, binding: NativeUploadBinding):
        """Host-only explicit removal. Root must retain its stage tombstone."""
        with self._guard():
            # Allow removal at expiry, but require the exact persisted namespace
            # and DB commitment; do not accept an arbitrary filename to unlink.
            description = _binding(binding)
            if not isinstance(reference, NativeUploadRef) or _locator(description) != reference.credential_ref:
                raise NativeVaultError("native_receipt_binding_changed")
            replace(reference)
            try:
                raw = self._read_bytes(reference.credential_ref)
            except FileNotFoundError:
                try:
                    raw = self._read_bytes(reference.credential_ref, staging=True)
                except FileNotFoundError:
                    return
            data = json.loads(raw)
            if (data.get("binding") != description or data.get("binding_digest") != reference.binding_digest
                    or data.get("receipt", {}).get("reusable_until") != reference.expires_at
                    or hashlib.sha256(_canonical({"binding": data["binding"], "receipt": data["receipt"]})).hexdigest() != reference.binding_digest):
                raise NativeVaultError("native_receipt_binding_changed")
            for name in (reference.credential_ref + ".json", _staging_name(reference.credential_ref)):
                try:
                    os.unlink(name, dir_fd=self._fd)
                except FileNotFoundError:
                    pass
            os.fsync(self._fd)

    def collect_expired(self, *, after_ref=None, limit=100):
        """Bounded metadata page; immutable stage tombstones remain root-owned.

        Names are enumerated in this private directory; host quotas bound its
        size, not this adapter. Corrupt private files retain recovery opportunity
        for max_private_ttl after their filesystem mtime, then are discarded;
        unsafe files stay pinned for operator review. No receipt is reconstructed
        from a raw URL or from the model.
        """
        if (after_ref is not None and (type(after_ref) is not str or not _REF.fullmatch(after_ref))) or type(limit) is not int or not 1 <= limit <= 100:
            raise NativeVaultError("invalid_native_vault_collection")
        with self._guard():
            candidates = set()
            for name in os.listdir(self._fd):
                locator = name[:-5] if name.endswith(".json") else name[8:-4] if name.startswith(".upload-") and name.endswith(".tmp") else ""
                if _REF.fullmatch(locator) and (after_ref is None or locator > after_ref):
                    candidates.add(locator)
            names = sorted(candidates)[:limit + 1]
            now, removed, skipped = self._now(), 0, 0
            for locator in names[:limit]:
                try:
                    try:
                        raw = self._read_bytes(locator)
                    except FileNotFoundError:
                        raw = self._read_bytes(locator, staging=True)
                    data = json.loads(raw)
                    received = _time(data["receipt"]["received_at"])
                    expiry = _time(data["receipt"]["reusable_until"])
                    if (data["credential_ref"] != locator or not received < expiry <= received + self.max_private_ttl
                            or _locator(data["binding"]) != locator
                            or hashlib.sha256(_canonical({"binding": data["binding"], "receipt": data["receipt"]})).hexdigest()
                               != data["binding_digest"]):
                        raise NativeVaultError("native_receipt_integrity_failure")
                    if expiry <= now:
                        for name in (locator + ".json", _staging_name(locator)):
                            try:
                                os.unlink(name, dir_fd=self._fd)
                            except FileNotFoundError:
                                pass
                        removed += 1
                except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    if self._purge_old_corrupt(locator, now):
                        removed += 1
                    else:
                        skipped += 1
            if removed:
                os.fsync(self._fd)
            return {"scanned": min(len(names), limit), "removed": removed, "skipped": skipped,
                    "next_ref": names[limit - 1] if len(names) > limit else None}

    def _purge_old_corrupt(self, locator, now):
        """No recovery from malformed bytes; bounded retention for private files.

        Filesystem mtime is only a conservative cleanup bound, never a source of
        renewed upload validity. Unsafe ACLs, symlinks and foreign hard links
        remain untouched. Host quotas/operator cleanup handle those violations.
        """
        try:
            names = []
            for staging in (False, True):
                name = _staging_name(locator) if staging else locator + ".json"
                try:
                    self._read_bytes(locator, staging=staging)
                except FileNotFoundError:
                    continue
                info = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
                if not math.isfinite(info.st_mtime) or info.st_mtime < 0 or now - info.st_mtime < self.max_private_ttl:
                    return False
                names.append(name)
            for name in names:
                os.unlink(name, dir_fd=self._fd)
            return bool(names)
        except (OSError, ValueError, TypeError):
            return False
