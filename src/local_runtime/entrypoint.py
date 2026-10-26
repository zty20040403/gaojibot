"""Container-only entrypoint; never loads a developer or working-directory .env."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def load_environment(filename: Path) -> str:
    from .files import read_json
    payload = read_json(filename)
    environment = payload.get("environment")
    if payload.get("schema") != 1 or not isinstance(environment, dict):
        raise ValueError("Invalid runtime configuration")
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\0" in value:
            raise ValueError("Invalid runtime environment")
        if not (key.startswith(("AI_", "ONEBOT_", "GAOJI_MODEL_KEY_")) or key in {"ENVIRONMENT", "HOST", "PORT", "DRIVER", "SUPERUSERS"}):
            raise ValueError("Unsupported runtime environment field")
    from .components import component_keys
    cluster_token = component_keys({"cluster": environment.pop("AI_DESKTOP_CLUSTER_TOKEN", "")})["cluster"]
    # Validate the whole payload before replacing any inherited configuration.
    for key in tuple(os.environ):
        if key.startswith(("AI_", "ONEBOT_", "DEEPSEEK_", "OPENAI_", "ANTHROPIC_", "GAOJI_MODEL_KEY_")):
            del os.environ[key]
    os.environ.update(environment)
    return cluster_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "migrate", "bootstrap-admin", "ensure-admin", "check-admin", "check-runtime", "check-database", "check-video", "check-sandbox", "probe-model", "probe-embedding", "probe-cluster", "probe-matrix", "probe-imessage"))
    args = parser.parse_args(argv)
    try:
        if args.command in {"probe-model", "probe-embedding", "probe-cluster", "probe-matrix", "probe-imessage"}:
            from .setup_clients import SetupError, probe_embedding, probe_model, probe_cluster, probe_matrix, probe_imessage
            from .components import ComponentError
            from .validation import InstanceError
            try:
                payload = json.loads(sys.stdin.read(32769))
                fields = {"model", "key", "listing"} if args.command == "probe-model" else {"options", "key"}
                if not isinstance(payload, dict) or set(payload) != fields:
                    raise ValueError("Invalid probe input")
                if args.command == "probe-embedding":
                    data = probe_embedding(payload["options"], payload["key"])
                elif args.command == "probe-cluster":
                    data = probe_cluster(payload["options"], payload["key"])
                elif args.command == "probe-matrix":
                    data = probe_matrix(payload["options"], payload["key"])
                elif args.command == "probe-imessage":
                    data = probe_imessage(payload["options"], payload["key"])
                else:
                    if type(payload["listing"]) is not bool:
                        raise ValueError("Invalid probe input")
                    data = probe_model(payload["model"], payload["key"], listing=payload["listing"])
                print(json.dumps({"data": data}))
            except (SetupError, ComponentError) as error:
                print(json.dumps({"error": str(error)}, ensure_ascii=False))
            except (ValueError, InstanceError):
                print(json.dumps({"error": "接口配置不完整或格式不正确"}, ensure_ascii=False))
            return 0
        if args.command == "check-runtime":
            from .runtime_check import check_runtime
            print(json.dumps(check_runtime()))
            return 0
        if args.command == "check-video":
            from .runtime_check import check_video
            print(json.dumps(check_video()))
            return 0
        cluster_token = load_environment(Path("/run/gaoji/runtime.json"))
        if args.command == "check-sandbox":
            import asyncio
            from src.sandbox_runtime.client import SandboxClient
            client = SandboxClient(os.environ.get("AI_SANDBOX_RUNNER_SOCKET", ""),
                os.environ.get("AI_SANDBOX_RUNNER_TOKEN", ""))
            print(json.dumps(asyncio.run(client.health())))
            return 0
        if args.command == "check-database":
            from src.bot_storage.database import PostgresDatabase
            from src.bot_storage.schema import HEAD_REVISION
            database = PostgresDatabase(os.environ["AI_POSTGRES_DSN"], schema=os.environ["AI_POSTGRES_SCHEMA"])
            try:
                database.require_revision(HEAD_REVISION)
            finally:
                database.close()
            print(json.dumps({"database_ready": True, "revision": HEAD_REVISION}))
            return 0
        if args.command == "migrate":
            from src.bot_storage.cli import main as migrate
            return migrate(["upgrade"])
        if args.command in {"bootstrap-admin", "ensure-admin", "check-admin"}:
            from src.bot_security.store import SecurityStore
            from src.bot_storage.database import PostgresDatabase
            from src.bot_storage.schema import HEAD_REVISION
            payload = json.loads(sys.stdin.read(16385))
            fields = {"qq_id"} if args.command == "check-admin" else {"username", "password", "qq_id"}
            if not isinstance(payload, dict) or set(payload) != fields:
                raise ValueError("Invalid administrator input")
            database = PostgresDatabase(os.environ["AI_POSTGRES_DSN"], schema=os.environ["AI_POSTGRES_SCHEMA"])
            try:
                database.require_revision(HEAD_REVISION)
                store = SecurityStore(database, Path(os.environ["AI_ADMIN_SECRET_FILE"]).read_bytes())
                created = False
                if args.command == "check-admin":
                    if not store.account_for_qq(payload["qq_id"]):
                        raise ValueError("Initialize administrator first")
                elif args.command == "ensure-admin" and store.accounts():
                    from src.bot_security.passwords import verify_password
                    with store.transaction() as cursor:
                        account = cursor.execute("SELECT * FROM admin_accounts WHERE username=? AND qq_id=? AND role='admin' AND enabled=1",
                            (payload["username"].strip().lower(), payload["qq_id"])).fetchone()
                    if not account or not verify_password(account["password_hash"], payload["password"]):
                        raise ValueError("Existing administrator credentials do not match")
                else:
                    store.bootstrap(payload["username"], payload["password"], payload["qq_id"])
                    created = True
            finally:
                database.close()
            print(json.dumps({"administrator_created": created, "administrator_ready": True}))
            return 0
        from .cluster_credentials import cluster_credentials
        with cluster_credentials(cluster_token):
            import nonebot
            from nonebot.adapters.onebot.v11 import Adapter
            nonebot.init(_env_file=(os.devnull,))
            nonebot.get_driver().register_adapter(Adapter)
            nonebot.load_plugin("src.plugins.ai_chat")
            from .runtime_health import install_health_writer
            install_health_writer()
            nonebot.run(timeout_graceful_shutdown=10)
        return 0
    except Exception:
        # Database and model exceptions may contain private connection strings.
        print("Runtime operation failed; check private service diagnostics.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
