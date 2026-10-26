"""Fixed synthetic witness for the localhost-only VM migration acceptance."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def main(request):
    assert set(request) == {"vm_id", "job", "run", "role", "action"}
    assert request["role"] in {"source", "target"}
    assert request["action"] in {"seed", "witness"}
    assert Path("/etc/gaoji-local-qa").read_text().strip() == request["vm_id"]
    assert re.fullmatch(r"gaoji-local-qa-[a-f0-9]{32}", request["vm_id"])
    assert all(re.fullmatch(r"[a-f0-9]{32}", request[key]) for key in ("job", "run"))
    root = Path("/var/lib/gaoji-app")
    deployment = json.loads((root / "jobs" / (request["job"] + ".json")).read_text())
    identifier = deployment["instance_id"]
    assert re.fullmatch(r"[a-f0-9]{32}", identifier)
    directory = root / "data/instances" / identifier
    config = json.loads((directory / "instance.json").read_text())
    assert config["id"] == identifier
    assert config["settings"]["models"] == []
    assert config["settings"]["bot_qq"] == ""
    desired = json.loads((directory / "config/desired.json").read_text())
    assert desired["bot_enabled"] is False
    assert deployment["docker_host"].startswith("unix:///")
    assert Path(deployment["docker"]).is_absolute()
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "HOME": "/root",
                   "DOCKER_HOST": deployment["docker_host"]}

    def sql(statement):
        command = [deployment["docker"], "compose", "--file", str(directory / "compose.json"),
                   "exec", "-T", "database", "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "gaoji", "-d", "gaoji"]
        result = subprocess.run(command, input=statement.encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=90, env=environment)
        if result.returncode:
            raise RuntimeError("Synthetic fixture SQL failed: " + result.stderr.decode(errors="replace")[:1500])
        return result.stdout.decode().strip()

    # These literals are generated solely from validated random QA IDs and fixed
    # fixture strings, never user messages or model-provided SQL.
    prefix = "qa-migration:" + request["run"] + ":" + request["role"]
    folder = "qa-migration-" + request["run"] + "-" + request["role"]
    files = {"说明.txt": ("群聊与文件迁移验收\n" + prefix + "\n").encode(),
             "payload.bin": bytes(range(256)) * 1024}
    if request["action"] == "seed":
        for group in (1, 2):
            scope = prefix + ":" + str(group)
            cid = f"(SELECT conversation_id FROM qq_bot.conversations WHERE scope_key='{scope}')"
            text = f"迁移验收第{group}组：这条中文消息属于当前群。"
            digest = hashlib.sha256(text.encode()).hexdigest()
            vector = "[" + ",".join(["1"] + ["0"] * 1023) + "]"
            sql(f"""BEGIN;
                INSERT INTO qq_bot.conversations(scope_key,platform,kind,native_conversation_id,created_at,updated_at)
                VALUES ('{scope}','qa','group','{scope}',1700000000,1700000001)
                ON CONFLICT(scope_key) DO NOTHING;
                INSERT INTO qq_bot.messages(conversation_id,native_message_id,sender_native_user_id,sender_display,
                    direction,body_json,rendered_text,occurred_at)
                VALUES ({cid},'qa-question','qa-user-{group}','验收用户{group}','inbound','{{"qa":true}}','{text}',1700000000)
                ON CONFLICT(conversation_id,native_message_id) DO NOTHING;
                INSERT INTO qq_bot.messages(conversation_id,native_message_id,sender_native_user_id,sender_display,
                    direction,body_json,rendered_text,occurred_at,reply_to_native_message_id,reply_to_canonical_message_id)
                VALUES ({cid},'qa-reply','qa-user-{group}','验收用户{group}','inbound','{{"qa":true}}','你觉得呢？',1700000001,
                    'qa-question',(SELECT canonical_message_id FROM qq_bot.messages WHERE conversation_id={cid} AND native_message_id='qa-question'))
                ON CONFLICT(conversation_id,native_message_id) DO NOTHING;
                INSERT INTO qq_bot.semantic_documents(scope_key,source_type,source_handle,content,content_hash,
                    metadata_json,embedding_model,embedding,created_at,updated_at)
                VALUES ('{scope}','qa','qa-question','{text}','{digest}','{{"qa":true,"group":{group}}}',
                    'qa-synthetic-1024','{vector}',1700000000,1700000001)
                ON CONFLICT(scope_key,source_type,source_handle) DO NOTHING;
                COMMIT;""")
        output = directory / "files" / folder
        output.mkdir(mode=0o700, exist_ok=True)
        assert not output.is_symlink()
        os.chown(output, 10001, 10001)
        for name, data in files.items():
            path = output / name
            assert not path.is_symlink()
            if path.exists():
                assert path.read_bytes() == data, "Existing fixture has changed"
            else:
                with path.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                path.chmod(0o600)
                os.chown(path, 10001, 10001)

    witness = json.loads(sql(f"""SELECT json_build_object(
        'conversations', (SELECT coalesce(json_agg(c ORDER BY c.conversation_id),'[]'::json) FROM qq_bot.conversations c WHERE scope_key LIKE '{prefix}:%'),
        'messages', (SELECT coalesce(json_agg(m ORDER BY m.canonical_message_id),'[]'::json) FROM qq_bot.messages m JOIN qq_bot.conversations c USING(conversation_id) WHERE c.scope_key LIKE '{prefix}:%'),
        'vectors', (SELECT coalesce(json_agg(d ORDER BY d.document_id),'[]'::json) FROM qq_bot.semantic_documents d WHERE scope_key LIKE '{prefix}:%'));
        """))
    witness["files"] = []
    for name in files:
        path = directory / "files" / folder / name
        if path.exists():
            assert path.is_file() and not path.is_symlink()
            data = path.read_bytes()
            witness["files"].append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    fingerprints = {name: hashlib.sha256((directory / "config" / name).read_bytes()).hexdigest()
                    for name in ("control.json", "admin.secret", "postgres.password")}
    print(json.dumps({"witness": witness, "instance_id": identifier, "ports": config["ports"],
        "settings": config["settings"], "revision": config["revision"], "fingerprints": fingerprints,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}, ensure_ascii=False))
