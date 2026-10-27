"""Opt-in real OCI smoke check using an already installed Python image.

Uses only throwaway, owner-labelled containers. Never builds/pulls images or
mounts host data. Run as a module with PYTHON_DOTENV_DISABLED=1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import time

import nonebot

PROBE = r'''
import json,os,sys,subprocess
tool={"name":"probe","description":"Local isolation fixture","inputSchema":{"type":"object","properties":{}}}
def probe():
 r={"uid":os.getuid(),"routes":open("/proc/net/route").read().splitlines(),"secret":os.getenv("GAOJI_MCP_SECRET_PROBE")}
 try:
  open("/mcp-write-probe","w").close()
  r["root_writable"]=True
 except OSError:
  r["root_writable"]=False
 with open("/tmp/probe","w") as f:f.write("#!/bin/sh\nexit 0\n")
 os.chmod("/tmp/probe",0o700)
 try:
  subprocess.run(["/tmp/probe"],check=True)
  r["tmp_executable"]=True
 except OSError:
  r["tmp_executable"]=False
 return {"content":[{"type":"text","text":json.dumps(r)}],"isError":False}
for line in sys.stdin:
 q=json.loads(line)
 if "id" not in q:continue
 m=q["method"]
 if m=="initialize":r={"protocolVersion":q["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"local-fixture","version":"1"}}
 elif m=="tools/list":r={"tools":[tool]}
 elif m=="tools/call":r=probe()
 else:r={}
 print(json.dumps({"jsonrpc":"2.0","id":q["id"],"result":r}),flush=True)
'''


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--image", required=True, help="Existing Python image reference; no pull")
    args = parser.parse_args()
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        raise RuntimeError("Disable dotenv before running isolated local verification")
    nonebot.init()
    from src.plugins.ai_chat.mcp_bridge.client import McpWireClient
    from src.plugins.ai_chat.mcp_bridge.stdio_transport import CONTAINER_PREFIX, DockerLauncher, StdioProfile, StdioSessions

    launcher = DockerLauncher(str(args.docker), str(args.socket), secrets.token_hex(16))
    image = (await launcher.command(["image", "inspect", "--format", "{{.Id}}", args.image])).decode().strip()
    profile = StdioProfile(image, ("/usr/local/bin/python", "-u", "-c", PROBE))
    wire = McpWireClient(StdioSessions({"probe": profile}, launcher), timeout_seconds=20)
    config = {"transport": "stdio", "profile": "probe"}
    os.environ["GAOJI_MCP_SECRET_PROBE"] = "must-not-enter-container"
    checked = False

    async def inspect():
        nonlocal checked
        names = (await launcher.command(["ps", "--filter", f"label=io.gaoji.mcp.owner={launcher.owner}", "--format", "{{.Names}}"])).decode().splitlines()
        assert len(names) == 1, names
        info = json.loads(await launcher.command(["inspect", names[0]]))[0]
        host = info["HostConfig"]
        assert host["ReadonlyRootfs"] and not host["Privileged"]
        assert host["NetworkMode"] == "none" and host["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["Memory"] == 512 * 1024 * 1024 and host["MemorySwap"] == host["Memory"]
        assert host["NanoCpus"] == 1_000_000_000 and host["PidsLimit"] == 64
        assert host["LogConfig"]["Type"] == "none" and host["AutoRemove"]
        assert not host["Binds"] and not info["Mounts"], info["Mounts"]
        assert "noexec" in host["Tmpfs"]["/tmp"] and "size=64m" in host["Tmpfs"]["/tmp"]
        assert info["Config"]["User"] == "65534:65534"
        checked = True

    catalog = await wire.discover(config)
    result = await wire.call(config, catalog[0], {}, before_call=inspect)
    assert result["ok"] and checked, result
    quoted = json.loads("\n".join(result["content"].splitlines()[1:-1]))
    report = json.loads(quoted["text"][0])
    assert report["uid"] == 65534 and report["secret"] is None, report
    assert len(report["routes"]) == 1, report
    assert not report["root_writable"] and not report["tmp_executable"], report
    print("Real SDK/OCI call: unprivileged, root read-only, tmp noexec, no host mount/secret/network, bounded resources: OK", flush=True)

    sleeper = StdioProfile(image, ("/usr/local/bin/python", "-c", "import time; time.sleep(90)"))
    foreign = DockerLauncher(str(args.docker), str(args.socket), secrets.token_hex(16))
    now = int(time.time())
    owned = []
    try:
        for owner, expiry in ((launcher, now - 1), (launcher, now + 120), (foreign, now - 1)):
            name = CONTAINER_PREFIX + secrets.token_hex(16)
            owned.append((owner, name))
            command = owner.arguments(sleeper, name, expiry)[len(owner.prefix):]
            command.insert(1, "--detach")
            await owner.command(command, timeout=15)
        await launcher.reap(now=now)
        remaining = (await launcher.command(["ps", "--all", "--format", "{{.Names}}"])).decode().splitlines()
        assert owned[0][1] not in remaining
        assert owned[1][1] in remaining and owned[2][1] in remaining
        print("Real expiry reaper: removes only expired matching owner, preserves live/foreign fixtures: OK", flush=True)
    finally:
        for owner, name in owned:
            await owner.remove(name)
        remaining = (await launcher.command(["ps", "--all", "--format", "{{.Names}}"])).decode().splitlines()
        assert all(name not in remaining for _, name in owned)
        print("All created test containers removed; existing containers/images unchanged", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
