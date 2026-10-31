"""One durable job, one engine run, one report returned to the foreground."""
from __future__ import annotations

import asyncio
import json
import time

from .agent_loop import AgentTrace

ACTIVE = ('queued', 'running')


class BackgroundJobs:
    def __init__(self, jobs, *, clock=time.time):
        self.jobs, self.clock = jobs, clock
        self.transaction = jobs._transaction

    def submit(self, cursor, *, kind, key, source, task, title, profile):
        # Serialize the one documented limit across accounts and simultaneous turns.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('gaoji-background-agents',0))")
        existing = cursor.execute('''SELECT b.* FROM background_jobs b JOIN durable_jobs j USING(job_id)
            WHERE j.idempotency_key=?''', (key,)).fetchone()
        if existing:
            return dict(existing)
        count = cursor.execute("SELECT count(*) AS n FROM background_jobs WHERE state IN ('queued','running')").fetchone()['n']
        if count >= 3:
            raise ValueError('已有三个后台任务在运行，请等一个完成或用 agent_cancel 停止任务。')
        now = int(self.clock())
        job, _ = self.jobs.enqueue(kind=kind, idempotency_key=key, scope_key=source['scope_key'],
            max_attempts=1, now=now, transaction_cursor=cursor)
        fields = {key: source[key] for key in (
            'account_key', 'endpoint_key', 'actor_key', 'scope_key', 'source_execution_id',
            'canonical_message_id', 'native_message_id', 'endpoint_generation', 'route_epoch')}
        fields.update(job_id=job.job_id, task=task, title=title or task[:80], profile=profile,
                      created_at=now, deadline_at=now + 7200)
        row = cursor.execute('INSERT INTO background_jobs (' + ','.join(fields) + ') VALUES (' +
            ','.join('?' for _ in fields) + ') RETURNING *', tuple(fields.values())).fetchone()
        return dict(row)

    def get(self, job_id):
        with self.transaction() as cursor:
            row = cursor.execute('SELECT * FROM background_jobs WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self, endpoint=None, actor=None, *, limit=20):
        with self.transaction() as cursor:
            where = 'WHERE endpoint_key=? AND actor_key=?' if endpoint is not None else ''
            args = (endpoint, actor, limit) if endpoint is not None else (limit,)
            rows = cursor.execute(f'SELECT * FROM background_jobs {where} ORDER BY created_at DESC LIMIT ?', args).fetchall()
        return [dict(row) for row in rows]

    def update(self, job_id, **fields):
        with self.transaction() as cursor:
            cursor.execute('UPDATE background_jobs SET ' + ','.join(key + '=?' for key in fields) +
                           ' WHERE job_id=?', (*fields.values(), job_id))

    def steer(self, job_id, endpoint, actor, message):
        with self.transaction() as cursor:
            row = cursor.execute('''SELECT steering_json FROM background_jobs
                WHERE job_id=? AND endpoint_key=? AND actor_key=? AND state IN ('queued','running') FOR UPDATE''',
                (job_id, endpoint, actor)).fetchone()
            if row is None:
                raise ValueError('找不到你在当前会话里运行的任务。')
            messages = json.loads(row['steering_json']) + [message]
            cursor.execute('UPDATE background_jobs SET steering_json=? WHERE job_id=?',
                           (json.dumps(messages, ensure_ascii=False), job_id))

    def cancel(self, job_id, endpoint, actor):
        with self.transaction() as cursor:
            row = cursor.execute('''UPDATE background_jobs SET state='cancelled', finished_at=?, error='用户取消了任务'
                WHERE job_id=? AND endpoint_key=? AND actor_key=? AND state IN ('queued','running') RETURNING job_id''',
                (int(self.clock()), job_id, endpoint, actor)).fetchone()
        if row:
            self.jobs.cancel(job_id)
        return row is not None

    def recover(self, account_key):
        with self.transaction() as cursor:
            cursor.execute('''UPDATE background_jobs SET state='failed', error='服务重启，任务已中断；工作区文件保留。',
                finished_at=? WHERE account_key=? AND state='running' ''', (int(self.clock()), account_key))

    def pending_results(self, account_key):
        with self.transaction() as cursor:
            cursor.execute("""UPDATE background_jobs b SET state='failed',
                error='任务尚未开始，后台队列已终止；请重新发起。', finished_at=?
                FROM durable_jobs j WHERE b.job_id=j.job_id AND b.account_key=? AND b.state='queued'
                AND j.status IN ('succeeded','failed','cancelled')""", (int(self.clock()), account_key))
            rows = cursor.execute('''SELECT * FROM background_jobs WHERE account_key=?
                AND state NOT IN ('queued','running') AND delivered_at IS NULL ORDER BY job_id LIMIT 20''',
                (account_key,)).fetchall()
        return [dict(row) for row in rows]


class BackgroundRunner:
    """Transport-free runner: the engine does the work, this keeps the job's state honest."""
    def __init__(self, store, *, calls, session_factory, save_report, ask_main, record_usage,
                 logger, engine, clock=time.time):
        self.store, self.calls, self.session_factory = store, calls, session_factory
        self.save_report, self.ask_main, self.record_usage = save_report, ask_main, record_usage
        self.logger, self.engine, self.clock = logger, engine, clock

    async def execute(self, durable_job):
        row = await self.calls(self.store.get, durable_job.job_id)
        if row['state'] != 'queued':
            return {'state': row['state']}
        await self.calls(self.store.update, row['job_id'], state='running', started_at=int(self.clock()))
        steering_offset, warned, asking = 0, False, False
        notes = []
        trace = AgentTrace()

        async def feedback(*, warn=True):
            nonlocal steering_offset, warned
            current = await self.calls(self.store.get, row['job_id'])
            if current['state'] == 'cancelled':
                raise asyncio.CancelledError
            if warn and asking:
                # While a question waits, the next message is its answer.
                return []
            messages = json.loads(current['steering_json'])
            fresh, steering_offset = messages[steering_offset:], len(messages)
            if warn and not warned and self.clock() >= row['deadline_at'] - 300:
                warned = True
                fresh.append('还剩五分钟，请停止扩展任务，保存文件并整理结果和未完成事项。')
            return fresh

        async def question(text):
            nonlocal asking
            asking = True
            try:
                await self.ask_main(row, text)
                until = min(self.clock() + 600, row['deadline_at'])
                while self.clock() < until:
                    messages = await feedback(warn=False)
                    if messages:
                        return '\n'.join(messages)
                    await asyncio.sleep(min(2, max(0, until - self.clock())))
            finally:
                asking = False
            assumption = '前台十分钟未回答，请自行判断继续，在报告中写清采用的假设。'
            notes.append(assumption)
            return assumption

        async def progress(name):
            await self.calls(self.store.update, row['job_id'], last_tool=name)

        session = None
        state, error, report = 'completed', '', ''
        try:
            session = await self.session_factory(row, durable_job, question)
            # The engine stops itself at the deadline; this only bounds a stuck one.
            async with asyncio.timeout(max(0, row['deadline_at'] - self.clock()) + 180):
                report = await self.engine(row, session, feedback=feedback, progress=progress, trace=trace)
        except asyncio.CancelledError:
            current = await self.calls(self.store.get, row['job_id'])
            state = 'cancelled' if current['state'] == 'cancelled' else 'failed'
            error = '用户取消了任务。' if state == 'cancelled' else '服务停止，任务已中断；工作区文件保留。'
            raise
        except TimeoutError:
            state, error = 'failed', '任务超过两小时上限且没能正常停下；请查看工作区中的现有文件。'
        except Exception as exc:
            state, error = 'failed', f'后台任务失败：{type(exc).__name__}: {exc}'
            self.logger.exception('Background job %s failed', row['job_id'])
        finally:
            report = '\n\n'.join(part for part in (report, error, '\n'.join(notes)) if part)
            await self.calls(self.store.update, row['job_id'], state=state, error=error,
                             report=report, finished_at=int(self.clock()))
            try:
                await self.save_report(row, report)
            except Exception as exc:
                # The report is already stored and is what gets delivered; the
                # copy in /work is a convenience, so its loss does not fail the job.
                self.logger.exception('Could not save background report to VM')
                await self.calls(self.store.update, row['job_id'],
                                 report=report + f'\n\n（报告未能写入 VM：{type(exc).__name__}）')
            await self.record_usage(row, trace)
            if session is not None:
                await session.close()
        return {'state': state}
