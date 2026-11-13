"""Short command-monitor transactions; no guest I/O while holding account locks."""
import re

from .lease import assert_epoch
from .program_actions import ProgramDenied

TERMINAL = {'completed','failed','cancelled'}


def observed(value):
    required = {'ok','state','stopped','exit_code','elapsed_seconds','error','output_head','output_tail','output_bytes'}
    if (type(value) is not dict or not required<=value.keys() or value.keys()-required-{'output_error'}
            or value['ok'] is not True or type(value['stopped']) is not bool
            or type(value['state']) is not str or value['state'] not in TERMINAL|{'running','ambiguous'}
            or value['stopped']!=(value['state'] in TERMINAL)):
        raise ValueError('invalid_command_observation')
    for key,maximum in (('elapsed_seconds',86400),('output_bytes',2**63-1),('exit_code',255)):
        number = value[key]
        if number is None and key!='output_bytes':
            continue
        if type(number) is not int or not (-255 if key=='exit_code' else 0)<=number<=maximum:
            raise ValueError('invalid_command_observation')
    for key in ('error','output_error'):
        if type(value.get(key,'')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{0,96}',value.get(key,'')):
            raise ValueError('invalid_command_observation')
    if value['state']=='completed' and (value['exit_code']!=0 or value['error']):
        raise ValueError('invalid_command_observation')
    result = dict(value)
    for key in ('output_head','output_tail'):
        if type(value[key]) is not str or len(value[key])>2048:
            raise ValueError('invalid_command_observation')
        text = re.sub(r'gvm_[A-Za-z0-9_-]{43}','[VM_TOKEN]',value[key])
        result[key] = text.encode()[:4096].decode(errors='ignore')
    return result


class CommandExecution:
    def __init__(self, commands, wakes):
        self.commands,self.wakes = commands,wakes
        self.sessions,self.transaction,self.clock = commands.sessions,commands.transaction,commands.clock

    def _row(self, cursor, command_id):
        assert_epoch(cursor,self.sessions.lease)
        row = cursor.execute('''SELECT c.* FROM qq_official_background_commands c
            JOIN qq_official_endpoints e USING(endpoint_key)
            WHERE c.command_id=? AND e.account_key=? FOR UPDATE OF c''',(command_id,self.sessions.lease.account_key)).fetchone()
        if row is None:
            raise ProgramDenied('background_command_not_found',404)
        return dict(row)

    def pending(self):
        with self.transaction() as cursor:
            assert_epoch(cursor,self.sessions.lease)
            return [row['command_id'] for row in cursor.execute('''SELECT c.command_id FROM qq_official_background_commands c
                JOIN qq_official_endpoints e USING(endpoint_key) WHERE e.account_key=? AND
                (c.finished_at IS NULL OR (c.state IN ('completed','failed') AND c.notification_action_id IS NULL AND c.notification_error=''))
                ORDER BY c.checked_at,c.created_at,c.command_id LIMIT 32''',(self.sessions.lease.account_key,)).fetchall()]

    def next(self, command_id):
        with self.transaction() as cursor:
            row = self._row(cursor,command_id)
            now = int(self.clock())
            cursor.execute('UPDATE qq_official_background_commands SET checked_at=? WHERE command_id=?',(now,command_id))
            if row['finished_at'] is not None:
                return 'notify',row
            try:
                self.commands.check(cursor,row)
            except PermissionError:
                row.update(cancel_requested=True,error_code=row['error_code'] if row['cancel_requested'] else 'command_source_unavailable')
            expired = row['deadline_at']<=now
            if row['state']=='pending' and (row['cancel_requested'] or expired):
                row.update(state='cancelled' if row['cancel_requested'] else 'failed',finished_at=now,elapsed_seconds=0,
                    error_code=row['error_code'] if row['cancel_requested'] else 'command_timeout')
                operation = 'notify'
            elif row['cancel_requested'] or expired:
                row.update(state='stopping',error_code=row['error_code'] if row['cancel_requested'] else 'command_timeout')
                operation = 'stop'
            elif row['state']=='pending':
                row.update(state='starting',started_at=now)
                operation = 'start'
            else:
                operation = 'status'
            fields = ('state','cancel_requested','started_at','finished_at','elapsed_seconds','error_code')
            cursor.execute('UPDATE qq_official_background_commands SET '+','.join(key+'=?' for key in fields)+
                ' WHERE command_id=?',(*(row[key] for key in fields),command_id))
            return operation,row

    def record(self, command_id, value):
        report = observed(value)
        with self.transaction() as cursor:
            row = self._row(cursor,command_id)
            if row['finished_at'] is not None:
                return  # A delayed running observation cannot erase a real completion.
            if row['state']=='pending':
                raise ProgramDenied('command_not_started')
            status,error = report['state'],report['error'] or report.get('output_error','')
            if not report['stopped'] and row['state']=='stopping':
                status,error = 'stopping',row['error_code']
            elif status=='cancelled' and row['error_code']=='command_timeout' and not row['cancel_requested']:
                status,error = 'failed','command_timeout'
            cursor.execute('''UPDATE qq_official_background_commands SET state=?,finished_at=?,exit_code=?,elapsed_seconds=?,
                output_head=?,output_tail=?,output_bytes=?,error_code=? WHERE command_id=?''',
                (status,int(self.clock()) if report['stopped'] else None,report['exit_code'],report['elapsed_seconds'],
                 report['output_head'],report['output_tail'],report['output_bytes'],error,command_id))

    def uncertain(self, command_id):
        with self.transaction() as cursor:
            row = self._row(cursor,command_id)
            if row['finished_at'] is None:
                cursor.execute('''UPDATE qq_official_background_commands
                    SET state=CASE WHEN state='stopping' THEN state ELSE 'ambiguous' END,
                        error_code=CASE WHEN state='stopping' THEN error_code ELSE 'command_guest_unavailable' END
                    WHERE command_id=?''',(command_id,))

    def notify(self, command_id):
        with self.transaction() as cursor:
            row = self._row(cursor,command_id)
            if (row['finished_at'] is None or row['state'] not in {'completed','failed'}
                    or row['notification_action_id'] is not None or row['notification_error']):
                return False
            try:
                current = self.commands.check(cursor,row)
            except PermissionError:
                cursor.execute("UPDATE qq_official_background_commands SET notification_error='command_notification_source_unavailable' WHERE command_id=?",(command_id,))
                return False
            text = (f"[后台命令 {command_id} {row['state']}]\n退出码：{row['exit_code']}；用时：{row['elapsed_seconds']} 秒；"
                f"错误：{row['error_code'] or '无'}\n完整输出：/work/.gaoji/out/{command_id}.txt\n"
                f"输出开头：\n{row['output_head'][:1600]}\n输出末尾：\n{row['output_tail'][-1600:]}")
            # Savepoint avoids committing a quota reservation if wake allocation
            # fails (e.g. its bounded overflow batch is already full).
            cursor.execute('SAVEPOINT command_notice')
            try:
                self.wakes.submit(current,generation=row['conversation_generation'],program=row['program_name'],
                    request_id='command-'+command_id,text=text,source_command_id=command_id,transaction_cursor=cursor)
            except ProgramDenied as error:
                cursor.execute('ROLLBACK TO SAVEPOINT command_notice')
                reason = 'command_notification_quota_unavailable' if error.status==429 else 'command_notification_unavailable'
                cursor.execute('UPDATE qq_official_background_commands SET notification_error=? WHERE command_id=?',(reason,command_id))
                return False
            finally:
                cursor.execute('RELEASE SAVEPOINT command_notice')
            return True
