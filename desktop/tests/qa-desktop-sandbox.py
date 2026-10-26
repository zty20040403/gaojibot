"""Fresh named volume only. No real QQ account or remote server access."""
import asyncio
import io
import json
import os

from pathlib import Path
from src.local_runtime.desktop import DesktopController, initialize_seed, ROOT
from src.local_runtime.files import read_json, write_json
from src.local_runtime.desktop_sandbox import sandbox_volume_sizes
from src.sandbox_runtime.client import SandboxClient


job, digest = os.environ["QA_JOB"], os.environ["QA_NEW"]
initialize_seed({"job": job, "checksum": digest, "token": "t" * 43, "bot_port": 28080})
write_json(ROOT / "desktop-install.json", {"status": "awaiting_setup", "stage": "ready"})
c = DesktopController(job)
builder = c.sandbox_build()
ready = builder.ensure(lambda message: print(message, flush=True))
c.deployment.update(sandbox_image=ready["image"], sandbox_image_id=ready["image_id"])
write_json(ROOT / "jobs" / (job + ".json"), c.deployment)
c = DesktopController(job)
c.sandbox.start()
c.capacity.scan(wait=True)
config = read_json(c.sandbox.config_path)
client = SandboxClient(str(c.sandbox.socket_path), config["token"])


async def verify():
    owner = "group:local-qa:user:local-qa"
    sandbox = (await client.create(owner))["sandbox_id"]
    basic = await client.exec(owner, sandbox, "node -e 'console.log(6*7)'", 60)
    assert basic.returncode == 0 and basic.stdout.strip() == "42", basic
    extra = await client.exec(owner, sandbox, "hello", 180, ["hello"])
    assert extra.returncode == 0 and "Hello" in extra.stdout, extra
    await client.write_file(owner, sandbox, "report.md", "# 中文验收报告\n\n本机沙盒正常生成并导出文件。\n".encode())
    rendered = await client.exec(owner, sandbox, "gaoji-pdf report.md report.pdf", 120)
    assert rendered.returncode == 0, rendered
    pdf, archived = await client.export_artifact(owner, sandbox, "report.pdf")
    assert pdf.startswith(b"%PDF-") and not archived
    # Validate extracted Chinese text and embedded font in the actual workspace.
    check = await client.exec(owner, sandbox, "python -c \"import fitz; d=fitz.open('report.pdf'); assert '中文验收报告' in ''.join(p.get_text() for p in d); assert any(d.extract_font(f[0])[3] for p in d for f in p.get_fonts())\"", 60)
    assert check.returncode == 0, check
    return owner, sandbox, len(pdf)


owner, sandbox, size = asyncio.run(verify())
from src.local_runtime.desktop_updates import docker
names = docker("volume", "ls", "--filter", "label=gaoji.app.instance=" + c.identifier, "--format", "{{.Name}}").splitlines()
usage = sandbox_volume_sizes(c.identifier, names)
assert sum(usage) >= size, usage
asyncio.run(client.destroy(owner, sandbox))
assert asyncio.run(client.list(owner)) == []
c.sandbox.pause()
assert not c.sandbox.container()["State"]["Running"]
print(json.dumps({"passed": True, "namedVolumeBuild": True, "authenticatedRunner": True,
    "node": True, "optionalNixTool": True, "chinesePDF": True, "exportedBytes": size,
    "workspaceUsageBytes": sum(usage), "sandboxDestroyed": True,
    "realQQDelivery": False, "serversAccessed": False}))
