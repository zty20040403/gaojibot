"""Pure account-locked bilateral reduction; no network, commits or queued replay."""
from dataclasses import dataclass,field
import hmac
import re
import secrets

from .lease import assert_epoch
from .link_commands import LinkCommand

LIVE = ('sending','issued','confirming')


@dataclass(frozen=True)
class LinkDecision:
    status: str
    challenge_id: str = ''
    link_id: str = ''
    code: str | None = field(default=None,repr=False)
    entries: tuple = field(default=(),repr=False)
    more: bool = False


class LinkReducer:
    def __init__(self,authority):self.authority = authority
    def __repr__(self):return 'LinkReducer(<protected authority>)'
    def now(self):return int(self.authority.clock())

    def get(self,cursor,token,reference):
        assert_epoch(cursor,token)
        return cursor.execute('SELECT * FROM qq_official_link_challenges WHERE challenge_id=? AND account_key=? FOR UPDATE',
            (reference,token.account_key)).fetchone()

    def code_hash(self,row,code):
        return self.authority._hash('qq-link-code:v1',[row[key] for key in ('challenge_id','account_key','account_epoch',
            'source_authority_hash','source_receipt_hash','expires_at')]+[code])

    @staticmethod
    def fields(proof,side):
        names = dict(endpoint_key='endpoint_key',actor_key='actor_key',scope_key='scope_key',endpoint_generation='generation',
            route_epoch='route_epoch',receipt_hash='receipt_hash',authority_hash='authority_hash')
        row = {side+'_'+destination:proof[source] for source,destination in names.items()}
        row['created_at' if side=='source' else 'target_confirmed_at'] = proof['received_at']
        return row

    @staticmethod
    def proof(row,side):
        names = dict(endpoint_key='endpoint_key',actor_key='actor_key',scope_key='scope_key',endpoint_generation='generation',
            route_epoch='route_epoch',receipt_hash='receipt_hash',authority_hash='authority_hash')
        return {source:row[side+'_'+destination] for source,destination in names.items()}|dict(account_epoch=row['account_epoch'],
            received_at=row['created_at' if side=='source' else 'target_confirmed_at'],expires_at=row['expires_at'])

    def current(self,cursor,token,row):
        return self.authority.current(cursor,token,self.proof(row,'source')) and (row['target_endpoint_key'] is None
            or self.authority.current(cursor,token,self.proof(row,'target')))

    def rate(self,cursor,token,key,maximum,seconds):
        bucket = self.authority._hash('qq-link-rate:v1',[token.account_key,key])
        cursor.execute('INSERT INTO qq_official_binding_rate_limits(bucket,window_start,hits) VALUES (?,?,0) ON CONFLICT DO NOTHING',(bucket,self.now()))
        row = cursor.execute('SELECT window_start,hits FROM qq_official_binding_rate_limits WHERE bucket=? FOR UPDATE',(bucket,)).fetchone()
        reset = row['window_start']+seconds<=self.now(); hits = 1 if reset else min(row['hits']+1,maximum+1)
        cursor.execute('UPDATE qq_official_binding_rate_limits SET window_start=?,hits=? WHERE bucket=?',
            (self.now() if reset else row['window_start'],hits,bucket))
        return hits<=maximum

    def apply(self,cursor,token,message,command):
        assert_epoch(cursor,token)
        if (not isinstance(command,LinkCommand) or not command.valid or command.reference
            or command.action not in {'create','confirm'} or (command.action=='create' and command.code)
            or (command.action=='confirm' and (not isinstance(command.code,str) or not re.fullmatch(r'[0-9]{6}',command.code)))):
            return LinkDecision('unsupported')
        proof = self.authority.proof(cursor,token,message)
        if proof is None:return LinkDecision('denied')
        repeat = cursor.execute('''SELECT * FROM qq_official_link_challenges WHERE account_key=?
            AND (source_receipt_hash=? OR target_receipt_hash=?)''',(token.account_key,proof['receipt_hash'],proof['receipt_hash'])).fetchone()
        if repeat is not None:return self.result(cursor,repeat)
        if not self.rate(cursor,token,'account',120,60):return LinkDecision('limited')
        if command.action=='create':return self.create(cursor,token,proof)
        if not self.rate(cursor,token,['confirm',proof['actor_key']],5,600):return LinkDecision('limited')
        rows = self.live(cursor,token)
        if len(rows)>20:return LinkDecision('limited')
        matches = [row for row in rows if hmac.compare_digest(row['code_hmac'],self.code_hash(row,command.code))]
        if len(matches)!=1:return LinkDecision('invalid')
        row = matches[0]
        if row['source_endpoint_key']==proof['endpoint_key'] or row['target_endpoint_key'] is not None:return LinkDecision('invalid')
        if not self.current(cursor,token,row):return self.close(cursor,row)
        target = self.authority.proof(cursor,token,message,expires_at=row['expires_at'])
        if target is None or target['received_at']<row['created_at']:return LinkDecision('invalid')
        values = self.fields(target,'target')
        cursor.execute("UPDATE qq_official_link_challenges SET state='confirming',"+','.join(name+'=?' for name in values)+
            ' WHERE challenge_id=?',(*values.values(),row['challenge_id']))
        return self.advance(cursor,token,self.get(cursor,token,row['challenge_id']))

    def live(self,cursor,token):
        return cursor.execute("""SELECT * FROM qq_official_link_challenges WHERE account_key=? AND code_hmac<>'' AND expires_at>?
            ORDER BY created_at,challenge_id LIMIT 21 FOR UPDATE""",(token.account_key,self.now())).fetchall()

    def create(self,cursor,token,proof):
        if not self.rate(cursor,token,['issue',proof['endpoint_key']],3,600) or not self.rate(cursor,token,'issues',20,600):return LinkDecision('limited')
        row = self.fields(proof,'source')|dict(challenge_id='LC-'+secrets.token_hex(16),account_key=token.account_key,
            account_epoch=token.epoch,expires_at=proof['expires_at'])
        live = self.live(cursor,token)
        # Fixed-window rates allow a boundary burst. Enforce the separate live
        # cap so this bounded scan always contains every possible code match.
        if len(live)>20 or sum(candidate['source_endpoint_key']!=proof['endpoint_key'] for candidate in live)>=20:return LinkDecision('limited')
        for _ in range(8):
            code = f'{secrets.randbelow(1_000_000):06d}'
            if not any(hmac.compare_digest(candidate['code_hmac'],self.code_hash(candidate,code)) for candidate in live):break
        else:return LinkDecision('limited')
        if not self.authority.current(cursor,token,proof):return LinkDecision('denied')
        for previous in live:
            if previous['source_endpoint_key']==proof['endpoint_key']:self.close(cursor,previous)
        row['code_hmac'] = self.code_hash(row,code)
        cursor.execute('INSERT INTO qq_official_link_challenges('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        return LinkDecision('sending',row['challenge_id'],code=code)

    def result(self,cursor,row):
        link = cursor.execute('SELECT link_id,state FROM qq_official_conversation_links WHERE challenge_id=?',(row['challenge_id'],)).fetchone()
        status = 'revoked' if link and link['state']=='revoked' else row['state']
        return LinkDecision(status,row['challenge_id'],link['link_id'] if link else '')

    def close(self,cursor,row):
        if row['state'] in LIVE:
            state = 'expired' if self.now()>=row['expires_at'] else 'cancelled'
            cursor.execute("UPDATE qq_official_link_challenges SET state=?,code_hmac='' WHERE challenge_id=?",(state,row['challenge_id']))
            return LinkDecision(state,row['challenge_id'])
        return self.result(cursor,row)

    def record_issue(self,cursor,token,reference,*,outcome,native_id=None,received_at=None):
        """Only the real sender/ledger adapter may supply an accepted native ID."""
        row = self.get(cursor,token,reference)
        if row is None:return LinkDecision('invalid')
        if outcome=='committed':
            if (not isinstance(native_id,str) or not native_id.strip() or len(native_id.encode())>256
                or any(ord(c)<32 for c in native_id) or type(received_at) is not int or received_at<row['created_at']):
                raise ValueError('link_receipt_invalid')
            receipt = self.authority._hash('qq-link-accepted:v1',[token.account_key,row['source_endpoint_key'],native_id])
            if row['delivery_receipt_hash'] is not None:
                if not hmac.compare_digest(row['delivery_receipt_hash'],receipt):raise ValueError('link_receipt_conflict')
            else:cursor.execute("""UPDATE qq_official_link_challenges SET delivery_state='committed',delivery_receipt_hash=?,delivery_received_at=?,
                state=CASE WHEN state='sending' THEN 'issued' ELSE state END WHERE challenge_id=?""",(receipt,received_at,reference))
        elif outcome in {'failed','unknown'}:
            if row['delivery_state']=='pending':
                state = ('expired' if self.now()>=row['expires_at'] else 'cancelled') if row['state'] in LIVE else row['state']
                cursor.execute("UPDATE qq_official_link_challenges SET delivery_state=?,state=?,code_hmac='' WHERE challenge_id=?",(outcome,state,reference))
        else:raise ValueError('link_receipt_outcome_invalid')
        return self.advance(cursor,token,self.get(cursor,token,reference))

    def advance(self,cursor,token,row):
        if row['state'] not in LIVE:return self.result(cursor,row)
        if row['delivery_state'] in {'failed','unknown'} or not self.current(cursor,token,row):return self.close(cursor,row)
        if row['delivery_state']!='committed':return self.result(cursor,row)
        if row['target_endpoint_key'] is None:
            cursor.execute("UPDATE qq_official_link_challenges SET state='issued' WHERE challenge_id=?",(row['challenge_id'],))
            return LinkDecision('issued',row['challenge_id'])
        existing = cursor.execute('''SELECT l.*,c.delivery_receipt_hash FROM qq_official_conversation_links l
            JOIN qq_official_link_challenges c USING(challenge_id) WHERE l.account_key=? AND l.source_endpoint_key=?
            AND l.target_endpoint_key=? AND l.state='active' FOR UPDATE OF l''',
            (token.account_key,row['source_endpoint_key'],row['target_endpoint_key'])).fetchone()
        if not self.current(cursor,token,row):return self.close(cursor,row)
        now = self.now()
        if now>=row['expires_at']:return self.close(cursor,row)
        if existing:
            names = ('source_scope_key','target_scope_key','source_generation','source_route_epoch','target_generation','target_route_epoch')
            from .link_management import LinkManagement
            if existing['delivery_receipt_hash'] and all(existing[name]==row[name] for name in names) and LinkManagement(self).current(cursor,token,existing):
                self.close(cursor,row); return LinkDecision('already_linked',row['challenge_id'],existing['link_id'])
            cursor.execute("UPDATE qq_official_conversation_links SET state='revoked',generation=generation+1,revoked_at=? WHERE link_id=?",(now,existing['link_id']))
        link_id = secrets.token_hex(16)
        cursor.execute("UPDATE qq_official_link_challenges SET state='active',code_hmac='',activated_at=? WHERE challenge_id=?",(now,row['challenge_id']))
        names = ('challenge_id','account_key','source_endpoint_key','target_endpoint_key','source_scope_key','target_scope_key',
            'source_generation','source_route_epoch','target_generation','target_route_epoch')
        values = dict(link_id=link_id,created_at=now)|{name:row[name] for name in names}
        cursor.execute('INSERT INTO qq_official_conversation_links('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
        return LinkDecision('active',row['challenge_id'],link_id)

    def recover(self,cursor,token):
        """Every prior interrupted challenge closes; never reconstruct/send a code."""
        assert_epoch(cursor,token)
        rows = cursor.execute("SELECT * FROM qq_official_link_challenges WHERE account_key=? AND state IN ('sending','issued','confirming') FOR UPDATE",
            (token.account_key,)).fetchall()
        for row in rows:self.close(cursor,row)
        return len(rows)
