"""Short-transaction window, quota and send-capability accounting.

The outbox owns delivery/attempt creation, human admission, durable outcomes and
network I/O. These helpers neither commit nor sleep. Call them in the same
transaction as begin_direct_attempt; any reservation failure requires rollback.
A local rate permit is scheduling evidence, never human/platform authorization.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from src.bot_storage import StoreCursor
from src.plugins.ai_chat.platform_types import EndpointKey

from .lease import LeaseToken, assert_epoch
from .rate_policy import (
    Certification, LocalRatePermit, QuotaSpec, SendMode, VerifiedDayBoundary,
    daily_quota_spec, effective_certification,
)

_PURPOSES = frozenset({'final', 'requested_artifact', 'background_result', 'progress', 'unsolicited', 'binding_otp'})
_PROBE_PURPOSES = frozenset({'final', 'requested_artifact', 'background_result'})
_LOW_PRIORITY = frozenset({'background_result', 'unsolicited'})
_MAX_TIME = 2**63 - 1


class SendPolicyError(RuntimeError):
    """Fixed safe reason only; caller must roll back the transaction."""


@dataclass(frozen=True)
class PolicyWait:
    reason: str
    retry_at: int | None = None


@dataclass(frozen=True)
class SendPolicyChoice:
    endpoint: EndpointKey = field(repr=False)
    endpoint_generation: int
    route_epoch: int
    capability_revision: int
    send_mode: SendMode
    window_id: str | None = field(repr=False)
    credential_kind: str | None
    credential_value: str | None = field(repr=False)
    msg_seq: int | None
    send_by_at: int
    quota_specs: tuple[QuotaSpec, ...] = field(repr=False)
    requires_probe: bool
    purpose: str
    certification: str
    rate_permit: LocalRatePermit | None = field(repr=False)


def _time(value: int) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_TIME:
        raise SendPolicyError('invalid_timestamp')


def _target(cursor, token, endpoint):
    if not isinstance(endpoint, EndpointKey) or endpoint.account.platform != 'qq-official':
        raise SendPolicyError('official_endpoint_required')
    if token.account_key != endpoint.account.key:
        raise SendPolicyError('account_mismatch')
    assert_epoch(cursor, token)
    row = cursor.execute('''SELECT * FROM qq_official_endpoints
        WHERE endpoint_key=? AND account_key=? FOR UPDATE''', (endpoint.key, endpoint.account.key)).fetchone()
    if row is None:
        raise SendPolicyError('endpoint_missing')
    if row['kind'] != endpoint.kind or row['native_conversation_id'] != endpoint.native_conversation_id:
        raise SendPolicyError('endpoint_identity_conflict')
    return row


def _available_window(row, endpoint_row, now, purpose):
    floor = 1 if purpose in _LOW_PRIORITY else 0
    return (
        row is not None and row['state'] == 'active' and row['issued_at'] is not None
        and row['endpoint_generation'] == endpoint_row['generation']
        and row['route_epoch'] == endpoint_row['route_epoch']
        and row['expires_at'] > now and row['next_seq'] <= 65535
        and row['max_replies'] - row['used_count'] - row['reserved_count'] > floor
    )


def _quota_row(cursor, spec):
    row = cursor.execute('SELECT * FROM qq_official_quota_periods WHERE quota_key=? FOR UPDATE', (spec.quota_key,)).fetchone()
    if row is None:
        return None
    expected = (spec.account_key, spec.endpoint_key, spec.scene, spec.send_mode, spec.metric,
                spec.period_start_at, spec.period_end_at, spec.reset_verified)
    actual = tuple(row[name] for name in ('account_key', 'endpoint_key', 'scene', 'send_mode', 'metric',
                                        'period_start_at', 'period_end_at', 'reset_verified'))
    if actual != expected:
        raise SendPolicyError('quota_identity_conflict')
    if row['policy_revision'] > spec.policy_revision:
        raise SendPolicyError('quota_policy_changed')
    return row


def _prior_quota_wait(cursor, spec, now):
    # Switching day policies never erases unsettled charges. A late positive
    # receipt conservatively blocks its observation period, not every future day.
    previous = cursor.execute('''SELECT * FROM qq_official_quota_periods
        WHERE account_key=? AND endpoint_key=? AND scene=? AND send_mode=? AND metric=?
          AND quota_key<>? ORDER BY quota_key FOR UPDATE''',
        (spec.account_key, spec.endpoint_key, spec.scene, spec.send_mode, spec.metric, spec.quota_key)).fetchall()
    for old in previous:
        if old['reserved_count'] > 0:
            return PolicyWait('quota_unsettled_period', spec.period_end_at)
        if old['used_count'] > 0 and (not spec.reset_verified or old['updated_at'] >= spec.period_start_at):
            return PolicyWait('quota_period_requires_review', spec.period_end_at)
        if (old['blocked_until'] or 0) > now:
            return PolicyWait('server_cooldown', old['blocked_until'])
    return None


def _quota_admission(cursor, endpoint, now, verified_boundary):
    # The unverified quota's identity deliberately does not include a date.
    # Discover its original start without creating a row during preflight.
    spec = daily_quota_spec(endpoint, 'proactive', now=now, unverified_period_start=now,
                            verified_boundary=verified_boundary)
    initial = cursor.execute('SELECT * FROM qq_official_quota_periods WHERE quota_key=? FOR UPDATE', (spec.quota_key,)).fetchone()
    if initial is not None and verified_boundary is None:
        spec = daily_quota_spec(endpoint, 'proactive', now=now, unverified_period_start=initial['period_start_at'])
    row = _quota_row(cursor, spec)
    previous = _prior_quota_wait(cursor, spec, now)
    if previous is not None:
        return previous
    if row is not None:
        if (row['blocked_until'] or 0) > now:
            return PolicyWait('server_cooldown', row['blocked_until'])
        if row['used_count'] + row['reserved_count'] >= min(spec.limit_count, row['limit_count']):
            return PolicyWait('quota_exhausted' if spec.reset_verified else 'quota_reset_unverified', spec.period_end_at)
    return spec


from .control_receipts import control_policy_authority


def check_send_policy(
    cursor: StoreCursor, *, token: LeaseToken, endpoint: EndpointKey, endpoint_row: Mapping,
    source_native_id: str | None, purpose: str, deadline: int, now: int,
    rate_permit: LocalRatePermit | None = None, certification: Certification = 'unknown',
    allow_newer_windows: bool = False, allow_proactive_probe: bool = False,
    verified_boundary: VerifiedDayBoundary | None = None, control_reply_id=None,
) -> SendPolicyChoice | PolicyWait:
    """Lock/check candidates; never allocate a sequence or charge an attempt.

    Caller must already revalidate human admission and the frozen plan's route.
    None permit supports a preliminary probe only; reserve requires a permit.
    Source native ID refers to an actual incoming message, not a quote index.
    """
    _time(now)
    _time(deadline)
    if purpose not in _PURPOSES or type(allow_newer_windows) is not bool or type(allow_proactive_probe) is not bool:
        raise SendPolicyError('invalid_policy_options')
    if source_native_id is not None and (not isinstance(source_native_id, str) or not 1 <= len(source_native_id.encode('utf-8')) <= 256):
        raise SendPolicyError('invalid_source_message')
    current = _target(cursor, token, endpoint)
    for key in ('endpoint_key', 'account_key', 'generation', 'route_epoch', 'capability_revision'):
        if endpoint_row.get(key) != current[key]:
            raise SendPolicyError('endpoint_snapshot_changed')
    if deadline <= now:
        return PolicyWait('delivery_expired')
    if purpose == 'progress':
        return PolicyWait('progress_suppressed')
    if current['binding_state'] == 'revoked' or current['membership_state'] == 'absent':
        return PolicyWait('endpoint_revoked')
    if current['membership_state'] != 'present':
        return PolicyWait('membership_unknown')
    control_view = None
    if control_reply_id is not None:
        control_view = control_policy_authority(cursor, token, control_id=control_reply_id, endpoint=endpoint,
            source_native_id=source_native_id, deadline=deadline, now=now)
        allow_newer_windows = allow_proactive_probe = False
    if control_view is None and endpoint.kind == 'group' and current['binding_state'] != 'active':
        return PolicyWait('endpoint_unbound')
    window = None
    if source_native_id is not None:
        candidate = cursor.execute('''SELECT * FROM qq_official_reply_windows
            WHERE account_key=? AND endpoint_key=? AND credential_kind='msg_id' AND credential_value=? FOR UPDATE''',
            (endpoint.account.key, endpoint.key, source_native_id)).fetchone()
        if _available_window(candidate, current, now, purpose):
            window = candidate
    if window is None and allow_newer_windows:
        # Newer-window reuse is disabled without explicit trusted probe policy.
        candidates = cursor.execute('''SELECT * FROM qq_official_reply_windows
            WHERE account_key=? AND endpoint_key=? AND credential_kind='msg_id' AND state='active'
              AND expires_at>? ORDER BY issued_at DESC,window_id FOR UPDATE''',
            (endpoint.account.key, endpoint.key, now)).fetchall()
        window = next((row for row in candidates if _available_window(row, current, now, purpose)), None)
    probe = False
    if window is not None:
        mode = 'passive'
    elif control_view is not None:
        return PolicyWait('waiting_reply_window')
    elif current['proactive_state'] == 'allowed':
        mode = 'proactive'
    elif (current['proactive_state'] == 'unknown' and current['proactive_probe_attempt_id'] is None
          and allow_proactive_probe and purpose in _PROBE_PURPOSES):
        mode, probe = 'proactive', True
    else:
        return PolicyWait('waiting_reply_window')
    profile = effective_certification(certification)
    if rate_permit is not None and (not isinstance(rate_permit, LocalRatePermit)
                                    or not rate_permit.matches(endpoint, mode, certification=profile)):
        return PolicyWait('mode_changed')
    cooldown = current[mode + '_blocked_until']
    if cooldown > now:
        return PolicyWait('server_cooldown', cooldown)
    quota_specs = ()
    send_by = min(deadline, window['expires_at']) if window is not None else deadline
    if mode == 'proactive':
        spec = _quota_admission(cursor, endpoint, now, verified_boundary)
        if isinstance(spec, PolicyWait):
            return spec
        quota_specs = (spec,)
        if spec.period_end_at is not None:
            send_by = min(send_by, spec.period_end_at)
    return SendPolicyChoice(endpoint, current['generation'], current['route_epoch'], current['capability_revision'],
        mode, window['window_id'] if window else None, window['credential_kind'] if window else None,
        window['credential_value'] if window else None, window['next_seq'] if window else None,
        send_by, quota_specs, probe, purpose, profile, rate_permit)


def _attempt(cursor, token, attempt_id):
    if type(attempt_id) is not int or attempt_id < 1:
        raise SendPolicyError('invalid_attempt')
    assert_epoch(cursor, token)
    row = cursor.execute('SELECT * FROM delivery_attempts WHERE attempt_id=? AND account_key=? FOR UPDATE',
                         (attempt_id, token.account_key)).fetchone()
    if row is None:
        raise SendPolicyError('attempt_account_mismatch')
    return row


def reserve_send_policy(cursor: StoreCursor, *, token: LeaseToken, choice: SendPolicyChoice, attempt_id: int, now: int, control_reply_id=None) -> None:
    """Reserve after attempt insertion; every mutation shares the caller's TX.

    Choice is single-use, not a recovery command. A duplicate window reservation
    fails its seq CAS and requires rollback; it can never charge the next seq.
    """
    _time(now)
    if not isinstance(choice, SendPolicyChoice) or not isinstance(choice.rate_permit, LocalRatePermit):
        raise SendPolicyError('rate_permit_required')
    if not choice.rate_permit.matches(choice.endpoint, choice.send_mode, certification=choice.certification):
        raise SendPolicyError('rate_permit_mismatch')
    current = _target(cursor, token, choice.endpoint)
    control_view = None
    if control_reply_id is not None:
        control_view = control_policy_authority(cursor, token, control_id=control_reply_id, endpoint=choice.endpoint,
            source_native_id=choice.credential_value, deadline=choice.send_by_at, now=now)
        if choice.send_mode != 'passive' or choice.window_id != control_view.window_id:
            raise SendPolicyError('control_window_changed')
    if (choice.purpose not in _PURPOSES or choice.purpose == 'progress'
            or current['membership_state'] != 'present' or current['binding_state'] == 'revoked'
            or (control_view is None and choice.endpoint.kind == 'group' and current['binding_state'] != 'active')):
        raise SendPolicyError('send_admission_changed')
    if (current['generation'], current['route_epoch'], current['capability_revision']) != (
            choice.endpoint_generation, choice.route_epoch, choice.capability_revision):
        raise SendPolicyError('endpoint_snapshot_changed')
    if now >= choice.send_by_at or current[choice.send_mode + '_blocked_until'] > now:
        raise SendPolicyError('send_deadline_or_cooldown_changed')
    row = _attempt(cursor, token, attempt_id)
    expected = (choice.endpoint.key, token.epoch, choice.endpoint_generation, choice.route_epoch,
                choice.capability_revision, choice.send_mode, choice.window_id, choice.msg_seq, choice.send_by_at)
    actual = tuple(row[key] for key in ('endpoint_key', 'fencing_epoch', 'endpoint_generation', 'route_epoch',
                                      'capability_revision', 'send_mode', 'window_id', 'msg_seq', 'send_by_at'))
    if actual != expected or row['state'] != 'reserved' or row['acceptance'] != 'unknown':
        raise SendPolicyError('attempt_policy_mismatch')
    if row['window_charge_state'] != ('reserved' if choice.send_mode == 'passive' else 'none'):
        raise SendPolicyError('attempt_charge_mismatch')
    if (choice.send_mode == 'passive' and choice.quota_specs) or (choice.send_mode == 'proactive' and len(choice.quota_specs) != 1):
        raise SendPolicyError('quota_policy_missing')
    part = cursor.execute('SELECT purpose,payload_mode FROM deliveries WHERE delivery_id=? AND account_key=?',
                          (row['delivery_id'], token.account_key)).fetchone()
    if part is None or part['purpose'] != choice.purpose:
        raise SendPolicyError('attempt_purpose_mismatch')
    if choice.window_id is not None:
        floor = 1 if choice.purpose in _LOW_PRIORITY else 0
        cursor.execute('''UPDATE qq_official_reply_windows SET reserved_count=reserved_count+1,next_seq=next_seq+1,updated_at=?
            WHERE window_id=? AND account_key=? AND endpoint_key=? AND endpoint_generation=? AND route_epoch=?
              AND state='active' AND issued_at IS NOT NULL AND expires_at>? AND expires_at>=?
              AND next_seq=? AND next_seq<=65535 AND max_replies-used_count-reserved_count>?''',
            (now, choice.window_id, token.account_key, choice.endpoint.key, choice.endpoint_generation, choice.route_epoch,
             now, choice.send_by_at, choice.msg_seq, floor))
        if cursor.rowcount != 1:
            raise SendPolicyError('window_reservation_changed')
    for spec in sorted(choice.quota_specs, key=lambda item: item.quota_key):
        if (not isinstance(spec, QuotaSpec) or spec.account_key != token.account_key
                or spec.endpoint_key != choice.endpoint.key or spec.send_mode != choice.send_mode
                or spec.metric != 'endpoint_messages_per_day'
                or spec.scene != ('group' if choice.endpoint.kind == 'group' else 'c2c')):
            raise SendPolicyError('quota_target_mismatch')
        if _prior_quota_wait(cursor, spec, now) is not None:
            raise SendPolicyError('quota_history_changed')
        cursor.execute('''INSERT INTO qq_official_quota_periods
            (quota_key,account_key,endpoint_key,scene,send_mode,metric,period_start_at,period_end_at,reset_verified,
             limit_count,policy_revision,release_policy,evidence_code,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(quota_key) DO NOTHING''',
            (spec.quota_key,spec.account_key,spec.endpoint_key,spec.scene,spec.send_mode,spec.metric,spec.period_start_at,
             spec.period_end_at,spec.reset_verified,spec.limit_count,spec.policy_revision,spec.release_policy,spec.evidence_code,now,now))
        quota = _quota_row(cursor, spec)
        if (quota['blocked_until'] or 0) > now:
            raise SendPolicyError('quota_cooldown_changed')
        cursor.execute('''INSERT INTO delivery_attempt_quotas
            (attempt_id,quota_key,account_key,endpoint_key,quota_endpoint_key,units,charge_state,created_at,updated_at)
            VALUES (?,?,?,?,?,1,'reserved',?,?) ON CONFLICT(attempt_id,quota_key) DO NOTHING''',
            (attempt_id,spec.quota_key,token.account_key,choice.endpoint.key,spec.endpoint_key,now,now))
        if cursor.rowcount == 1:
            cursor.execute('''UPDATE qq_official_quota_periods SET reserved_count=reserved_count+1,updated_at=?
                WHERE quota_key=? AND account_key=? AND used_count+reserved_count<?''',
                (now,spec.quota_key,token.account_key,min(quota['limit_count'],spec.limit_count)))
            if cursor.rowcount != 1:
                raise SendPolicyError('quota_reservation_changed')
    if choice.requires_probe:
        if choice.purpose not in _PROBE_PURPOSES or choice.send_mode != 'proactive':
            raise SendPolicyError('invalid_proactive_probe')
        cursor.execute('''UPDATE qq_official_endpoints SET proactive_probe_attempt_id=?,proactive_probe_at=?,updated_at=?
            WHERE endpoint_key=? AND account_key=? AND proactive_state='unknown' AND proactive_probe_attempt_id IS NULL''',
            (attempt_id,now,now,choice.endpoint.key,token.account_key))
        if cursor.rowcount != 1:
            raise SendPolicyError('proactive_probe_consumed')
    elif choice.send_mode == 'proactive' and current['proactive_state'] != 'allowed':
        raise SendPolicyError('proactive_permission_changed')


def _settlement_target(state, result, release_confirmed):
    target = 'spent' if result == 'accepted' else 'released' if (
        result == 'proven_unsent' or (result == 'confirmed_unaccepted' and release_confirmed)) else None
    if target is None or state == target:
        return None
    if state != 'reserved':
        raise SendPolicyError('charge_settlement_conflict')
    return target


def settle_send_policy(
    cursor: StoreCursor, *, token: LeaseToken, attempt_id: int,
    result: Literal['accepted', 'proven_unsent', 'confirmed_unaccepted', 'ambiguous'], now: int,
    release_confirmed_window: bool = False,
) -> None:
    """CAS charges only. Current holder may settle an old epoch's exact receipt.

    proven_unsent is trusted current-process control-flow evidence supplied by
    the outbox, never a user/model boolean. This grants no authority to resend.
    """
    _time(now)
    if result not in {'accepted','proven_unsent','confirmed_unaccepted','ambiguous'} or type(release_confirmed_window) is not bool:
        raise SendPolicyError('invalid_settlement')
    attempt = _attempt(cursor, token, attempt_id)
    if result == 'proven_unsent' and attempt['fencing_epoch'] != token.epoch:
        raise SendPolicyError('stale_unsent_proof')
    if attempt['window_id'] is not None:
        target = _settlement_target(attempt['window_charge_state'], result, release_confirmed_window)
        if target is not None:
            cursor.execute('''UPDATE qq_official_reply_windows SET reserved_count=reserved_count-1,used_count=used_count+?,updated_at=?
                WHERE window_id=? AND account_key=? AND endpoint_key=? AND reserved_count>=1''',
                (1 if target=='spent' else 0,now,attempt['window_id'],token.account_key,attempt['endpoint_key']))
            if cursor.rowcount != 1:
                raise SendPolicyError('window_charge_missing')
            cursor.execute('UPDATE delivery_attempts SET window_charge_state=? WHERE attempt_id=? AND account_key=?',
                           (target,attempt_id,token.account_key))
    charges = cursor.execute('''SELECT c.quota_key,c.charge_state,c.units,q.release_policy FROM delivery_attempt_quotas c
        JOIN qq_official_quota_periods q ON q.quota_key=c.quota_key AND q.account_key=c.account_key
        WHERE c.attempt_id=? AND c.account_key=? ORDER BY c.quota_key FOR UPDATE OF c,q''',
        (attempt_id,token.account_key)).fetchall()
    for charge in charges:
        target = _settlement_target(charge['charge_state'],result,charge['release_policy']=='confirmed_unaccepted')
        if target is None:
            continue
        units = charge['units']
        cursor.execute('''UPDATE qq_official_quota_periods SET reserved_count=reserved_count-?,used_count=used_count+?,updated_at=?
            WHERE quota_key=? AND account_key=? AND reserved_count>=?''',
            (units,units if target=='spent' else 0,now,charge['quota_key'],token.account_key,units))
        if cursor.rowcount != 1:
            raise SendPolicyError('quota_charge_missing')
        cursor.execute('UPDATE delivery_attempt_quotas SET charge_state=?,updated_at=? WHERE attempt_id=? AND quota_key=? AND account_key=?',
                       (target,now,attempt_id,charge['quota_key'],token.account_key))


def apply_policy_rejection(cursor: StoreCursor, *, token: LeaseToken, attempt_id: int, category: str, now: int) -> None:
    """Apply only the outbox's already verified refusal classification."""
    _time(now)
    attempt = _attempt(cursor, token, attempt_id)
    if category == 'window_exhausted' and attempt['send_mode'] == 'passive':
        cursor.execute("UPDATE qq_official_reply_windows SET state='expired',updated_at=? WHERE window_id=? AND account_key=? AND endpoint_key=? AND state='active'",
                       (now,attempt['window_id'],token.account_key,attempt['endpoint_key']))
    elif category == 'proactive_denied' and attempt['send_mode'] == 'proactive':
        cursor.execute("""UPDATE qq_official_endpoints SET proactive_state='denied',capability_revision=capability_revision+1,updated_at=?
            WHERE endpoint_key=? AND account_key=? AND proactive_state<>'denied'
              AND generation=? AND route_epoch=? AND capability_revision=?""",
            (now,attempt['endpoint_key'],token.account_key,attempt['endpoint_generation'],attempt['route_epoch'],attempt['capability_revision']))
    else:
        raise SendPolicyError('rejection_mode_mismatch')


def read_server_cooldown(cursor: StoreCursor, endpoint: EndpointKey, send_mode: SendMode) -> int:
    if not isinstance(endpoint, EndpointKey) or endpoint.account.platform != 'qq-official' or send_mode not in {'passive','proactive'}:
        raise SendPolicyError('invalid_cooldown_target')
    column = send_mode + '_blocked_until'
    row = cursor.execute(f'SELECT {column} FROM qq_official_endpoints WHERE endpoint_key=? AND account_key=?',
                         (endpoint.key,endpoint.account.key)).fetchone()
    return int(row[column]) if row is not None else 0


def extend_server_cooldown(cursor: StoreCursor, *, token: LeaseToken, endpoint: EndpointKey, send_mode: SendMode, blocked_until: int, now: int) -> None:
    _time(now)
    _time(blocked_until)
    if send_mode not in {'passive','proactive'}:
        raise SendPolicyError('invalid_cooldown_mode')
    _target(cursor,token,endpoint)
    column = send_mode + '_blocked_until'
    cursor.execute(f'UPDATE qq_official_endpoints SET {column}=GREATEST({column},?),updated_at=? WHERE endpoint_key=? AND account_key=?',
                   (blocked_until,now,endpoint.key,token.account_key))
