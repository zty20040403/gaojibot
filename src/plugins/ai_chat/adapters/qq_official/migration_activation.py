"""Atomic conversion and release of reviewed intents in existing domain stores.

This host port is called only after the console binds its current admin request,
or to recover an already completed migration. It never supplies a new identity,
deadline, queue or execution grant of its own.
"""
from .migration_types import MigrationDenied


class MigrationActivation:
    def __init__(self, *, journal, domain_for_item):
        if not callable(domain_for_item):
            raise ValueError('migration_domain_resolver_required')
        self.journal, self.domain_for_item = journal, domain_for_item

    def _items(self, cursor, migration_id, phases):
        journal, route = self.journal._load(cursor, migration_id)
        if route['phase'] not in phases:
            raise MigrationDenied('migration_activation_phase')
        items = cursor.execute('''SELECT * FROM migration_items WHERE migration_id=?
            AND parent_item_id IS NULL AND classification='unsent' ORDER BY item_id FOR UPDATE''',
            (journal['migration_id'],)).fetchall()
        if len(items) > 100:
            raise MigrationDenied('migration_activation_limit')
        return items

    def _domain(self, cursor, item):
        domain = self.domain_for_item(cursor, dict(item))
        if not all(callable(getattr(domain, name, None)) for name in ('prepare', 'activate')):
            raise MigrationDenied('migration_domain_host_unavailable')
        return domain

    def prepare_migration(self, cursor, *, migration_id, now):
        items = self._items(cursor, migration_id, {'switching', 'verifying'})
        for item in items:
            if item['state'] not in {'reviewed', 'prepared'}:
                raise MigrationDenied('migration_item_not_preparable')
            proof = self.journal.issue_item(cursor, item_id=item['item_id'], operation='prepare', now=now)
            self._domain(cursor, item).prepare(cursor, proof, now=now)
            # The same transaction must contain the exact source freeze, domain
            # binding and parked job (or frozen recurring schedule).
            current = cursor.execute('SELECT state FROM migration_items WHERE item_id=?', (item['item_id'],)).fetchone()
            if current is None or current['state'] != 'prepared':
                raise MigrationDenied('migration_domain_not_prepared')
            self.journal.issue_item(cursor, item_id=item['item_id'], operation='prepare', now=now)
        return len(items)

    def require_prepared(self, cursor, *, migration_id, now):
        items = self._items(cursor, migration_id, {'verifying'})
        for item in items:
            if item['state'] != 'prepared':
                raise MigrationDenied('migration_domain_not_prepared')
            self.journal.issue_item(cursor, item_id=item['item_id'], operation='prepare', now=now)
            self._domain(cursor, item)
        return len(items)

    def activate_migration(self, cursor, *, migration_id, now):
        items = self._items(cursor, migration_id, {'completed'})
        activated = 0
        for item in items:
            if item['state'] in {'active', 'expired'}:
                # An already running/finished target is not reactivated or
                # re-rendered by a repeated control request or process restart.
                continue
            if item['state'] != 'prepared':
                raise MigrationDenied('migration_domain_not_prepared')
            proof = self.journal.issue_item(cursor, item_id=item['item_id'], operation='activate', now=now)
            self._domain(cursor, item).activate(cursor, proof, now=now)
            current = cursor.execute('SELECT state FROM migration_items WHERE item_id=?', (item['item_id'],)).fetchone()
            if current is None or current['state'] != 'active':
                raise MigrationDenied('migration_domain_not_activated')
            activated += 1
        return activated
