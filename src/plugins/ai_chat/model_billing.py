"""Historical provider receipts; isolated from conservative token quota rows.

Only trusted host factories construct sources. The caller performs admission
before begin; finish records a receipt even after cancellation/revocation. No
request body, response text, URL, API key or VM-reported count is persisted.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import secrets
import time

from .billing_sources import metadata
from .model_pricing import ModelPricing
from .model_usage import TokenUsage
from .platform_types import ActorKey,EndpointKey


@dataclass(frozen=True)
class BillingSource:
    endpoint: EndpointKey
    purpose: str
    actor: ActorKey | None = None
    turn_id: int | None = None
    background_job_id: int | None = None
    program_wake_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.endpoint,EndpointKey) or self.endpoint.account.platform!='qq-official':
            raise ValueError('billing_requires_physical_endpoint')
        if self.purpose not in {'turn','proactive','ocr','image_caption','video','background_agent','program_wake','memory','semantic_index'}:
            raise ValueError('invalid_billing_purpose')
        if self.actor is not None and (not isinstance(self.actor,ActorKey) or self.actor.identity_kind=='bot'
                or not self.actor.matches_endpoint(self.endpoint)):
            raise ValueError('billing_actor_endpoint_mismatch')
        if self.program_wake_id is not None and (self.actor is not None or self.background_job_id is not None):
            raise ValueError('program_billing_cannot_impersonate_human_job')


class ModelBilling:
    def __init__(self,database,*,clock=time.time):
        self.database,self.clock = database,clock

    @contextmanager
    def transaction(self):
        connection = self.database.store_connection()
        try:
            yield connection.cursor()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def begin(self,source,profile,*,transaction_cursor=None):
        if transaction_cursor is None:
            with self.transaction() as cursor:return self.begin(source,profile,transaction_cursor=cursor)
        cursor = transaction_cursor
        if not isinstance(source,BillingSource):raise ValueError('billing_requires_host_source')
        if source.background_job_id is not None:
            job = cursor.execute('SELECT endpoint_key,actor_key FROM background_jobs WHERE job_id=?',
                (source.background_job_id,)).fetchone()
            if job is None or (job['endpoint_key'],job['actor_key'])!=(source.endpoint.key,source.actor.key if source.actor else None):
                raise ValueError('billing_job_source_mismatch')
        if source.program_wake_id is not None:
            wake = cursor.execute('SELECT endpoint_key FROM qq_official_program_wakes WHERE wake_id=?',
                (source.program_wake_id,)).fetchone()
            if wake is None or wake['endpoint_key']!=source.endpoint.key:raise ValueError('billing_wake_source_mismatch')
        if source.turn_id is not None:
            turn = cursor.execute('SELECT endpoint_key,requester_actor_key FROM qq_official_turn_executions WHERE journal_turn_id=?',
                (source.turn_id,)).fetchone()
            if turn is None or (turn['endpoint_key'],turn['requester_actor_key'])!=(source.endpoint.key,source.actor.key if source.actor else None):
                raise ValueError('billing_turn_source_mismatch')
        call_id = secrets.token_hex(16)
        cursor.execute('''INSERT INTO model_usage_calls(call_id,endpoint_key,scope_key,actor_key,purpose,turn_id,
            background_job_id,program_wake_id,provider,model,profile,pricing_json,started_at,source_metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (call_id,source.endpoint.key,source.endpoint.scope.key,source.actor.key if source.actor else None,source.purpose,
             source.turn_id,source.background_job_id,source.program_wake_id,profile.provider,profile.model,profile.name,
             json.dumps(profile.pricing.to_mapping(),sort_keys=True) if profile.pricing else None,int(self.clock()),metadata(cursor,source)))
        return call_id

    def finish(self,call_id,usage):
        if not isinstance(usage,TokenUsage):raise ValueError('billing_requires_provider_receipt')
        with self.transaction() as cursor:
            row = cursor.execute('SELECT * FROM model_usage_calls WHERE call_id=? FOR UPDATE',(call_id,)).fetchone()
            if row is None or row['finished_at'] is not None:return False
            pricing = ModelPricing.from_mapping(json.loads(row['pricing_json'])) if row['pricing_json'] else None
            quote = usage.quote(pricing)
            cursor.execute('''UPDATE model_usage_calls SET input_tokens=?,output_tokens=?,cached_tokens=?,cache_write_tokens=?,
                cost_usd=?,cost_complete=?,finished_at=? WHERE call_id=?''',
                (usage.input_tokens,usage.output_tokens,usage.cached_tokens,usage.cache_write_tokens,
                 quote.usd,quote.complete,max(int(self.clock()),row['started_at']),call_id))
            return True
