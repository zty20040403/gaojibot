"""Bounded VM-only action routes; clients never supply an endpoint or actor."""
import asyncio
import json

from starlette.responses import JSONResponse

from .program_actions import ProgramDenied
from .program_delivery import say
from .send_types import OutboxInvariantError


class ProgramHTTP:
    def __init__(self, actions, runtime, calls, logger, *, programs=None, reads=None, stream=None, files=None):
        self.actions,self.runtime,self.calls,self.logger = actions,runtime,calls,logger
        self.programs,self.reads,self.stream,self.files = programs,reads,stream,files

    async def handle(self, request):
        if request.url.path=='/vm/events':
            return await self.stream.handle(request) if self.stream is not None else JSONResponse({'error':'program_events_unavailable'},503)
        from .program_reads import ROUTES
        if request.url.path in ROUTES:
            return await self.reads.handle(request) if self.reads is not None else JSONResponse({'error':'program_reads_unavailable'},503)
        scope = request.state.channel
        try:
            if request.method=='GET':
                action = request.path_params.get('action_id')
                if action is None and (set(request.query_params)!={'request_id'} or not request.query_params['request_id']):
                    raise ValueError()
                result = await self.calls(self.actions.get,scope.identity,action or request.query_params['request_id'],
                    by_request=action is None)
                return JSONResponse(result)
            sender = getattr(self.runtime,'sender',None)
            if sender is None:
                return JSONResponse({'error':'program_sender_unavailable'},503)
            body = bytearray()
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    if len(body)+len(chunk)>16384:
                        return JSONResponse({'error':'program_request_too_large'},413)
                    body.extend(chunk)
            await scope.current()
            data = json.loads(body)
            if request.url.path=='/vm/send-file':
                if (type(data) is not dict or not {'path','request_id'}<=data.keys()
                        or data.keys()-{'path','request_id','program','filename'}):
                    raise ValueError()
                if self.files is None:
                    return JSONResponse({'error':'program_files_unavailable'},503)
                result = await self.files.submit(scope.identity,generation=scope.generation,**data)
                return JSONResponse(result,{'completed':200,'failed':502,'ambiguous':409,'cancelled':409}.get(result['state'],202))
            if (type(data) is not dict or not {'text','request_id'}<=data.keys()
                    or data.keys()-{'text','request_id','program'}):
                raise ValueError()
            if request.url.path=='/vm/wake':
                if self.programs is None:
                    return JSONResponse({'error':'program_runner_unavailable'},503)
                result = await self.programs.submit(scope.identity,generation=scope.generation,
                    program=data.get('program','manual'),request_id=data['request_id'],text=data['text'])
                return JSONResponse(result,202)
            action,ref = await self.calls(say,self.actions,scope.identity,generation=scope.generation,
                program=data.get('program','manual'),request_id=data['request_id'],text=data['text'])
            try:
                async with asyncio.timeout(25):
                    await sender.send_delivery(ref.delivery_ids[0])
            except TimeoutError:
                # The existing sender owns the attempt; a timeout is not a
                # licence to create a second request/plan or claim success.
                pass
            result = await self.calls(self.actions.get,scope.identity,action)
            status = {'completed':200,'pending':202,'queued':202,'sending':202,'ambiguous':409,
                'cancelled':409,'failed':502}.get(result['state'],202)
            return JSONResponse(result,status)
        except ProgramDenied as error:
            return JSONResponse({'error':str(error)},error.status)
        except PermissionError:
            return JSONResponse({'error':'vm_source_revoked'},403)
        except (ValueError,TypeError):
            return JSONResponse({'error':'invalid_program_request'},400)
        except OutboxInvariantError:
            return JSONResponse({'error':'program_delivery_unavailable'},409)
        except TimeoutError:
            return JSONResponse({'error':'program_request_timeout'},408)
        except Exception as error:
            self.logger.warning('Program request failed error=%s',type(error).__name__)
            return JSONResponse({'error':'program_request_unavailable'},503)
