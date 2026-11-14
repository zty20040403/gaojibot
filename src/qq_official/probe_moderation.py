"""Owner-run, one-subject F7 experiment. Not imported by application startup."""
import asyncio
from dataclasses import dataclass,field
from datetime import datetime
import re
import secrets
import time

from .errors import QQAPIError
from .events import EventParseError,parse_dispatch
from .group_moderation import _identifier
from .http import scoped_request_guard
from .probe import ProtectedReport,Pseudonyms


@dataclass(frozen=True)
class ModerationProbeOptions:
    report: object
    app_id: str = field(repr=False)
    group: str = field(repr=False)
    member: str = field(repr=False)
    checks: tuple[str,...] = ('read',)
    duration: float = 120
    environment: str = 'production'

    def validate(self):
        _identifier(self.group); _identifier(self.member)
        if (not isinstance(self.checks,tuple) or not self.checks or any(not isinstance(check,str) for check in self.checks)
            or len(set(self.checks))!=len(self.checks) or set(self.checks)-{'read','mute','recall'}
            or type(self.duration) not in {int,float} or not 0<self.duration<=300 or self.environment not in {'production','sandbox','test'}):
            raise ValueError('Invalid moderation experiment scope')


class ProbeStopped(Exception):
    pass


def _rules(body):
    if not isinstance(body.get('members'),list):raise ProbeStopped('invalid_mute_rules')
    values = {}
    for row in body['members']:
        try:
            member = _identifier(row['member_openid'])
            expiry = datetime.fromisoformat(row['mute_expire_at'])
            if expiry.tzinfo is None or member in values:raise ValueError
            values[member] = expiry.timestamp()
        except (TypeError,ValueError,KeyError):raise ProbeStopped('invalid_mute_rules') from None
    return values


async def run_moderation_probe(options,protocol,gateway,*,confirm,output=print,clock=time.time):
    options.validate()
    credentials = protocol.rest.token_manager.credentials
    if options.app_id!=credentials.app_id:raise ValueError('Moderation account mismatch')
    report_file,aliases = ProtectedReport(options.report),Pseudonyms()
    account = aliases.get('account',options.app_id)
    report = {'status':'awaiting_confirmation','account':account,'environment':options.environment,
        'evidence':'local_fake' if credentials.is_test else 'live_experiment','checks':list(options.checks),
        'group':aliases.get('group',options.group,namespace=account),
        'member':aliases.get('member',options.member,namespace=options.group),'requests':[],
        'invalid_events':0,'dropped_events':0,'manual_verification_required':True}
    report['max_business_requests'] = sum({'read':2,'mute':8,'recall':2}[check] for check in options.checks)
    report_file.write(report)
    output('测试范围：'+report['account']+' / '+report['group']+' / '+report['member']+'；步骤 '+','.join(options.checks)
        +'；最多 '+str(report['max_business_requests'])+' 次业务HTTP（不含鉴权/网关），请先核对私有scope文件。')
    if confirm('Only the selected independent test group/member. Mute lasts 60s; recall deletes a fresh test message. YES?') is not True:
        report['status'] = 'cancelled'; report_file.write(report)
        return report
    deadline = clock()+options.duration
    marker = 'GAOJI_MODERATION_TEST_'+secrets.token_hex(8)
    output('请指定普通成员在测试群新发 @bot '+marker+'；只用这条新测试消息，不处理转发/引用。')
    queue,used = asyncio.Queue(maxsize=16),set()
    source = None

    async def observe(envelope):
        try:event = parse_dispatch(envelope)
        except EventParseError:
            report['invalid_events'] += 1
            return
        msg = event.message
        if (event.event_type not in {'GROUP_MESSAGE_CREATE','GROUP_AT_MESSAGE_CREATE'} or msg is None
            or msg.kind!='group' or msg.endpoint_id!=options.group or msg.author.native_id!=options.member
            or msg.author.is_bot or msg.author.platform_role!='member' or not msg.mentions_bot
            or not msg.message_id or msg.message_id in used or msg.occurred_at is None
            or not 0<=clock()-msg.occurred_at<120 or msg.message_type not in {None,0,1}
            or msg.attachments or msg.elements or msg.ark_data or msg.ref_msg_idx or msg.reference_conflict
            or re.sub(r'<@!?[A-Za-z0-9_-]{1,256}>','',msg.content).strip()!=marker):return
        used.add(msg.message_id)
        try:queue.put_nowait(msg)
        except asyncio.QueueFull:report['dropped_events'] += 1

    async def guard(_stage):
        if clock()>=deadline:raise PermissionError('original_probe_deadline')
        if gateway.health.get('message_gap'):raise PermissionError('gateway_message_gap')
        if source is not None and clock()>=source.occurred_at+120:raise PermissionError('original_test_message_expired')

    async def call(action,**arguments):
        entry = {'action':action,'status':'in_progress'}
        report['requests'].append(entry); report_file.write(report)
        try:
            with scoped_request_guard(guard,before_headers=True):
                result = await protocol.request(options.group,action,**arguments)
        except QQAPIError as error:
            entry.update(status='rejected' if error.confirmed_not_accepted else 'unknown',http_status=error.status,code=error.code,reason=error.category)
            raise ProbeStopped('request_'+entry['status']) from None
        except PermissionError as error:
            entry.update(status='not_run',reason=str(error))
            raise ProbeStopped(str(error)) from None
        else:
            entry.update(status='observed' if action in {'bot_state','mutes'} else 'accepted',http_status=result.status)
            report_file.write(report)
            return result.body

    async def require_admin():
        state = await call('bot_state')
        role = state.get('member_role')
        report['bot_role'] = role if role in {'owner','admin','member'} else 'unknown'
        if role not in {'owner','admin'}:raise ProbeStopped('bot_not_group_admin')
        try:bot = _identifier(state.get('member_openid'))
        except ValueError:raise ProbeStopped('bot_identity_unknown') from None
        if bot==options.member:raise ProbeStopped('test_subject_is_bot')

    task = asyncio.create_task(gateway.run(observe))
    async def fresh_source():
        waiter = asyncio.create_task(queue.get())
        try:
            done,_ = await asyncio.wait((waiter,task),return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:return waiter.result()
            if task.cancelled() or task.exception() is not None:raise ProbeStopped('gateway_connection_failed')
            if not queue.empty():return queue.get_nowait()
            raise ProbeStopped('gateway_ended_without_fresh_consent')
        finally:
            waiter.cancel()
            await asyncio.gather(waiter,return_exceptions=True)
    try:
        report['status'] = 'running'
        async with asyncio.timeout(options.duration):
            for check in options.checks:
                if check=='read':
                    state = await call('bot_state')
                    role = state.get('member_role')
                    report['bot_role'] = role if role in {'owner','admin','member'} else 'unknown'
                    report['muted_member_count'] = len(_rules(await call('mutes')))
                    continue
                output('等待下一条不同ID的新测试消息：'+marker)
                source = await fresh_source()
                report['source_message'] = aliases.get('message',source.message_id,namespace=options.group)
                await require_admin()
                if check=='recall':
                    await call('recall',message=source.message_id)
                    continue
                if options.member in _rules(await call('mutes')):raise ProbeStopped('test_member_already_has_rule')
                start = int(clock())
                await call('mute',member=options.member,seconds=60)
                expiry = _rules(await call('mutes')).get(options.member)
                if expiry is None or not start+58<=expiry<=start+61 or expiry<=clock():
                    raise ProbeStopped('temporary_mute_not_verified')
                report['temporary_mute_verified'] = True
                await require_admin()
                # Never clear a different, newly established mute or repeat an
                # unknown write. A test group must have no concurrent moderator;
                # QQ has no compare-and-swap, so this is not an atomic guarantee.
                if _rules(await call('mutes')).get(options.member)!=expiry:raise ProbeStopped('mute_changed_before_cleanup')
                await call('unmute',member=options.member)
                if options.member in _rules(await call('mutes')):raise ProbeStopped('unmute_not_verified')
                report['unmute_verified'] = True
        report['status'] = 'completed'
    except TimeoutError:
        report.update(status='stopped',reason='original_probe_deadline_or_no_fresh_consent')
    except ProbeStopped as error:
        report.update(status='stopped',reason=str(error))
    except asyncio.CancelledError:
        report.update(status='interrupted',reason='operator_cancelled')
        raise
    except Exception as error:
        report.update(status='failed',reason='local_or_gateway_error',error_type=type(error).__name__)
        raise
    finally:
        for entry in report['requests']:
            if entry['status']=='in_progress':entry.update(status='unknown',reason='interrupted_request_not_replayed')
        await gateway.stop()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        report_file.write(report)
    return report
