"""Inside the conversation VM: carry `codex app-server` stdio to the host's tunnel.

Usage: GAOJI_JOB_TOKEN=... CODEX_HOME=... python3 sandbox_codex_bridge.py HOST PORT
Codex exits when the host closes the connection, and the connection closes when Codex exits.
"""
import asyncio
import os
import sys


async def pump(source, sink):
    try:
        while line := await source.readline():
            sink.write(line)
            await sink.drain()
    finally:
        sink.close()


async def main(host, port):
    reader, writer = await asyncio.open_connection(host, int(port), limit=2**24)
    writer.write(os.environ['GAOJI_JOB_TOKEN'].encode() + b'\n')
    await writer.drain()
    codex = await asyncio.create_subprocess_exec('codex', 'app-server', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, cwd='/work', limit=2**24)
    tasks = [asyncio.create_task(pump(reader, codex.stdin)), asyncio.create_task(pump(codex.stdout, writer))]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()
    if codex.returncode is None:
        codex.terminate()
        try:
            await asyncio.wait_for(codex.wait(), 10)
        except TimeoutError:
            codex.kill()


if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:3]))
