"""Source binding for official conversation workspaces."""
from .file_artifacts import current_source, _require


class GenerationBindingAdapter:
    def __init__(self,source,store,calls,*,account_limit=16,actor_limit=4):
        self.source,self.store,self.calls=source,store,calls
        self.account_limit,self.actor_limit=account_limit,actor_limit
        self.endpoint,self.actor=source.endpoint,source.actor
        self.source_key,self.deadline_at=source.source_key,source.deadline_at
        self.source_native_message_id=source.source_native_message_id

    def allows_missing_native_source(self):
        _require(self.source)
        return (self.source.source_native_message_id is None and self.source.source_execution_id is None
            and self.source.task_id is not None and self.source.source_migration_item_id is not None
            and getattr(self.source.extension,'kind',None) in {'migration','project'})

    def matches_tool_source(self, source):
        from src.plugins.ai_chat.platform_types import BackgroundToolSource
        _require(self.source)
        kind = 'project' if getattr(self.source.extension,'kind',None)=='project' else 'subagent'
        return (isinstance(source,BackgroundToolSource) and self.source.task_id is not None
            and source.endpoint==self.endpoint and source.actor==self.actor
            and source.source_kind==kind and source.source_ref==f'task:{self.source.task_id}'
            and source.source_revision==1)

    async def assert_current(self):
        def check():
            with self.store.transaction() as cursor:
                current_source(cursor,self.source,self.store._now())
        await self.calls(check)

