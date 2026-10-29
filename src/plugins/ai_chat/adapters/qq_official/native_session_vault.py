"""Private immutable prepare-session evidence, separate from file_info vault.

Only actual host uploader callbacks save. The DB owns the once-only prepare
stage; a valid private file is evidence, never user authority or permission to
repeat a network call. Signed PUT URLs never enter a DB row, repr or exception.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import os

from src.qq_official.credentials import verify_private_file

from .native_media import CONTRACT, NativeLimits, PreparedUpload, parse_prepare
from .native_part_transport import MultipartConfiguration
from .native_vault import (NativeUploadBinding, NativeUploadRef, NativeUploadVault,
    NativeVaultError, _binding, _canonical, _digest, _locator, _staging_name, _time, _FILE_LIMIT)


@dataclass(frozen=True,repr=False)
class NativeSessionBinding:
    upload: NativeUploadBinding
    deadline_at: int
    configuration: MultipartConfiguration

    def __post_init__(self):
        if type(self.upload) is not NativeUploadBinding or type(self.configuration) is not MultipartConfiguration:
            raise NativeVaultError('invalid_upload_session_binding')
        replace(self.upload)
        _time(self.deadline_at)
        if self.upload.artifact.file_type!=4:
            raise NativeVaultError('invalid_upload_session_binding')


@dataclass(frozen=True,repr=False)
class NativeSessionReceipt:
    prepared: PreparedUpload
    received_at: int
    reusable_until: int


def _description(binding):
    if type(binding) is not NativeSessionBinding:
        raise NativeVaultError('invalid_upload_session_binding')
    replace(binding)
    return dict(namespace='qq-native-prepare-v1',upload=_binding(binding.upload),
        deadline_at=binding.deadline_at,allowed_part_hosts=sorted(binding.configuration.allowed_part_hosts),
        max_parts=binding.configuration.max_parts,max_session_seconds=binding.configuration.max_session_seconds)


def _payload(prepared):
    return dict(upload_id=prepared.upload_id,block_size=str(prepared.block_size),
        parts=[dict(index=p.index,block_size=str(p.length),presigned_url=p.presigned_url) for p in prepared.parts],
        upload_config=dict(concurrency=prepared.concurrency,retry_timeout=prepared.retry_timeout_seconds,
            retry_delay=prepared.retry_delay_seconds))


class NativeSessionVault(NativeUploadVault):
    """Reuse the already-tested descriptor/ACL/fsync/immutable-write primitive.

    The receipt schema/namespace is independent. Existing NativeUploadVault and
    FileCredentialVault contracts are not widened. Keep a separate directory.
    """
    def __repr__(self):
        return 'NativeSessionVault(<protected>)'

    def _encode(self,binding,receipt):
        description=_description(binding)
        if type(receipt) is not NativeSessionReceipt or type(receipt.prepared) is not PreparedUpload:
            raise NativeVaultError('invalid_upload_session_receipt')
        prepared=receipt.prepared
        _time(receipt.received_at);_time(receipt.reusable_until)
        try:
            if prepared.contract!=CONTRACT:
                raise ValueError
            body=_payload(prepared)
            rebuilt=parse_prepare(200,body,endpoint=binding.upload.endpoint,artifact=binding.upload.artifact,
                allowed_hosts=binding.configuration.allowed_part_hosts,
                limits=NativeLimits(max_parts=binding.configuration.max_parts))
            if (rebuilt!=prepared or not receipt.received_at<receipt.reusable_until<=min(binding.deadline_at,
                    receipt.received_at+binding.configuration.max_session_seconds,
                    receipt.received_at+prepared.retry_timeout_seconds)):
                raise ValueError
        except (TypeError,ValueError,AttributeError):
            raise NativeVaultError('invalid_upload_session_receipt') from None
        value=dict(prepared=body,received_at=receipt.received_at,reusable_until=receipt.reusable_until,contract=CONTRACT)
        payload=dict(binding=description,receipt=value)
        digest=hashlib.sha256(_canonical(payload)).hexdigest()
        locator=_locator(description)
        encoded=_canonical(dict(version=1,credential_ref=locator,binding_digest=digest,**payload))
        if len(encoded)>_FILE_LIMIT:
            raise NativeVaultError('upload_session_too_large')
        return NativeUploadRef(locator,digest,receipt.reusable_until),encoded

    def _restore(self,binding):
        description=_description(binding);locator=_locator(description);staging=False
        try:raw=self._read_bytes(locator)
        except FileNotFoundError:raw,staging=self._read_bytes(locator,staging=True),True
        data=json.loads(raw)
        if (type(data) is not dict or set(data)!={'version','credential_ref','binding_digest','binding','receipt'}
                or type(data['version']) is not int or data['version']!=1
                or data['credential_ref']!=locator or data['binding']!=description
                or type(data['receipt']) is not dict
                or set(data['receipt'])!={'prepared','received_at','reusable_until','contract'}
                or data['receipt']['contract']!=CONTRACT):
            raise NativeVaultError('upload_session_binding_changed')
        saved=data['receipt']
        prepared=parse_prepare(200,saved['prepared'],endpoint=binding.upload.endpoint,artifact=binding.upload.artifact,
            allowed_hosts=binding.configuration.allowed_part_hosts,
            limits=NativeLimits(max_parts=binding.configuration.max_parts))
        receipt=NativeSessionReceipt(prepared,saved['received_at'],saved['reusable_until'])
        reference,encoded=self._encode(binding,receipt)
        if (not hmac.compare_digest(reference.binding_digest,_digest(data['binding_digest']))
                or not hmac.compare_digest(encoded,raw)):
            raise NativeVaultError('upload_session_integrity_failure')
        if not receipt.received_at<=self._now()<receipt.reusable_until:
            raise NativeVaultError('upload_session_expired')
        if staging:
            fd=os.open(_staging_name(locator),os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self._fd)
            try:verify_private_file(fd);os.fsync(fd)
            finally:os.close(fd)
            os.link(_staging_name(locator),locator+'.json',src_dir_fd=self._fd,dst_dir_fd=self._fd,follow_symlinks=False)
        try:pending=os.stat(_staging_name(locator),dir_fd=self._fd,follow_symlinks=False)
        except FileNotFoundError:pending=None
        if pending is not None:
            final=os.stat(locator+'.json',dir_fd=self._fd,follow_symlinks=False)
            if (pending.st_dev,pending.st_ino)!=(final.st_dev,final.st_ino):
                raise NativeVaultError('upload_session_integrity_failure')
            os.unlink(_staging_name(locator),dir_fd=self._fd)
        os.fsync(self._fd)
        return reference,receipt

    def delete(self,reference,binding):
        # Explicit exact-binding cleanup; DB stage/session tombstones remain.
        with self._guard():
            description=_description(binding)
            if type(reference) is not NativeUploadRef or _locator(description)!=reference.credential_ref:
                raise NativeVaultError('upload_session_binding_changed')
            replace(reference)
            try:raw=self._read_bytes(reference.credential_ref)
            except FileNotFoundError:
                try:raw=self._read_bytes(reference.credential_ref,staging=True)
                except FileNotFoundError:return
            data=json.loads(raw)
            if (data.get('binding')!=description or data.get('binding_digest')!=reference.binding_digest
                    or data.get('receipt',{}).get('reusable_until')!=reference.expires_at
                    or hashlib.sha256(_canonical(dict(binding=data['binding'],receipt=data['receipt']))).hexdigest()
                       !=reference.binding_digest):
                raise NativeVaultError('upload_session_binding_changed')
            for name in (reference.credential_ref+'.json',_staging_name(reference.credential_ref)):
                try:os.unlink(name,dir_fd=self._fd)
                except FileNotFoundError:pass
            os.fsync(self._fd)
