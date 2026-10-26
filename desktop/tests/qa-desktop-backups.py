"""Run inside a disposable controller against a fresh, labeled Docker volume."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from src.local_runtime.desktop import DesktopController, initialize_seed, ROOT
from src.local_runtime.files import read_json, write_json
from src.local_runtime.qq_handover import require_handover


def main():
    job, backup, restore = (os.environ[name] for name in ("QA_JOB", "QA_BACKUP", "QA_RESTORE"))
    image = os.environ["QA_RUNTIME_IMAGE"]
    assert all(re.fullmatch(r"[a-f0-9]{32}", value) for value in (job, backup, restore))
    assert re.fullmatch(r"gaoji-app-runtime:[a-f0-9]{64}", image)
    initialize_seed({"job": job, "checksum": image.split(":")[1], "token": "t" * 43, "bot_port": 28080})
    write_json(ROOT / "desktop-install.json", {"status": "awaiting_setup", "stage": "ready"})
    c = DesktopController(job)
    # Never start QQ or a chat process in acceptance; only database CLI commands.
    require_handover(c.directory, "a" * 32, restore)
    c.render(False)
    c.compose(["up", "-d", "--wait", "--wait-timeout", "90", "database"], timeout=120)
    c.compose(["run", "--rm", "--no-deps", "-T", "bot", "migrate"], timeout=180)
    def sql(text):
        result = subprocess.run([c.deployment["docker"], "compose", "--file", str(c.directory / "compose.json"),
            "exec", "-T", "database", "psql", "-v", "ON_ERROR_STOP=1", "-U", "gaoji", "-d", "gaoji", "-Atc", text],
            capture_output=True, text=True, timeout=30, env=c.process_environment())
        assert result.returncode == 0, result.stderr
        return result.stdout
    vector_schema = sql("SELECT quote_ident(n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'").strip()
    assert vector_schema
    sql(f"CREATE TABLE public.desktop_backup_probe (message text, embedding {vector_schema}.vector(3)); "
        "INSERT INTO public.desktop_backup_probe VALUES ('原始上下文', '[1,2,3]')")
    files = {"state/context.txt": "群聊上下文与引用关系".encode(), "files/report.txt": "中文文件交付".encode()}
    for name, content in files.items():
        (c.directory / name).write_bytes(content)
    def operation(kind, identifier, **extra):
        c.operation = {"id": identifier, "kind": kind, "revision": c.store.read(c.identifier)["revision"], "status": "queued", **extra}
        c.persist()
        c.maintenance().run()
        assert c.operation["status"] == "completed", c.public_operation()
    operation("backup", backup)
    sql("UPDATE public.desktop_backup_probe SET message='changed', embedding='[9,9,9]'")
    for name in files:
        (c.directory / name).write_bytes(b"changed after snapshot")
    operation("restore", restore, backup_id=backup)
    result = json.loads(sql("SELECT json_build_object('message', message, 'vector', embedding::text) FROM public.desktop_backup_probe"))
    assert result == {"message": "原始上下文", "vector": "[1,2,3]"}, result
    for name, content in files.items():
        assert (c.directory / name).read_bytes() == content
    assert not read_json(c.desired_path)["bot_enabled"]
    assert c.napcat.base == "http://napcat:6099"
    states = c.services()["services"]
    assert not any(row["name"] in {"bot", "napcat"} and row["state"] == "running" for row in states), states
    for source, target in (("config/postgres.password", "database-config/postgres-password"),
                           ("config/admin.secret", "runtime-config/admin.secret")):
        assert (c.directory / source).read_bytes() == (c.directory / target).read_bytes()
    print(json.dumps({"passed": True, "runtimeImage": image, "postgresBackupRestore": True,
        "vectorRestored": True, "chineseTextRestored": True,
        "fileHashes": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "botAndQQStayedStopped": True, "realQQLogin": False, "userServersAccessed": False}))


if __name__ == "__main__":
    main()
