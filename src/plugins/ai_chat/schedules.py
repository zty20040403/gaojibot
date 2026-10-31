"""One table for reminders and scheduled background work, in Shanghai time."""
from datetime import datetime
import json
import time
from zoneinfo import ZoneInfo

from croniter import croniter

ZONE = ZoneInfo('Asia/Shanghai')


def next_time(*, at=None, cron=None, now):
    if bool(at) == bool(cron):
        raise ValueError('请指定 at 或 cron 中的一个；时间按 Asia/Shanghai 解释。')
    if cron:
        if len(cron.split()) != 5:
            raise ValueError('cron 使用五段格式：分 时 日 月 星期。')
        iterator = croniter(cron, datetime.fromtimestamp(now, ZONE))
        if iterator.expanded[0] == ['*'] or len(iterator.expanded[0]) != 1:
            raise ValueError('例行任务间隔不能短于一小时，请在每小时固定的一分钟执行。')
        return int(iterator.get_next(datetime).timestamp())
    value = datetime.fromisoformat(at)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZONE)
    timestamp = int(value.timestamp())
    if timestamp <= now:
        raise ValueError('提醒时间必须在未来。')
    return timestamp


class Schedules:
    def __init__(self, transaction, *, clock=time.time):
        self.transaction, self.clock = transaction, clock

    def create(self, cursor, source, *, text, kind='reminder', at=None, cron=None):
        if kind not in {'reminder', 'routine'} or not text.strip():
            raise ValueError('请提供 reminder 或 routine，以及具体内容。')
        now = int(self.clock())
        due = next_time(at=at, cron=cron, now=now)
        cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))', ('schedule:' + source['actor_key'],))
        count = cursor.execute('SELECT count(*) AS n FROM schedules WHERE actor_key=? AND cancelled_at IS NULL AND next_at IS NOT NULL',
                               (source['actor_key'],)).fetchone()['n']
        if count >= 20:
            raise ValueError('每人最多保留二十个提醒和例行任务，请先取消不需要的。')
        row = cursor.execute('''INSERT INTO schedules(scope_key,endpoint_key,actor_key,kind,text,next_at,cron,created_at,source_json)
            VALUES(?,?,?,?,?,?,?,?,?) RETURNING *''', (source['scope_key'], source['endpoint_key'], source['actor_key'],
            kind, text.strip(), due, cron, now, json.dumps(source))).fetchone()
        return self.view(row)

    @staticmethod
    def view(row):
        return {key: value for key, value in dict(row).items() if key != 'source_json'} | {
            'next_time': datetime.fromtimestamp(row['next_at'], ZONE).isoformat() if row['next_at'] else None}

    def list(self, cursor, endpoint, actor):
        rows = cursor.execute('''SELECT * FROM schedules WHERE endpoint_key=? AND actor_key=?
            AND cancelled_at IS NULL AND next_at IS NOT NULL ORDER BY next_at''', (endpoint, actor)).fetchall()
        return [self.view(row) for row in rows]

    def cancel(self, cursor, endpoint, actor, schedule_id):
        row = cursor.execute('''UPDATE schedules SET cancelled_at=? WHERE id=? AND endpoint_key=? AND actor_key=?
            AND cancelled_at IS NULL RETURNING id''', (int(self.clock()), schedule_id, endpoint, actor)).fetchone()
        return row is not None
