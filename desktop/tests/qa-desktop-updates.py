"""Inside a disposable Desktop volume; QQ and model traffic stay disabled."""
import json
import os
import secrets
import time

from src.local_runtime.desktop import DesktopController, initialize_seed, ROOT
from src.local_runtime.desktop_updates import prepare_update, docker
from src.local_runtime.files import read_json, write_json
from src.local_runtime.qq_handover import require_handover


job, old, new = (os.environ[name] for name in ("QA_JOB", "QA_OLD", "QA_NEW"))
port = int(os.environ["QA_PORT"])
initialize_seed({"job": job, "checksum": old, "token": "t" * 43, "bot_port": 28080})
write_json(ROOT / "desktop-install.json", {"status": "awaiting_setup", "stage": "ready"})
c = DesktopController(job)
require_handover(c.directory, "a" * 32, secrets.token_hex(16))
c.render(False)
c.compose(["up", "-d", "--wait", "--wait-timeout", "90", "database"], timeout=120)
c.compose(["run", "--rm", "--no-deps", "-T", "bot", "migrate"], timeout=180)


def sql(query):
    return c.compose(["exec", "-T", "database", "psql", "-v", "ON_ERROR_STOP=1", "-U", "gaoji", "-d", "gaoji", "-Atc", query], timeout=30).strip()


sql("CREATE TABLE public.desktop_upgrade_probe (message text); INSERT INTO public.desktop_upgrade_probe VALUES ('升级前的上下文')")
original = (c.directory / "config/admin.secret").read_bytes()
(c.directory / "state/qa.txt").write_text("中文文件")
mounts = ["--mount", f"type=volume,source=gaoji-desktop-{job}-data,target={ROOT}",
          "--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock"]
name = f"gaoji-desktop-{job}-controller"
docker("run", "-d", "--name", name, "--label", "gaoji.desktop.job=" + job,
    "--network", "gaoji-desktop-" + job, "--publish", f"127.0.0.1:{port}:8090", *mounts,
    "gaoji-desktop-controller:" + old, "serve")
time.sleep(3)
# Legacy managers predate QQ handover. The internal QA network has no egress;
# stop their unconfigured login component before exercising the upgrade.
c.compose(["stop", "napcat"], timeout=60)
c.render(False)


def run_update(kind, target, rollback=None):
    global c
    operation = secrets.token_hex(16)
    request = {"id": operation, "job": job, "kind": kind, "expected_checksum": c.deployment["checksum"],
        "target_checksum": target, "revision": c.store.read(c.identifier)["revision"]}
    if rollback:
        request.update(rollback_id=rollback, confirm_data_replacement=True)
    prepare_update({"request": request, "runner_checksum": new, "port": port})
    worker = f"gaoji-desktop-{job}-update-{operation}"
    docker("run", "-d", "--name", worker, "--label", "gaoji.desktop.job=" + job,
        "--label", "gaoji.desktop.update=" + operation, "--restart", "unless-stopped",
        "--network", "gaoji-desktop-" + job, *mounts, "gaoji-desktop-controller:" + new, "update", operation)
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        state = read_json(ROOT / "updates" / (operation + ".json"))
        if state.get("finished") or state.get("status") == "failed":
            assert state["status"] == "completed", state
            c = DesktopController(job)
            assert c.deployment["checksum"] == target
            assert c.deployment["controller_checksum"] == new
            docker("stop", worker)
            docker("rm", worker)
            return operation
        time.sleep(1)
    raise AssertionError("Isolated desktop update timed out")


upgrade = run_update("update", new)
assert sql("SELECT message FROM public.desktop_upgrade_probe") == "升级前的上下文"
assert (c.directory / "config/admin.secret").read_bytes() == original
sql("UPDATE public.desktop_upgrade_probe SET message='升级后新数据'")
run_update("rollback", old, upgrade)
assert sql("SELECT message FROM public.desktop_upgrade_probe") == "升级前的上下文"
assert (c.directory / "state/qa.txt").read_text() == "中文文件"
assert (c.directory / "runtime-config/admin.secret").read_bytes() == original
assert not any(row["name"] in {"bot", "napcat"} and row["state"] == "running" for row in c.services()["services"])
print(json.dumps({"passed": True, "durableDockerWorker": True, "realPostgresUpgradeRollback": True,
    "preservedCredentials": True, "retainedPostUpgradeData": bool(list((c.directory / "recovery").glob("*/rollback/database"))),
    "qqAndModelCalls": False, "serversAccessed": False}))
