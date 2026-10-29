"""Console authentication without implicit OneBot identity or approval transport."""
from __future__ import annotations
import os
from src.bot_security.service import MobileAuthorization
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


class ConsoleAuthorization(MobileAuthorization):
    """The console can authenticate; only an epoch-bound transport may approve."""
    async def propose(self,*args,**kwargs):
        raise SecurityError('official_approval_source_required')

    async def run_forever(self):
        raise SecurityError('console_approval_worker_forbidden')


def build_console_authorization(settings,database):
    path=str(getattr(settings,'admin_secret_file','') or '')
    if not path:return None
    if database is None:raise SecurityError('console_storage_unavailable')
    store=SecurityStore(database,read_console_secret(path))
    def no_bot():raise SecurityError('legacy_transport_disabled')
    async def no_send(*args,**kwargs):raise SecurityError('legacy_transport_disabled')
    return ConsoleAuthorization(store,sender=no_send,bot_selector=no_bot)
