"""Bridge an agent's RPC stdio to the host tunnel from inside its VM.

Usage: GAOJI_JOB_TOKEN=... python3 sandbox_agent_bridge.py HOST PORT -- COMMAND [ARG ...]
Either end closing terminates the child; diagnostics go to agent.err, never stdout.
"""
import asyncio
import contextlib
import os
import sys


async def pump(source, sink):
    try:
        while line := await source.readline():
            sink.write(line)
            await sink.drain()
    finally:
        sink.close()


async def main(host, port, command):
    if not command:
        raise ValueError('agent command required')
    reader, writer = await asyncio.open_connection(host, int(port), limit=2**24)
    process = None
    tasks = []
    try:
        writer.write(os.environ['GAOJI_JOB_TOKEN'].encode() + b'\n')
        await writer.drain()
        with open(os.environ.get('GAOJI_AGENT_ERR', 'agent.err'), 'ab', buffering=0) as errors:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=errors, cwd=os.environ.get('GAOJI_PI_CWD', '/work'), limit=2**24,
            )
        tasks = [asyncio.create_task(pump(reader, process.stdin)),
                 asyncio.create_task(pump(process.stdout, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()


if __name__ == '__main__':
    if len(sys.argv) < 5 or sys.argv[3] != '--':
        raise SystemExit('usage: sandbox_agent_bridge.py HOST PORT -- COMMAND [ARG ...]')
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[4:]))
