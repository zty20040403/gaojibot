"""Built-in static-site adapter. No uploaded content is executed on the host."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import time

from src.local_runtime.files import read_json, write_json
from .content import MAX_FILE, extract_website
from .retention import prune_uploads, prune_versions
from . import backups

OPERATIONS = {"install", "update", "rollback", "start", "stop", "uninstall", "purge", "backup", "restore", "delete-backup"}
LABEL = "com.gaoji.application"
INSTANCE = "com.gaoji.instance"


def request_value(value: dict) -> dict:
    fields = {"schema", "app_id", "instance_id", "name", "operation", "expected_revision", "content_sha256"}
    if isinstance(value, dict) and value.get("operation") in {"restore", "delete-backup"}:
        fields.add("backup_id")
        backups.identifier(value.get("backup_id"))
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != 1 or value.get("app_id") != "static-site":
        raise ValueError("不支持的软件部署请求")
    if not isinstance(value["instance_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["instance_id"]):
        raise ValueError("无效实例编号")
    if not isinstance(value["name"], str) or not value["name"].strip() or len(value["name"]) > 80 or re.search(r"[\x00-\x1f\x7f]", value["name"]):
        raise ValueError("无效实例名称")
    if value["operation"] not in OPERATIONS or type(value["expected_revision"]) is not int or value["expected_revision"] < 0:
        raise ValueError("无效部署操作或版本")
    content = value["content_sha256"]
    if not isinstance(content, str) or (content and not re.fullmatch(r"[a-f0-9]{64}", content)):
        raise ValueError("无效网站内容校验码")
    if bool(content) != (value["operation"] in {"install", "update"}):
        raise ValueError("部署操作与网页内容不匹配")
    return value


def nginx_config() -> str:
    return """worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 256; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  access_log /dev/stdout;
  client_body_temp_path /tmp/client;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  server_tokens off;
  server {
    listen 8080;
    root /site;
    index index.html;
    add_header X-Content-Type-Options nosniff always;
    location ~ /\\. { deny all; }
    location / { try_files $uri $uri/ =404; }
  }
}
"""


class StaticSite:
    def __init__(self, host, request: dict, *, root: Path):
        self.host = host
        self.request = request_value(request)
        self.root = root
        self.identifier = request["instance_id"]
        self.directory = root / "applications" / self.identifier
        self.network = "gaoji-site-" + self.identifier
        self.record = self.directory / "instance.json"
        self.public = root / "status/applications" / (self.identifier + ".json")
        self.state = {}
        self.image = ""

    def persist(self, **values) -> None:
        self.state.update(values, updated_at=int(time.time()))
        write_json(self.record, self.state)
        self.publish()

    def publish(self) -> None:
        self.public.parent.mkdir(mode=0o755, exist_ok=True)
        self.public.parent.chmod(0o755)
        public = {key: self.state[key] for key in ("schema", "id", "app_id", "name", "revision", "status", "current", "previous", "operation", "updated_at", "cleanup", "backups") if key in self.state}
        public["data_directory"] = str(self.directory)
        public["data_bytes"] = sum(path.stat().st_size for path in self.directory.rglob("*") if path.is_file() and not path.is_symlink())
        write_json(self.public, public, mode=0o644)

    def prepare(self) -> bool:
        for path in (self.root / "applications", self.directory):
            path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError("软件数据目录所有者无效")
        if self.record.exists():
            self.state = read_json(self.record)
            if self.state.get("id") != self.identifier or self.state.get("app_id") != "static-site":
                raise ValueError("该实例不属于静态网站")
        else:
            if any(self.directory.iterdir()):
                raise ValueError("目标目录已有未知内容，不会接管或覆盖")
            if self.request["operation"] != "install" or self.request["expected_revision"] != 0:
                raise ValueError("尚未安装此软件")
            self.state = {"schema": 1, "id": self.identifier, "app_id": "static-site", "name": self.request["name"],
                          "revision": 0, "status": "not_installed", "current": None, "previous": None}
        operation = self.state.get("operation") or {}
        if operation.get("job") == self.host.job:
            if operation["request"] != self.request:
                raise ValueError("原任务参数发生变化，不能重放")
            return operation["status"] == "completed"
        if operation.get("status") == "running" or operation.get("status") == "failed" and not operation.get("safe_to_replace"):
            raise ValueError("请先继续原任务，不能启动冲突操作")
        if self.state["revision"] != self.request["expected_revision"]:
            raise ValueError("实例已发生变化，请刷新后操作")
        if self.state["status"] == "removed":
            raise ValueError("原实例已被彻底删除，请创建新的实例")
        if self.request["operation"] == "install" and self.state.get("current"):
            raise ValueError("已有网站，不能重复安装")
        if self.request["operation"] == "purge" and self.state["status"] != "retained":
            raise ValueError("请先卸载服务并保留数据，再单独确认彻底删除")
        if self.request["operation"] in {"restore", "delete-backup"} and not any(item["id"] == self.request["backup_id"] for item in self.state.get("backups", [])):
            raise ValueError("此实例没有该备份")
        self.persist(revision=self.state["revision"] + 1, operation={"job": self.host.job,
                     "request": self.request, "status": "running"})
        return False

    def inspect(self, kind: str, name: str) -> dict | None:
        if kind not in {"network", "container"} or not re.fullmatch("gaoji-site-" + self.identifier + r"(?:-[a-f0-9]{32})?", name):
            raise ValueError("资源名称不属于本网站")
        # Listing must succeed before absence counts as evidence. An unavailable
        # Docker daemon is not proof that a container was removed.
        command = [self.host.docker, kind, "ls"] + (["--all"] if kind == "container" else [])
        ids = self.host.run(command + ["--filter", "name=" + name, "--quiet"], timeout=30, capture=True).split()
        if not ids:
            return None
        if any(not re.fullmatch(r"[a-f0-9]{12,64}", value) for value in ids):
            raise ValueError("资源编号无效")
        values = json.loads(self.host.run([self.host.docker, kind, "inspect", *ids], timeout=30, capture=True))
        values = [value for value in values if value.get("Name", "").lstrip("/") == name]
        if not values:
            return None
        if len(values) != 1:
            raise ValueError("容器状态不可确认")
        value = values[0]
        labels = value.get("Config", {}).get("Labels", {}) if kind == "container" else value.get("Labels", {})
        if labels.get(LABEL) != "static-site" or labels.get(INSTANCE) != self.identifier:
            raise ValueError("检测到不属于本实例的资源，已停止操作")
        return value

    def remove(self, name: str) -> None:
        if self.inspect("container", name):
            self.host.run([self.host.docker, "container", "rm", "--force", name], timeout=45)
            if self.inspect("container", name):
                raise ValueError("网站容器仍存在，删除结果尚未确认")

    def image_and_network(self) -> None:
        self.host.save(stage="runtime", message="正在准备固定版本的网站组件")
        version = self.host.run([self.host.docker, "info", "--format", "{{.ServerVersion}}"], timeout=30, capture=True).strip()
        major = re.match(r"^(\d+)\.", version)
        if not major or int(major.group(1)) < 28:
            raise ValueError("网站需要 Docker 28 或更新版本以保证本机端口隔离；请先维护现有 Docker，不会自动升级共享服务")
        catalog = read_json(self.host.release / "applications.json")
        definition = next(item for item in catalog["applications"] if item["id"] == "static-site")
        self.image = definition["images"]["web"]
        pinned = self.state.get("previous" if self.request["operation"] == "rollback" else "current") or {}
        if self.request["operation"] in {"rollback", "start"} and pinned.get("image"):
            self.image = pinned["image"]
        if not re.fullmatch(r"docker.io/nginxinc/nginx-unprivileged:[a-z0-9.-]+@sha256:[a-f0-9]{64}", self.image):
            raise ValueError("网站镜像未锁定版本")
        output = self.host.run([self.host.docker, "image", "inspect", self.image], timeout=30, capture=True, check=False)
        found = json.loads(output or "[]")
        if not isinstance(found, list):
            raise ValueError("网站镜像状态无法确认")
        if not found:
            self.host.run([self.host.docker, "pull", self.image], timeout=900)
        network = self.inspect("network", self.network)
        if network and network.get("Internal"):
            if network.get("Containers"):
                raise ValueError("原网站网络仍被容器使用，不能自动替换")
            self.host.run([self.host.docker, "network", "rm", self.network], timeout=30)
            network = None
        if not network:
            # Docker does not publish ports on internal-only networks. Keep a
            # separate bridge, disable outbound NAT and bind ingress to loopback.
            self.host.run([self.host.docker, "network", "create",
                           "--opt", "com.docker.network.bridge.enable_ip_masquerade=false",
                           "--opt", "com.docker.network.bridge.enable_icc=false",
                           "--opt", "com.docker.network.bridge.host_binding_ipv4=127.0.0.1",
                           "--label", LABEL + "=static-site",
                           "--label", INSTANCE + "=" + self.identifier, self.network], timeout=30)
        self.host.done("runtime")

    def candidate(self, checksum: str) -> dict:
        release = self.directory / "releases" / checksum
        content = read_json(release / "content.json")
        entry = next(item for item in content["files"] if item["path"] == "index.html")
        config = release / "nginx.conf"
        if config.is_symlink():
            raise ValueError("网站配置不能是链接")
        config.write_text(nginx_config(), encoding="utf-8")
        config.chmod(0o644)
        name = "gaoji-site-" + self.identifier + "-" + self.host.job
        existing = self.inspect("container", name)
        if not existing:
            # Pin the selected port in Docker's restart configuration. An empty
            # HostPort is reassigned after a host reboot. Docker remains the
            # final arbiter if another process claims this port before run.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                selected_port = listener.getsockname()[1]
            self.host.run([self.host.docker, "run", "--detach", "--name", name, "--pull=never",
                "--label", LABEL + "=static-site", "--label", INSTANCE + "=" + self.identifier,
                "--label", "com.gaoji.content=" + checksum, "--user", "101:101", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "64",
                "--memory", "128m", "--cpus", "0.5", "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
                "--log-driver", "json-file", "--log-opt", "max-size=5m", "--log-opt", "max-file=2",
                "--network", self.network, "--publish", f"127.0.0.1:{selected_port}:8080", "--restart", "unless-stopped",
                "--mount", f"type=bind,source={release / 'html'},target=/site,readonly",
                "--mount", f"type=bind,source={config},target=/etc/nginx/nginx.conf,readonly",
                "--entrypoint", "nginx", self.image, "-g", "daemon off;"], timeout=60)
        else:
            if existing["Config"]["Labels"].get("com.gaoji.content") != checksum:
                raise ValueError("恢复任务发现内容版本冲突")
            self.host.run([self.host.docker, "start", name], timeout=30)
        value = self.inspect("container", name)
        bindings = (value or {}).get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp")
        if not isinstance(bindings, list) or len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1":
            raise ValueError("网站未获得仅限本机的访问端口，原网站没有被替换")
        port = int(bindings[0]["HostPort"])
        self.host.save(stage="verify", message="正在实际访问新网站并核对页面内容")
        for _ in range(30):
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                client.request("GET", "/index.html", headers={"Accept-Encoding": "identity"})
                response = client.getresponse()
                data = response.read(MAX_FILE + 1)
                if response.status == 200 and hashlib.sha256(data).hexdigest() == entry["sha256"]:
                    return {"checksum": checksum, "container": name, "port": port, "bytes": content["bytes"],
                            "index_sha256": entry["sha256"], "verified_at": int(time.time()), "image": self.image}
            except (OSError, http.client.HTTPException):
                pass
            finally:
                client.close()
            time.sleep(1)
        raise ValueError("新网站实际访问验证失败，未替换原网站")

    def deploy(self) -> None:
        kind = self.request["operation"]
        checksum = self.request["content_sha256"]
        if kind == "rollback":
            checksum = (self.state.get("previous") or {}).get("checksum")
        if kind == "start":
            checksum = (self.state.get("current") or {}).get("checksum")
        if kind == "restore":
            archive, info = backups.verify(self.directory, self.identifier, self.request["backup_id"])
            checksum = info["sha256"]
            extract_website(archive, self.directory / "releases" / checksum, checksum)
        if not checksum:
            raise ValueError("没有可用的网站版本")
        if kind in {"install", "update"}:
            extract_website(self.host.archive.parent / "website.tar.gz", self.directory / "releases" / checksum, checksum)
        self.image_and_network()
        self.host.save(stage="service", message="正在启动候选网站，原网站暂不变更")
        # The commit record is durable before old-container cleanup. On resume,
        # finish cleanup instead of creating or promoting another candidate.
        candidate_name = "gaoji-site-" + self.identifier + "-" + self.host.job
        if (self.state.get("current") or {}).get("container") != candidate_name:
            old = self.state.get("current")
            self.persist(pending_container=candidate_name)
            try:
                candidate = self.candidate(checksum)
            except Exception:
                self.remove(candidate_name)
                self.persist(pending_container=None)
                raise
            previous = old if old and old["checksum"] != checksum else self.state.get("previous")
            self.persist(current=candidate, previous=previous, obsolete=old, pending_container=None, status="running")
        obsolete = self.state.get("obsolete")
        if obsolete and obsolete["container"] != candidate_name:
            self.remove(obsolete["container"])
        self.persist(obsolete=None)
        self.host.done("service")
        self.host.done("verify")

    def maintenance(self) -> None:
        kind = self.request["operation"]
        self.host.save(stage="service", message="正在维护此网站实例")
        current = self.state.get("current")
        if current and self.inspect("container", current["container"]):
            if kind == "stop":
                self.host.run([self.host.docker, "stop", current["container"]], timeout=45)
                after = self.inspect("container", current["container"])
                if after and after.get("State", {}).get("Running"):
                    raise ValueError("容器仍在运行，停止操作尚未通过验证")
            else:
                self.remove(current["container"])
        if kind in {"uninstall", "purge"} and self.inspect("network", self.network):
            self.host.run([self.host.docker, "network", "rm", self.network], timeout=30)
            if self.inspect("network", self.network):
                raise ValueError("网站专属网络仍存在，卸载尚未确认")
        if kind == "purge":
            releases = self.directory / "releases"
            backups.purge_candidates(self.directory)
            if releases.is_symlink():
                raise ValueError("网站数据目录不能是链接")
            mounts = self.mounted_sources()
            if any(source == str(target) or source.startswith(str(target) + "/")
                   for source in mounts for target in (releases, self.directory / "backups")):
                raise ValueError("仍有容器使用此网站文件或备份，未删除正在使用的数据")
            if releases.exists():
                shutil.rmtree(releases)
            prune_uploads(self.host, self.root, self.identifier, purge=True)
            backups.purge(self.directory)
            self.persist(current=None, previous=None, backups=[], status="removed")
        else:
            self.persist(status="stopped" if kind == "stop" else "retained")
        self.host.done("service")
        self.host.done("verify")

    def backup_operation(self) -> None:
        self.host.save(stage="service", message="正在处理此网站备份，当前网站不变更")
        saved = list(self.state.get("backups", []))
        if self.request["operation"] == "backup":
            checksum = (self.state.get("current") or {}).get("checksum", "")
            info = backups.create(self.directory, self.identifier, self.host.job, checksum)
            saved = [info, *[item for item in saved if item["id"] != info["id"]]]
        else:
            backup = self.request["backup_id"]
            backups.delete(self.directory, backup)
            saved = [item for item in saved if item["id"] != backup]
        self.persist(backups=saved)
        self.host.done("service")
        self.host.done("verify")

    def mounted_sources(self) -> list[str]:
        identifiers = self.host.run([self.host.docker, "container", "ls", "--all", "--quiet"], capture=True, timeout=30).split()
        if not identifiers:
            return []
        if any(not re.fullmatch(r"[a-f0-9]{12,64}", item) for item in identifiers):
            raise ValueError("无法确认容器文件挂载，未执行清理")
        values = json.loads(self.host.run([self.host.docker, "container", "inspect", *identifiers], capture=True, timeout=30))
        return [mount["Source"] for value in values for mount in value.get("Mounts", []) if mount.get("Type") == "bind"]

    def cleanup(self) -> None:
        keep = {value["checksum"] for value in (self.state.get("current"), self.state.get("previous")) if value}
        released = prune_versions(self.directory, keep, self.mounted_sources())
        released += prune_uploads(self.host, self.root, self.identifier)
        self.persist(cleanup={"released_bytes": released, "checked_at": int(time.time()), "message": ""})

    def execute(self) -> None:
        try:
            complete = self.prepare()
        except ValueError:
            if (self.state.get("operation") or {}).get("job") != self.host.job:
                self.host.save(instance_id=self.identifier, app_id="static-site", rejected=True)
            raise
        if complete:
            self.host.save(status="completed", message="该任务已完成，没有重复执行")
            return
        try:
            self.host.save(instance_id=self.identifier, app_id="static-site", status="running", stage="prepare", message="正在核对网站部署范围")
            self.host.done("prepare")
            backup_only = self.request["operation"] in {"backup", "delete-backup"}
            if backup_only:
                self.backup_operation()
            else:
                self.host.save(stage="engine", message="正在检查容器运行环境")
                self.host.setup_engine()
                self.host.save(docker=self.host.docker, docker_host=self.host.environment["DOCKER_HOST"])
                self.host.done("engine")
                if self.request["operation"] in {"install", "update", "rollback", "start", "restore"}:
                    self.deploy()
                else:
                    self.maintenance()
            self.persist(operation={**self.state["operation"], "status": "completed"})
            self.host.save(status="completed", message="网站操作已完成并验证")
        except Exception:
            self.persist(operation={**self.state["operation"], "status": "failed",
                         "safe_to_replace": not self.state.get("pending_container") and not self.state.get("obsolete")})
            raise
        # Cleanup cannot turn an already committed task back into a failed one:
        # its old deployment archive may just have been removed successfully.
        if backup_only:
            return
        try:
            self.cleanup()
        except Exception as error:
            try:
                self.persist(cleanup={"checked_at": int(time.time()), "message": str(error) if isinstance(error, ValueError) else "运行已完成，部分旧副本待后续清理"})
            except Exception:
                pass
