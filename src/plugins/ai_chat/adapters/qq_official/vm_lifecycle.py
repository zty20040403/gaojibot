"""Bind the account runtime's authenticated endpoints to owned conversation VMs."""
import asyncio
from pathlib import Path
import time

from ...background_tools import ContainerToolSession, workspace_owner
from ...platform_types import EndpointKey
from .browser_runtime import BrowserRuntime
from .lease import assert_epoch
from .vm_api import VMAPIAuthority, VMWriteAdmission
from .vm_credentials import VMCredentials
from .vm_http import VMHTTP
from .vm_sessions import VMSessions

MAX_RESIDENT_CONTAINERS = 12
WAKE_WINDOW_SECONDS = 600
MAX_WAKES_PER_WINDOW = 12
UNIT_EXTS = ('.service', '.socket', '.timer', '.path', '.target')


class OfficialVMLifecycle:
    def __init__(self, *, transaction, lease, private_admission, manager, calls, gateway, logger, clock, runtime, app):
        self.transaction, self.lease, self.manager, self.calls = transaction, lease, manager, calls
        self.gateway, self.logger, self.app = gateway, logger, app
        self.clock = clock
        self.ask_handler = None
        self.wake_handler = None
        self.resident_sandboxes: set[str] = set()
        self.wake_tasks: set[asyncio.Task] = set()
        self.wake_timestamps: dict[str, list[float]] = {}
        self.store = VMSessions(transaction, lease, private_admission=private_admission, clock=clock)
        self.credentials = VMCredentials(self.store, manager, calls, port=gateway.port)
        self.api = VMAPIAuthority(self.credentials, actions=VMWriteAdmission(self.store))
        self.browser = BrowserRuntime(self)
        self.http = VMHTTP(self, runtime, app, logger)
        self.closed, self.restoring = False, None

    def route(self, owner):
        with self.transaction() as cursor:
            assert_epoch(cursor, self.lease)
            rows = cursor.execute(
                'SELECT endpoint_key,generation,route_epoch FROM qq_official_endpoints WHERE account_key=?',
                (self.lease.account_key,),
            ).fetchall()
            for row in rows:
                endpoint = EndpointKey.parse(row['endpoint_key'])
                if workspace_owner(endpoint) == owner:
                    self.store._route(cursor, endpoint)
                    return endpoint, (row['generation'], row['route_epoch'])
        raise PermissionError('vm_endpoint_unavailable')

    def _disk_systemd_unit_count(self, sandbox_id: str) -> int:
        systemd_dir = Path('/var/lib/gaoji-ct') / sandbox_id / 'work' / '.gaoji' / 'systemd'
        try:
            if not systemd_dir.is_dir():
                return 0
            return sum(
                1
                for entry in systemd_dir.iterdir()
                if entry.name.endswith(UNIT_EXTS) and not entry.is_symlink() and entry.is_file()
            )
        except OSError:
            return 0

    async def _set_resident_metadata(self, owner: str, sandbox_id: str, count: int) -> None:
        if not hasattr(self.manager, '_metadata') or not hasattr(self.manager, '_save_metadata'):
            return
        try:
            meta = await self.manager._metadata(owner, sandbox_id)
            meta['resident_units'] = int(count)
            self.manager._save_metadata(sandbox_id, meta)
        except Exception:
            pass

    async def keepalive(self, scope, count: int) -> dict[str, object]:
        await scope.current()
        endpoint = scope.identity.endpoint
        owner = workspace_owner(endpoint)
        sid = str(getattr(scope.identity, 'sandbox_id', '') or '')
        if not sid and hasattr(self.manager, 'ensure_default'):
            sb = await self.manager.ensure_default(owner)
            sid = str(sb.get('sandbox_id') or '')
        count = max(0, int(count))
        await self._set_resident_metadata(owner, sid, count)
        if count <= 0:
            self.resident_sandboxes.discard(sid)
            return {'ok': True, 'resident': False, 'said': '已清空常驻单元，容器空闲后将正常休眠。'}
        if sid in self.resident_sandboxes or len(self.resident_sandboxes) < MAX_RESIDENT_CONTAINERS:
            if sid:
                self.resident_sandboxes.add(sid)
            return {
                'ok': True,
                'resident': True,
                'said': f'已登记 {count} 个常驻单元，容器将保持运行（当前常驻容器 {len(self.resident_sandboxes)}/{MAX_RESIDENT_CONTAINERS}）。',
            }
        return {
            'ok': True,
            'resident': False,
            'said': f'常驻容器数量已达上限（{MAX_RESIDENT_CONTAINERS} 个），空闲时本容器仍可能休眠。',
        }

    async def wake(self, scope, text: str, *, unit: str = '') -> dict[str, object]:
        await scope.current()
        clean = (text or '').strip()[:4000]
        if not clean:
            raise ValueError('唤醒内容不能为空。')
        if self.wake_handler is None:
            raise RuntimeError('当前环境未绑定唤醒处理器。')
        endpoint = scope.identity.endpoint
        now = float(self.clock() if callable(self.clock) else time.time())
        recent = [ts for ts in self.wake_timestamps.get(endpoint.key, ()) if now - ts < WAKE_WINDOW_SECONDS]
        if len(recent) >= MAX_WAKES_PER_WINDOW:
            return {'ok': False, 'error': 'units_wake_rate_limited', 'said': '唤醒过于频繁，已限流。'}
        recent.append(now)
        self.wake_timestamps[endpoint.key] = recent
        task = asyncio.create_task(self._run_wake(endpoint, clean, unit=unit))
        self.wake_tasks.add(task)
        task.add_done_callback(self.wake_tasks.discard)
        return {'ok': True, 'accepted': True}

    async def _run_wake(self, endpoint, text: str, *, unit: str = '') -> None:
        try:
            await self.wake_handler(endpoint, text, unit=unit)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception('Container unit wake delivery failed endpoint=%s unit=%s', endpoint.key, unit)

    async def _has_active_guest_jobs(self, sandbox_id: str) -> bool:
        guest_exec = getattr(self.manager, '_guest_exec', None)
        if not callable(guest_exec):
            return False
        try:
            code, out, _ = await guest_exec(
                sandbox_id,
                '/run/current-system/sw/bin/systemctl',
                ['list-units', '--no-legend', '--plain', '--state=active,activating', 'gaoji-cmd-*.service', 'gaoji-job-*.service'],
                timeout=10,
            )
            return code == 0 and bool(out.strip())
        except Exception:
            return False

    async def ready(self, owner, sandbox_id, *, automatic=False):
        if self.closed:
            raise PermissionError('vm_service_closed')
        endpoint, route = await self.calls(self.route, owner)
        current = await self.credentials.prepare(endpoint, sandbox_id, expected_route=route)
        self.api.rotated(current)
        # Re-register resident status if this workspace carries persistent units.
        meta_count = 0
        if hasattr(self.manager, '_metadata'):
            try:
                meta = await self.manager._metadata(owner, sandbox_id)
                meta_count = int(meta.get('resident_units') or 0)
            except Exception:
                meta_count = 0
        disk_count = self._disk_systemd_unit_count(sandbox_id)
        if (meta_count > 0 or disk_count > 0) and (
            sandbox_id in self.resident_sandboxes or len(self.resident_sandboxes) < MAX_RESIDENT_CONTAINERS
        ):
            self.resident_sandboxes.add(sandbox_id)
        return current

    async def starting(self, owner, sandbox_id, *, automatic=False):
        if self.closed:
            raise PermissionError('vm_service_closed')
        await self.calls(self.route, owner)

    async def failed(self, owner, sandbox_id):
        return None

    async def stopped(self, owner, sandbox_id):
        return None

    async def stopping(self, owner, sandbox_id, *, automatic=False):
        if automatic:
            if sandbox_id in self.resident_sandboxes:
                return False
            if await self._has_active_guest_jobs(sandbox_id):
                return False
        if not await self.calls(self.store.revoke_vm, sandbox_id, idle_owner=owner if automatic else None):
            return False
        self.resident_sandboxes.discard(sandbox_id)
        self.api.revoke(sandbox_id)
        return True

    async def start(self):
        if getattr(self.manager, 'conversation_lifecycle', None) not in (None, self):
            raise RuntimeError('vm_lifecycle_already_attached')
        self.manager.conversation_lifecycle = self
        self.gateway.vm_authority = self.api
        self.gateway.vm_handler = self.http.handle
        self.gateway.vm_tool_session = lambda scope: ContainerToolSession(
            self.app, scope, self.http, ask_handler=self.ask_handler, units_handler=self,
        )
        await self.gateway.start()
        self.restoring = asyncio.create_task(self.restore())

    async def restore(self):
        try:
            items = sorted(await self.manager._all_metadata(), key=lambda row: str(row['id']))
            stopped_with_units: list[tuple[str, str]] = []
            for item in items:
                owner, sid = str(item.get('owner_ref', '')), str(item['id'])
                if not owner.startswith('qqo:conversation:') or self.closed:
                    continue
                try:
                    if await self.manager._running(sid):
                        await self.ready(owner, sid, automatic=True)
                    elif int(item.get('resident_units') or 0) > 0 or self._disk_systemd_unit_count(sid) > 0:
                        stopped_with_units.append((owner, sid))
                except Exception as error:
                    self.logger.warning('VM credential restore failed vm=%s error=%s', sid, type(error).__name__)
            for owner, sid in stopped_with_units:
                if self.closed or len(self.resident_sandboxes) >= MAX_RESIDENT_CONTAINERS:
                    break
                try:
                    await self.manager.start_owned(owner, sid, automatic=True)
                    self.resident_sandboxes.add(sid)
                except Exception as error:
                    self.logger.warning('Resident container boot failed vm=%s error=%s', sid, type(error).__name__)
        except Exception as error:
            self.logger.warning('VM credential scan failed error=%s', type(error).__name__)

    async def close(self):
        self.closed = True
        if getattr(self.manager, 'conversation_lifecycle', None) is self:
            self.manager.conversation_lifecycle = None
        if self.restoring is not None:
            self.restoring.cancel()
            await asyncio.gather(self.restoring, return_exceptions=True)
        for task in tuple(self.wake_tasks):
            task.cancel()
        if self.wake_tasks:
            await asyncio.gather(*tuple(self.wake_tasks), return_exceptions=True)
        await self.api.close()
