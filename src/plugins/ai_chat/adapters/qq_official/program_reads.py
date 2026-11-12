"""VM read routes reuse scoped host tools, never a human or model channel."""
import asyncio
import json
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from ...background_tools import BackgroundPlatform
from ...platform_types import ProgramToolSource
from ...tools import neutral_registry_for
from ...tools.state import ToolContext, TurnState

READ_TIMEOUT = 60
ROUTES = {'/vm/history':'history','/vm/web-search':'web_search','/vm/web-fetch':'web_fetch'}


class ProgramReads:
    def __init__(self, app):
        self.app,self.active = app,{}

    async def handle(self, request):
        scope = request.state.channel
        endpoint = scope.identity.endpoint
        count = self.active.get(endpoint.key,0)
        if count>=4 or sum(self.active.values())>=16:
            return JSONResponse({'error':'program_reads_busy'},429)
        self.active[endpoint.key] = count+1
        try:
            body = bytearray()
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    if len(body)+len(chunk)>16384:
                        return JSONResponse({'error':'program_request_too_large'},413)
                    body.extend(chunk)
            arguments = json.loads(body)
            capabilities = {'context'}
            if self.app.settings.search_enabled:
                capabilities.add('web')
            context = ToolContext(SimpleNamespace(context=self.app),program_source=ProgramToolSource(
                endpoint,'manual',scope.caller_ref),platform=BackgroundPlatform(endpoint),capabilities=frozenset(capabilities))
            registry = neutral_registry_for(context)
            name = ROUTES[request.url.path]
            definition = next((item['function'] for item in registry.definitions(context) if item['function']['name']==name),None)
            if definition is None:
                return JSONResponse({'error':'program_read_disabled'},403)
            if request.query_params or not Draft202012Validator(definition['parameters']).is_valid(arguments):
                return JSONResponse({'error':'invalid_program_arguments'},400)
            await scope.current()
            async with asyncio.timeout(READ_TIMEOUT):
                result = await self.execute(request,registry,TurnState(context),name,arguments)
            await scope.current()  # Do not return data after permission/boot revocation.
            result = json.loads(result)
            return JSONResponse(result,200 if result.get('ok') else (400 if name=='history' else 502))
        except ClientDisconnect:
            return JSONResponse({'error':'program_client_disconnected'},499)
        except PermissionError:
            return JSONResponse({'error':'vm_source_revoked'},403)
        except (ValueError,TypeError):
            return JSONResponse({'error':'invalid_program_request'},400)
        except TimeoutError:
            return JSONResponse({'error':'program_read_timeout'},408)
        except Exception as error:
            self.app.logger.warning('Program read failed error=%s',type(error).__name__)
            return JSONResponse({'error':'program_read_unavailable'},503)
        finally:
            remaining = self.active[endpoint.key]-1
            if remaining:
                self.active[endpoint.key] = remaining
            else:
                self.active.pop(endpoint.key)

    async def execute(self, request, registry, turn, name, arguments):
        async def disconnected():
            while (await request.receive())['type']!='http.disconnect':
                pass
        work = asyncio.create_task(registry.execute(turn,name,arguments))
        closed = asyncio.create_task(disconnected())
        try:
            await asyncio.wait((work,closed),return_when=asyncio.FIRST_COMPLETED)
            if closed.done():
                closed.result()
                raise ClientDisconnect()
            return work.result()
        finally:
            for task in (work,closed):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(work,closed,return_exceptions=True)
