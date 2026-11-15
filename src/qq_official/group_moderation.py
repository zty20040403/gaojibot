"""Fixed QQ group routes for opt-in probing; not a model/VM tool or capability proof."""
from datetime import datetime, timedelta, timezone
import re
import time

from .http import ProtocolResponse


_ACTIONS = {'bot_state','mutes','mute','update_mute','unmute','recall'}


def _identifier(value):
    if not isinstance(value,str) or re.fullmatch(r'[A-Za-z0-9_-]{1,256}',value) is None:
        raise ValueError('Invalid group moderation identifier')
    return value


class GroupModerationProtocol:
    def __init__(self,rest,*,clock=time.time):
        self.rest,self.clock = rest,clock

    async def request(self,group,action,*,member=None,seconds=None,message=None) -> ProtocolResponse:
        if not isinstance(action,str) or action not in _ACTIONS:
            raise ValueError('Unknown group moderation action')
        prefix = '/v2/groups/'+_identifier(group)
        method,payload = 'GET',None
        if action in {'mute','update_mute','unmute'}:
            if message is not None:
                raise ValueError('Mute does not accept a message')
            member = _identifier(member)
            if action=='unmute':
                if seconds is not None:
                    raise ValueError('Unmute does not accept a duration')
                expiry,operation = '','del'
            else:
                if type(seconds) is not int or not 1<=seconds<=2592000:
                    raise ValueError('Mute duration must be 1 to 2592000 seconds')
                expiry = datetime.fromtimestamp(int(self.clock())+seconds,timezone(timedelta(hours=8))).isoformat()
                operation = 'update' if action=='update_mute' else 'add'
            method,path = 'POST',prefix+'/restrict_chat_setting'
            payload = {'members':[{'op':operation,'member_openid':member,'mute_expire_at':expiry}]}
        elif action=='recall':
            if member is not None or seconds is not None:
                raise ValueError('Recall does not accept a member or duration')
            method,path = 'DELETE',prefix+'/messages/'+_identifier(message)
        else:
            if member is not None or seconds is not None or message is not None:
                raise ValueError('Read does not accept mutation arguments')
            path = prefix+('/bot_state' if action=='bot_state' else '/restrict_chat_setting')
        token = await self.rest.token_manager.get()
        # No auth or transport retry. The physical source/deadline guard still
        # runs after token preparation and, when selected, TLS/header waits.
        return await self.rest._request_response_with_token(method,path,token,payload,
            stage='moderation',empty_moderation_delete=action=='recall')
