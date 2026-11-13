"""VM-only command admission/control; acknowledges persistence, not execution."""
import asyncio
import json
import re

from starlette.responses import JSONResponse

from .program_actions import ProgramDenied, REQUEST


class CommandHTTP:
    def __init__(self, runner):
        self.runner,self.calls = runner,runner.calls

    async def status(self, current, identifier, *, by_request=False):
        row = await self.calls(self.runner.commands.get,current,identifier,by_request=by_request)
        if row['notification_action_id'] is not None:
            row['notification'] = await self.calls(self.runner.lifecycle.actions.get,current,row['notification_action_id'])
        return row

    async def handle(self, request):
        scope = request.state.channel
        try:
            command_id = request.path_params.get('command_id')
            if command_id is not None and re.fullmatch(r'[0-9a-f]{32}',command_id) is None:
                raise ValueError()
            if request.method=='GET':
                if command_id is None:
                    if set(request.query_params)!={'request_id'} or REQUEST.fullmatch(request.query_params['request_id']) is None:
                        raise ValueError()
                elif request.query_params:
                    raise ValueError()
                await scope.current()
                return JSONResponse(await self.status(scope.identity,command_id or request.query_params['request_id'],by_request=command_id is None))
            if request.query_params:
                raise ValueError()
            body = bytearray()
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    if len(body)+len(chunk)>32768:
                        return JSONResponse({'error':'command_request_too_large'},413)
                    body.extend(chunk)
            await scope.current()
            data = json.loads(body)
            if command_id is not None:
                if data!={}:
                    raise ValueError()
                await self.calls(self.runner.commands.get,scope.identity,command_id)
                await self.calls(self.runner.commands.cancel,scope.identity,command_id)
                self.runner.changed.set()
                return JSONResponse(await self.status(scope.identity,command_id),202)
            if (type(data) is not dict or not {'argv','request_id'}<=data.keys() or data.keys()-{'argv','request_id','program'}):
                raise ValueError()
            if self.runner.closed or self.runner.task is None or self.runner.task.done():
                return JSONResponse({'error':'command_runner_unavailable'},503)
            row,_ = await self.calls(self.runner.commands.reserve,scope.identity,generation=scope.generation,
                program=data.get('program','manual'),request_id=data['request_id'],argv=data['argv'])
            self.runner.changed.set()
            result = await self.status(scope.identity,row['command_id'])
            result['instruction'] = '已登记，不等于命令已完成。不要 sleep 或轮询等待；完成后尝试通知，送达结果以状态为准。'
            return JSONResponse(result,202)
        except ProgramDenied as error:
            return JSONResponse({'error':str(error)},error.status)
        except PermissionError:
            return JSONResponse({'error':'vm_source_revoked'},403)
        except (ValueError,TypeError):
            return JSONResponse({'error':'invalid_command_request'},400)
        except TimeoutError:
            return JSONResponse({'error':'command_request_timeout'},408)
        except Exception as error:
            self.runner.logger.warning('Background command HTTP failed error=%s',type(error).__name__)
            return JSONResponse({'error':'command_request_unavailable'},503)
