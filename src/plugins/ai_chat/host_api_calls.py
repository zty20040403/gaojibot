"""Reserve before forwarding: failed/unknown calls still consume their slot."""
from contextlib import contextmanager
import re
import time

from .host_apis import canonical_path


class HostAPICalls:
    def __init__(self, database, *, clock=time.time):
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

    def begin(self, *, scope_key, api_name, caller_ref, method, path, limit, rejection=None):
        if type(limit) is not int or limit<0:
            raise ValueError('invalid_api_limit')
        # Only canonical relative paths, never query parameters or request bodies.
        try:
            if canonical_path(path.encode())!=path:
                raise ValueError
        except (ValueError,AttributeError):
            path = '<invalid>'
        api_name = api_name if re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}',api_name) else '<unknown>'
        now = int(self.clock())
        with self.transaction() as cursor:
            status,error = rejection or (0,'')
            if rejection is None:
                reserved = cursor.execute('''INSERT INTO host_api_quota(scope_key,api_name,hour,used) VALUES (?,?,?,1)
                    ON CONFLICT(scope_key,api_name,hour) DO UPDATE SET used=host_api_quota.used+1
                    WHERE ?=0 OR host_api_quota.used<? RETURNING used''',
                    (scope_key,api_name,now//3600,limit,limit)).fetchone()
                if reserved is None:
                    status,error = 429,'api_hourly_limit'
            row = cursor.execute('''INSERT INTO host_api_calls(scope_key,api_name,caller_ref,method,path,
                status_code,outcome,error_code,created_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING call_id''',
                (scope_key,api_name,caller_ref,method,path,status,'denied' if status else 'pending',error,now,now if status else None)).fetchone()
        return dict(call_id=row['call_id'],status_code=status,error_code=error)

    def finish(self, call_id, *, status_code, outcome, error_code=''):
        if outcome not in {'completed','failed','interrupted'} or not re.fullmatch('[a-z_]*',error_code):
            raise ValueError('invalid_api_outcome')
        with self.transaction() as cursor:
            return cursor.execute('''UPDATE host_api_calls SET status_code=?,outcome=?,error_code=?,finished_at=?
                WHERE call_id=? AND finished_at IS NULL RETURNING call_id''',
                (status_code,outcome,error_code,int(self.clock()),call_id)).fetchone() is not None

    def snapshot(self, *, limit=100):
        with self.transaction() as cursor:
            rows = cursor.execute('''SELECT * FROM host_api_calls ORDER BY call_id DESC LIMIT ?''',
                (min(max(int(limit),1),500),)).fetchall()
            counts = cursor.execute('''SELECT outcome,count(*) AS count FROM host_api_calls
                WHERE created_at>=? GROUP BY outcome''',(int(self.clock())-86400,)).fetchall()
        return {'items':[dict(row) for row in rows],'counts':{row['outcome']:row['count'] for row in counts}}
