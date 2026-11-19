"""Console authentication without implicit OneBot identity or approval transport."""
from __future__ import annotations
import os
from src.bot_security.store import SecurityError, SecurityStore
from src.qq_official.credentials import verify_private_file, CredentialError


def read_console_secret(path):
    fd=None
    try:
        if os.name=='nt':
            from src.qq_official.windows_private import open_windows_private_readonly
            fd=open_windows_private_readonly(path)
        else:
            fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        verify_private_file(fd)
        if not 32<=os.fstat(fd).st_size<=16384:raise ValueError
        with os.fdopen(fd,'rb',closefd=False) as stream:value=stream.read(16385)
        if not 32<=len(value)<=16384:raise ValueError
        return value
    except (OSError,ValueError,CredentialError):
        raise SecurityError('console_secret_unavailable') from None
    finally:
        if fd is not None:os.close(fd)


def build_console_store(settings,database):
    path=str(getattr(settings,'admin_secret_file','') or '')
    if not path:return None
    if database is None:raise SecurityError('console_storage_unavailable')
    return SecurityStore(database,read_console_secret(path))
