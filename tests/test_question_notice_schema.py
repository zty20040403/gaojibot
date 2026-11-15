"""0106 schema fences for ephemeral bot notice plans and real applied receipts."""
import secrets
import psycopg

from src.plugins.ai_chat.adapters.qq_official.keys import account_job_kind
from src.plugins.ai_chat.adapters.qq_official.plans import enqueue_plan
from src.plugins.ai_chat.adapters.qq_official.renderer import render_plan
from src.plugins.ai_chat.message_ir import MessageBody,TextNode
from src.plugins.ai_chat.platform_types import AccountKey,ActorKey,EndpointKey
from tests.question_notice_fixture import NoticeFixture


class QuestionNoticeSchemaTests(NoticeFixture):
    def next_source(self,**changes):
        self.jobs.mark_succeeded(self.notice_job.job_id,self.notice_job.lease_owner,
            result={'question_id':self.question['question_id']},now=self.now,
            lease_generation=self.notice_job.lease_generation)
        self.query("UPDATE qq_official_conversation_questions SET state='answered',reason_code='',answer='TEST_ANSWER',finished_at=? WHERE question_id=?",
            (self.now,self.question['question_id']))
        self.question = self.insert_question(deadline_at=min(self.now+7200,self.deadline))
        self.notice_job = self.jobs.claim_due('TEST_NOTICE_WORKER',
            kinds=(account_job_kind('question',self.account.key),),now=self.now)[0]
        self.query("UPDATE qq_official_conversation_questions SET state='running',started_at=? WHERE question_id=?",
            (self.now,self.question['question_id']))
        return self.notice(**changes)

    def bot_actor(self,account=None,native_id='TEST_NOTICE_BOT'):
        return ActorKey(account or self.account,'bot','',native_id)

    def notice_plan(self,source,**changes):
        target = EndpointKey.parse(source['target_endpoint_key'])
        with self.tx() as cursor:
            ref = enqueue_plan(cursor,token=self.token,
                rendered=render_plan(MessageBody((TextNode(0,'TEST_QUESTION_NOTICE_BASE'),)),target),
                requester=self.actor if target!=self.target else ActorKey(self.account,'group_member',target.native_conversation_id,'TEST_BASE_MEMBER'),
                logical_key='TEST_QN_BASE_'+secrets.token_hex(4),source_scope_key=target.scope.key,
                expires_at=self.now+30,now=self.now,admission=lambda *args:True)
        row = dict(self.query('SELECT * FROM delivery_plans WHERE plan_id=?',(ref.plan_id,))[0])
        row.update(plan_id=secrets.token_hex(16),idempotency_key='qqp1:'+secrets.token_hex(32),
            requester_actor_key=self.bot_actor(target.account,source['bot_native_id']).key,
            source_scope_key=source['target_scope_key'],source_canonical_message_id=None,turn_id=None,
            endpoint_generation=source['target_generation'],route_epoch=source['target_route_epoch'],
            capability_revision=source['capability_revision'],purpose='question_notice',payload_mode='ephemeral',
            manifest_json='{"v":1,"ephemeral":true}',manifest_digest='a'*64,
            digest_scheme='hmac_sha256_v1',digest_key_id='v1',part_count=1,
            created_at=source['created_at'],expires_at=source['expires_at'],question_notice_id=source['notice_id'])
        row.update(changes)
        with self.tx() as cursor:
            cursor.execute('INSERT INTO delivery_plans('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        return row['plan_id']

    def question_notice(self,source,**changes):
        row = dict(notice_id=source['notice_id'],question_id=source['question_id'],
            account_key=source['account_key'],target_endpoint_key=source['target_endpoint_key'],
            bot_native_id=source['bot_native_id'],bot_actor_key=self.bot_actor(native_id=source['bot_native_id']).key,
            state='pending',plan_id=None,delivery_state='pending',receipt_id=None,
            delivery_receipt_hash=None,delivery_received_at=None,
            created_at=source['created_at'],updated_at=source['created_at'])
        row.update(changes)
        with self.tx() as cursor:
            cursor.execute('INSERT INTO qq_official_question_notices('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        return dict(self.query('SELECT * FROM qq_official_question_notices WHERE notice_id=?',(source['notice_id'],))[0])

    def delivery_and_receipt(self,source,plan_id,*,send_mode='proactive',applied=True,kind='response',
            delivery_status='committed',native_id=None,bot_actor_key=None,bot_source=None,bot_revision=None,
            reply_to='',observed_at=None):
        native_id = native_id or 'TEST_QQ_NOTICE_'+secrets.token_hex(6)
        observed_at = source['created_at']+2 if observed_at is None else observed_at
        plan = self.query('SELECT * FROM delivery_plans WHERE plan_id=?',(plan_id,))[0]
        with self.tx() as cursor:
            did = cursor.execute('''INSERT INTO deliveries(
                idempotency_key,source_scope_key,target_platform,target_kind,target_native_conversation_id,
                reply_to_native_message_id,body_json,content_fingerprint,status,attempts,next_attempt_at,
                native_message_id,last_error,created_at,updated_at,plan_id,part_index,account_key,endpoint_key,
                requester_actor_key,endpoint_generation,route_epoch,capability_revision,purpose,payload_mode,
                variant,payload_digest,digest_scheme,digest_key_id,priority,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING delivery_id''',
                ('qqd1:'+secrets.token_hex(32),plan['source_scope_key'],'qq-official','group',self.target.native_conversation_id,
                 reply_to,'{"v":1,"nodes":[]}','','pending',1,self.now,
                 '',plan['created_at'],observed_at,plan_id,0,
                 plan['account_key'],plan['endpoint_key'],plan['requester_actor_key'],plan['endpoint_generation'],
                 plan['route_epoch'],plan['capability_revision'],plan['purpose'],plan['payload_mode'],
                 'primary','b'*64,'hmac_sha256_v1','v1',0,plan['expires_at'])).fetchone()['delivery_id']
            window_id = None; msg_seq = None; charge = 'none'
            if send_mode=='passive':
                window_id = 'WIN-'+secrets.token_hex(8); msg_seq = 1; charge = 'spent'
                cursor.execute('''INSERT INTO qq_official_reply_windows(
                    window_id,account_key,endpoint_key,endpoint_generation,route_epoch,
                    credential_kind,credential_value,received_at,expires_at,max_replies,used_count,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (window_id,plan['account_key'],plan['endpoint_key'],plan['endpoint_generation'],plan['route_epoch'],
                     'msg_id',reply_to or 'TEST_HUMAN_MSG_'+secrets.token_hex(4),self.now,self.now+300,5,1,self.now,self.now))
            attempt_id = cursor.execute('''INSERT INTO delivery_attempts(
                delivery_id,attempt,state,detail,started_at,dispatched_at,finished_at,account_key,endpoint_key,
                fencing_epoch,endpoint_generation,route_epoch,capability_revision,bot_actor_key,bot_identity_source,
                bot_identity_revision,send_mode,window_id,msg_seq,window_charge_state,payload_mode,payload_digest,
                digest_scheme,digest_key_id,wire_payload_json,send_by_at,acceptance)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING attempt_id''',
                (did,1,'committed' if delivery_status=='committed' else 'dispatched','',plan['created_at'],plan['created_at'],
                 observed_at if delivery_status=='committed' else None,plan['account_key'],plan['endpoint_key'],
                 source['account_epoch'],plan['endpoint_generation'],plan['route_epoch'],plan['capability_revision'],
                 bot_actor_key or plan['requester_actor_key'],bot_source or source['bot_identity_source'],
                 bot_revision or source['bot_identity_revision'],send_mode,window_id,msg_seq,charge,'ephemeral','c'*64,
                 'hmac_sha256_v1','v1',None,plan['created_at']+30,'confirmed_yes' if delivery_status=='committed' else 'unknown')).fetchone()['attempt_id']
            cursor.execute('UPDATE deliveries SET status=?,native_message_id=?,claim_epoch=?,current_attempt_id=? WHERE delivery_id=?',
                (delivery_status,native_id if delivery_status=='committed' else '',source['account_epoch'],attempt_id,did))
            receipt_id = secrets.token_hex(16)
            cursor.execute('''INSERT INTO delivery_receipts(
                receipt_id,attempt_id,account_key,endpoint_key,kind,native_message_id,receipt_digest,observed_at,applied_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (receipt_id,attempt_id,plan['account_key'],plan['endpoint_key'],kind,native_id,'d'*64,observed_at,
                 observed_at if applied else None))
        return did,attempt_id,receipt_id,observed_at

    def test_one_ephemeral_plan_per_notice_source_and_immutable_plan_fences(self):
        source = self.notice(); notice = self.question_notice(source); plan = self.notice_plan(source)
        with self.pg_error(psycopg.errors.UniqueViolation):self.notice_plan(source)
        self.query("UPDATE qq_official_question_notices SET state='sending',plan_id=?,updated_at=? WHERE notice_id=?",
            (plan,self.now+1,notice['notice_id']))
        with self.pg_error(pattern='question_notice_original_source_required'):self.notice_plan(source)
        for field,value in (('question_notice_id',None),('expires_at',source['expires_at']-1),('source_scope_key','TEST_OTHER')):
            with self.subTest(field=field),self.pg_error(pattern='immutable_question_notice_plan'):
                self.query(f'UPDATE delivery_plans SET {field}=? WHERE plan_id=?',(value,plan))
        with self.pg_error(pattern='immutable_question_notice_plan'):
            self.query('DELETE FROM delivery_plans WHERE plan_id=?',(plan,))

    def test_human_other_bot_and_mismatched_source_fields_cannot_build_plan(self):
        human = ActorKey(self.account,'group_member',self.target.native_conversation_id,'TEST_NOTICE_BOT')
        wrong_bot = self.bot_actor(native_id='TEST_OTHER_BOT')
        other_account = AccountKey(self.account.platform,self.account.app_id,'sandbox')
        other_group = EndpointKey(other_account,'group',self.target.native_conversation_id)
        self.provision(other_group)
        for changes in (dict(requester_actor_key=human.key),dict(requester_actor_key=wrong_bot.key),
                dict(requester_actor_key='actor:v1:[broken'),dict(requester_actor_key='actor:v1: ["a"]'),
                dict(account_key=other_account.key,endpoint_key=other_group.key),
                dict(source_scope_key=self.endpoint.scope.key),dict(endpoint_generation=2),
                dict(route_epoch=2),dict(capability_revision=999),
                dict(source_canonical_message_id=1),dict(turn_id=1),
                dict(created_at=self.now-1),dict(created_at=self.now+30),dict(expires_at=self.now+31)):
            with self.subTest(changes=changes),self.pg_error():
                self.notice_plan(self.next_source(),**changes)

    def test_smuggling_persistent_or_other_purposes_is_rejected(self):
        for changes in (dict(payload_mode='persistent'),dict(part_count=2),dict(question_notice_id=None),
                dict(purpose='unsolicited',payload_mode='persistent',digest_scheme='sha256',digest_key_id=None),
                dict(purpose='final',payload_mode='persistent',digest_scheme='sha256',digest_key_id=None),
                dict(link_reply_inbox_id='TEST_INBOX'),dict(ops_action_id='TEST_OPS')):
            with self.subTest(changes=changes),self.pg_error():
                self.notice_plan(self.next_source(),**changes)

    def test_real_proactive_applied_receipt_commits_and_rejects_fakes(self):
        # Fake unapplied / verified_echo / passive / wrong bot revision / non-committed delivery receipts must all fail.
        for label,kwargs in (
            ('unapplied',dict(applied=False)),
            ('verified_echo',dict(kind='verified_echo')),
            ('passive_window_only',dict(send_mode='passive',reply_to='')),
            ('passive_window',dict(send_mode='passive',reply_to='TEST_HUMAN_MSG')),
            ('reply_to_set',dict(reply_to='TEST_HUMAN_MSG')),
            ('not_committed',dict(delivery_status='accepted')),
            ('wrong_bot_revision',dict(bot_revision=2)),
            ('wrong_bot_source',dict(bot_source='self_api')),
            ('too_early',dict(observed_at=self.now-1)),
        ):
            bad_source = self.next_source(); bad_notice = self.question_notice(bad_source); bad_plan = self.notice_plan(bad_source)
            self.query("UPDATE qq_official_question_notices SET state='sending',plan_id=?,updated_at=? WHERE notice_id=?",
                (bad_plan,self.now+1,bad_notice['notice_id']))
            _, _, bad_receipt, observed = self.delivery_and_receipt(bad_source,bad_plan,**kwargs)
            with self.subTest(label=label),self.pg_error(pattern='question_notice_receipt_required'):
                self.query('''UPDATE qq_official_question_notices SET state='committed',delivery_state='committed',
                    receipt_id=?,delivery_receipt_hash=?,delivery_received_at=?,updated_at=? WHERE notice_id=?''',
                    (bad_receipt,secrets.token_hex(32),observed,max(observed,self.now+2),bad_notice['notice_id']))
        source = self.next_source(); notice = self.question_notice(source); plan = self.notice_plan(source)
        self.query("UPDATE qq_official_question_notices SET state='sending',plan_id=?,updated_at=? WHERE notice_id=?",
            (plan,self.now+1,notice['notice_id']))
        # Without any receipt row, committed state is rejected by CHECK constraint.
        with self.pg_error(psycopg.errors.CheckViolation):
            self.query("UPDATE qq_official_question_notices SET state='committed',delivery_state='committed' WHERE notice_id=?",
                (notice['notice_id'],))
        # Real applied proactive response commits cleanly once.
        _, _, real_receipt, observed = self.delivery_and_receipt(source,plan)
        receipt_hash = secrets.token_hex(32)
        self.query('''UPDATE qq_official_question_notices SET state='committed',delivery_state='committed',
            receipt_id=?,delivery_receipt_hash=?,delivery_received_at=?,updated_at=? WHERE notice_id=?''',
            (real_receipt,receipt_hash,observed,observed,notice['notice_id']))
        row = self.query('SELECT * FROM qq_official_question_notices WHERE notice_id=?',(notice['notice_id'],))[0]
        self.assertEqual((row['state'],row['delivery_state'],row['receipt_id']),('committed','committed',real_receipt))
        with self.pg_error(pattern='immutable_question_notice'):
            self.query('UPDATE qq_official_question_notices SET delivery_receipt_hash=? WHERE notice_id=?',
                (secrets.token_hex(32),notice['notice_id']))

    def test_late_accepted_receipt_records_after_cancel_or_expiry_without_resurrecting_state(self):
        for terminal,initial_delivery in (('cancelled','pending'),('expired','pending'),('unknown','unknown')):
            with self.subTest(terminal=terminal,initial_delivery=initial_delivery):
                source = self.next_source()
                notice = self.question_notice(source)
                plan = self.notice_plan(source)
                self.query("UPDATE qq_official_question_notices SET state='sending',plan_id=?,updated_at=? WHERE notice_id=?",
                    (plan,self.now+1,notice['notice_id']))
                self.query("UPDATE qq_official_question_notices SET state=?,delivery_state=?,updated_at=? WHERE notice_id=?",
                    (terminal,initial_delivery,self.now+2,notice['notice_id']))
                _, _, real_receipt, observed = self.delivery_and_receipt(source,plan,observed_at=self.now+50)
                with self.pg_error(pattern='immutable_question_notice'):
                    self.query('''UPDATE qq_official_question_notices SET state='committed',delivery_state='committed',
                        receipt_id=?,delivery_receipt_hash=?,delivery_received_at=?,updated_at=? WHERE notice_id=?''',
                        (real_receipt,secrets.token_hex(32),observed,observed,notice['notice_id']))
                self.query('''UPDATE qq_official_question_notices SET delivery_state='committed',
                    receipt_id=?,delivery_receipt_hash=?,delivery_received_at=?,updated_at=? WHERE notice_id=?''',
                    (real_receipt,secrets.token_hex(32),observed,observed,notice['notice_id']))
                row = self.query('SELECT state,delivery_state,receipt_id FROM qq_official_question_notices WHERE notice_id=?',
                    (notice['notice_id'],))[0]
                self.assertEqual(row,dict(state=terminal,delivery_state='committed',receipt_id=real_receipt))

    def test_terminal_notice_without_plan_cannot_attach_plan_or_receipt_later(self):
        source = self.notice(); notice = self.question_notice(source)
        self.query("UPDATE qq_official_question_notices SET state='cancelled',delivery_state='cancelled',updated_at=? WHERE notice_id=?",
            (self.now+1,notice['notice_id']))
        with self.pg_error(pattern='question_notice_original_source_required'):
            self.notice_plan(source)
        # Even if a plan existed first, terminal row with plan_id=NULL cannot bind it.
        source2 = self.next_source(); notice2 = self.question_notice(source2); plan2 = self.notice_plan(source2)
        self.query("UPDATE qq_official_question_notices SET state='cancelled',delivery_state='cancelled',updated_at=? WHERE notice_id=?",
            (self.now+1,notice2['notice_id']))
        with self.pg_error(pattern='immutable_question_notice'):
            self.query("UPDATE qq_official_question_notices SET plan_id=?,updated_at=? WHERE notice_id=?",
                (plan2,self.now+2,notice2['notice_id']))

    def test_notice_table_blocks_delete_truncate_and_reset_restores_guard(self):
        source = self.notice(); notice = self.question_notice(source)
        with self.pg_error(pattern='immutable_question_notice'):
            self.query('DELETE FROM qq_official_question_notices WHERE notice_id=?',(notice['notice_id'],))
        with self.pg_error(pattern='immutable_question_notice'):
            self.query('TRUNCATE qq_official_question_notices')
        workspace = type(self).__dict__['_owned_postgres_fixture']
        workspace.reset()
        self.assertEqual(self.query('SELECT * FROM qq_official_question_notices'),[])
        with self.pg_error(pattern='immutable_question_notice'):
            self.query('TRUNCATE qq_official_question_notices')
