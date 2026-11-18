"""One durable job, one engine run, one report returned to the foreground."""
from __future__ import annotations

import asyncio
import inspect
import json
import time

from .agent_loop import AgentTrace

ACTIVE = ('queued', 'running')


def outcome_of(report: str) -> str:
    for raw in (report or '').splitlines():
        line = raw.strip().strip('*#').strip()
        if not line:
            continue
        normalized = line.replace(':', '：', 1)
        if normalized == '结果：成功':
            return 'success'
        if normalized == '结果：部分完成':
            return 'partial'
        if normalized == '结果：失败':
            return 'failed'
        return 'unknown'
    return 'unknown'


class BackgroundJobs:
    def __init__(self, jobs, *, clock=time.time):
        self.jobs, self.clock = jobs, clock
        self.transaction = jobs._transaction

    @staticmethod
    def hours(cursor, endpoint):
        row = cursor.execute('SELECT agent_timeout_hours FROM conversation_settings WHERE scope_key=?',
            (endpoint.scope.key,)).fetchone()
        return row['agent_timeout_hours'] if row else 6

    def submit(
        self,
        cursor,
        *,
        kind,
        key,
        source,
        task,
        title,
        profile,
        independent=False,
        hours=6,
        resume_job_id=None,
        workdir='',
    ):
        if type(hours) is not int or not 1 <= hours <= 6:
            raise ValueError('后台任务时限必须是1–6小时。')
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('gaoji-background-agents',0))")
        existing = cursor.execute('''SELECT b.* FROM background_jobs b JOIN durable_jobs j USING(job_id)
            WHERE j.idempotency_key=?''', (key,)).fetchone()
        if existing:
            return dict(existing)
        if resume_job_id is not None:
            prev = cursor.execute(
                'SELECT * FROM background_jobs WHERE job_id=? AND endpoint_key=?',
                (int(resume_job_id), source['endpoint_key']),
            ).fetchone()
            if prev is None:
                raise ValueError(f'当前会话里找不到任务 job#{resume_job_id}。')
            if prev['state'] in ('queued', 'running'):
                raise ValueError(f'job#{resume_job_id} 还在运行，补充要求请直接用 agent_steer。')
            if not title:
                title = prev['title']
        if not independent:
            active = cursor.execute('''SELECT job_id FROM background_jobs
                WHERE endpoint_key=? AND actor_key=? AND state IN ('queued','running') ORDER BY job_id''',
                (source['endpoint_key'], source['actor_key'])).fetchall()
            if active:
                names = '、'.join(f"job#{row['job_id']}" for row in active)
                raise ValueError(f'已有活动任务 {names}，未新建。追问进度请用 agent_status，'
                                 '补充要求请用 agent_steer；目标不明确先问用户。'
                                 '只有用户确实要求独立新任务时才设 independent=true。')
        count = cursor.execute("SELECT count(*) AS n FROM background_jobs WHERE state IN ('queued','running')").fetchone()['n']
        if count >= 3:
            raise ValueError('已有三个后台任务在运行，请等一个完成或用 agent_cancel 停止任务。')
        now = int(self.clock())
        payload = {}
        if resume_job_id is not None:
            payload['resume_job_id'] = int(resume_job_id)
        if workdir:
            payload['workdir'] = str(workdir)
        job, _ = self.jobs.enqueue(
            kind=kind,
            idempotency_key=key,
            payload=payload or None,
            scope_key=source['scope_key'],
            max_attempts=20,
            resume_on_lease_loss=True,
            now=now,
            transaction_cursor=cursor,
        )
        fields = {key: source[key] for key in (
            'account_key', 'endpoint_key', 'actor_key', 'scope_key', 'source_execution_id',
            'canonical_message_id', 'native_message_id', 'endpoint_generation', 'route_epoch')}
        fields.update(conversation_generation=source.get('conversation_generation', 0),
                      job_id=job.job_id, task=task, title=title or task[:80], profile=profile,
                      created_at=now, deadline_at=now + hours * 3600)
        row = cursor.execute('INSERT INTO background_jobs (' + ','.join(fields) + ') VALUES (' +
            ','.join('?' for _ in fields) + ') RETURNING *', tuple(fields.values())).fetchone()
        return dict(row)

    def get(self, job_id):
        with self.transaction() as cursor:
            row = cursor.execute('SELECT * FROM background_jobs WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self, endpoint=None, actor=None, *, limit=20):
        with self.transaction() as cursor:
            if endpoint is not None and actor is not None:
                where, args = 'WHERE endpoint_key=? AND actor_key=?', (endpoint, actor, limit)
            elif endpoint is not None:
                where, args = 'WHERE endpoint_key=?', (endpoint, limit)
            else:
                where, args = '', (limit,)
            rows = cursor.execute(f'''SELECT * FROM background_jobs {where}
                ORDER BY (state IN ('queued','running')) DESC,created_at DESC,job_id DESC LIMIT ?''', args).fetchall()
        return [dict(row) for row in rows]

    def start(self, job_id):
        with self.transaction() as cursor:
            row = cursor.execute(
                "UPDATE background_jobs SET state='running', started_at=COALESCE(started_at, ?) "
                "WHERE job_id=? AND state IN ('queued', 'running') RETURNING *",
                (int(self.clock()), job_id),
            ).fetchone()
        return dict(row) if row else None

    def finish(self, job_id, *, state, error, report):
        with self.transaction() as cursor:
            return cursor.execute('''UPDATE background_jobs SET state=CASE WHEN state='cancelled' THEN state ELSE ? END,
                error=CASE WHEN state='cancelled' THEN error ELSE ? END,report=?,finished_at=? WHERE job_id=? RETURNING state''',
                (state, error, report, int(self.clock()), job_id)).fetchone()['state']

    def update(self, job_id, **fields):
        with self.transaction() as cursor:
            cursor.execute('UPDATE background_jobs SET ' + ','.join(key + '=?' for key in fields) +
                           ' WHERE job_id=?', (*fields.values(), job_id))

    def steer(self, job_id, endpoint, actor=None, message=''):
        # Any admitted member in the same conversation can steer an active job.
        with self.transaction() as cursor:
            row = cursor.execute('''SELECT steering_json FROM background_jobs
                WHERE job_id=? AND endpoint_key=? AND state IN ('queued','running') FOR UPDATE''',
                (job_id, endpoint)).fetchone()
            if row is None:
                raise ValueError('找不到当前会话里正在运行的任务。')
            messages = json.loads(row['steering_json']) + [message]
            cursor.execute('UPDATE background_jobs SET steering_json=? WHERE job_id=?',
                           (json.dumps(messages, ensure_ascii=False), job_id))

    def cancel(self, job_id, endpoint, actor=None, *, allow_any_actor=False):
        with self.transaction() as cursor:
            if allow_any_actor or actor is None:
                row = cursor.execute('''UPDATE background_jobs SET state='cancelled', finished_at=?, error='用户取消了任务'
                    WHERE job_id=? AND endpoint_key=? AND state IN ('queued','running') RETURNING job_id''',
                    (int(self.clock()), job_id, endpoint)).fetchone()
            else:
                row = cursor.execute('''UPDATE background_jobs SET state='cancelled', finished_at=?, error='用户取消了任务'
                    WHERE job_id=? AND endpoint_key=? AND actor_key=? AND state IN ('queued','running') RETURNING job_id''',
                    (int(self.clock()), job_id, endpoint, actor)).fetchone()
        if row:
            self.jobs.cancel(job_id)
        return row is not None

    def recover(self, account_key):
        now = int(self.clock())
        with self.transaction() as cursor:
            # Jobs whose deadline already passed while host was down fail cleanly.
            cursor.execute('''UPDATE background_jobs SET state='failed',
                error='任务超过创建时确定的截止时间；工作区文件保留。', finished_at=?
                WHERE account_key=? AND state='running' AND deadline_at <= ?''',
                (now, account_key, now))
            # Jobs still within their deadline stay 'running' so the new worker
            # reconnects to their container systemd unit immediately.
            cursor.execute('''UPDATE durable_jobs j
                SET status='pending',
                    attempts=CASE WHEN j.attempts > 0 THEN j.attempts - 1 ELSE 0 END,
                    next_attempt_at=?,
                    lease_owner='',
                    lease_until=NULL,
                    updated_at=?
                FROM background_jobs b
                WHERE j.job_id = b.job_id
                  AND b.account_key = ?
                  AND b.state = 'running'
                  AND b.deadline_at > ?
                  AND j.status = 'running' ''',
                (now, now, account_key, now))

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
        row = await self.calls(self.store.start, durable_job.job_id)
        if row is None:
            current = await self.calls(self.store.get, durable_job.job_id)
            return {'state': current['state'] if current else 'cancelled'}
        payload = dict(getattr(durable_job, 'payload', None) or {})
        row = {**dict(row), **payload}
        steering_offset, warned, asking = 0, False, False
        notes = []
        trace = AgentTrace()

        async def is_cancelled():
            current = await self.calls(self.store.get, row['job_id'])
            return current is None or current['state'] == 'cancelled'

        async def feedback(*, warn=True):
            nonlocal steering_offset, warned
            current = await self.calls(self.store.get, row['job_id'])
            if current['state'] == 'cancelled':
                raise asyncio.CancelledError
            if warn and asking:
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
                until = min(self.clock() + 7200, row['deadline_at'])
                while self.clock() < until:
                    messages = await feedback(warn=False)
                    if messages:
                        return '\n'.join(messages)
                    await asyncio.sleep(min(2, max(0, until - self.clock())))
            finally:
                asking = False
            assumption = ('任务已到截止时间，未收到答复；停止扩展任务，保存现有文件并整理报告。'
                if self.clock() >= row['deadline_at'] else '前台两小时未回答，请自行判断继续，在报告中写清采用的假设。')
            notes.append(assumption)
            return assumption

        async def progress(name):
            await self.calls(self.store.update, row['job_id'], last_tool=name)

        session = None
        state, error, report = 'completed', '', ''
        report_failed = False
        host_interrupted = False
        deadline = asyncio.timeout(max(0, row['deadline_at'] - self.clock()) + 180)
        try:
            session = await self.session_factory(row, durable_job, question)
            engine_kwargs = dict(feedback=feedback, progress=progress, trace=trace)
            try:
                if 'is_cancelled' in inspect.signature(self.engine).parameters:
                    engine_kwargs['is_cancelled'] = is_cancelled
            except (TypeError, ValueError):
                pass
            async with deadline:
                report = await self.engine(row, session, **engine_kwargs)
            if outcome_of(report) == 'failed':
                report_failed = True
                lines = [line.strip() for line in (report or '').splitlines() if line.strip()]
                reason = lines[1][:200] if len(lines) > 1 else ''
                state = 'failed'
                error = '后台报告任务失败：' + reason
        except asyncio.CancelledError:
            current = await self.calls(self.store.get, row['job_id'])
            if current is not None and current['state'] == 'cancelled':
                state, error = 'cancelled', '用户取消了任务。'
            else:
                # Host worker is stopping/restarting: keep background_jobs in 'running'
                # so the restarted worker reconnects to the container unit.
                host_interrupted = True
            raise
        except TimeoutError as exc:
            if deadline.expired():
                state, error = 'failed', '任务超过创建时确定的截止时间且没能正常停下；请查看工作区中的现有文件。'
            else:
                state, error = 'failed', f'后台任务失败：后台进程没能启动或内部步骤超时（{exc}）'
                self.logger.exception('Background job %s timed out before job deadline', row['job_id'])
        except Exception as exc:
            state, error = 'failed', f'后台任务失败：{type(exc).__name__}: {exc}'
            self.logger.exception('Background job %s failed', row['job_id'])
        finally:
            if not host_interrupted:
                parts = (report, '\n'.join(notes)) if report_failed else (report, error, '\n'.join(notes))
                report = '\n\n'.join(part for part in parts if part)
                state = await self.calls(self.store.finish, row['job_id'], state=state, error=error, report=report)
                try:
                    if state != 'cancelled':
                        await self.save_report(row, report)
                except Exception as exc:
                    self.logger.exception('Could not save background report to VM')
                    await self.calls(self.store.update, row['job_id'],
                                     report=report + f'\n\n（报告未能写入 VM：{type(exc).__name__}）')
                await self.record_usage(row, trace)
            if session is not None:
                await session.close()
        return {'state': state}
