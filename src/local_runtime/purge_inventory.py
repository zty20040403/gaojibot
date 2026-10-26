"""Exact resource inventory for a stopped desktop instance; no global prune."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from .backups import backup_id
from .manager import PATH
from .setup_clients import SetupError
from .storage import Storage


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def related(left, right):
    return left == right or left in right.parents or right in left.parents


def tree_snapshot(directory, *, deadline_seconds=30, max_files=1000000):
    """Count an owned tree without following links, including stopped QQ sockets.

    Symlinks and socket names can be unlinked, but devices/mounts cannot be
    recursively treated as user data. This is metadata, not a content backup.
    """
    first = directory.lstat()
    if not stat.S_ISDIR(first.st_mode) or first.st_uid != os.geteuid() or first.st_mode & 0o077:
        raise SetupError("实例目录归属或权限异常，未列入删除清单")
    mounts = Storage.kernel_mounts()
    canonical = directory.resolve()
    if any(mount == canonical or canonical in mount.parents for mount in mounts):
        raise SetupError("实例目录含系统挂载，未列入删除清单")
    stack, count, size, digest = [directory], 0, 0, hashlib.sha256()
    deadline = time.monotonic() + deadline_seconds
    while stack:
        path = stack.pop()
        info = path.lstat()
        if time.monotonic() > deadline or count >= max_files:
            raise SetupError("数据清单尚未扫描完整，未允许删除")
        if info.st_dev != first.st_dev or path.is_mount():
            raise SetupError("数据清单出现挂载点，未允许删除")
        if not any(check(info.st_mode) for check in (stat.S_ISDIR, stat.S_ISREG, stat.S_ISLNK, stat.S_ISSOCK, stat.S_ISFIFO)):
            raise SetupError("数据目录含设备文件，未允许删除")
        count += 1
        size += info.st_blocks * 512
        item = [str(path.relative_to(directory)), info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink]
        if stat.S_ISLNK(info.st_mode):
            item.append(os.readlink(path))
        digest.update(json.dumps(item, separators=(",", ":")).encode())
        if stat.S_ISDIR(info.st_mode):
            stack.extend(sorted(path.iterdir(), reverse=True))
    return {"bytes": size, "entries": count, "identity": [first.st_dev, first.st_ino], "fingerprint": digest.hexdigest()}


class PurgeInventory:
    """Enumerate all consumers before selecting this instance's fixed resources."""
    def __init__(self, identifier, directory, deployment):
        self.identifier = backup_id(identifier)
        self.directory = directory.absolute()
        self.project = "gaoji-" + identifier
        self.prefix = "gaoji-app-" + identifier
        self.deployment = deployment
        docker, host = deployment["docker"], deployment["docker_host"]
        if (not isinstance(docker, str) or not docker.startswith("/") or re.search(r"[\s\x00]", docker)
                or not isinstance(host, str) or not host.startswith("unix:///") or re.search(r"[\s\x00]", host)):
            raise SetupError("清理仅支持安装记录中的本机容器引擎")

    def command(self, *args):
        try:
            result = subprocess.run([self.deployment["docker"], *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
                env={"PATH": PATH, "LANG": "C.UTF-8", "DOCKER_HOST": self.deployment["docker_host"]})
            if result.returncode or len(result.stdout) > 32 * 1024**2:
                raise SetupError("容器引擎未确认清单或操作结果，未继续删除")
            return result.stdout.decode()
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise SetupError("容器清单暂不可核对，未继续删除") from None

    def objects(self, kind):
        listing = {"container": ("ps", "--all", "--quiet", "--no-trunc"),
            "volume": ("volume", "ls", "--quiet"), "network": ("network", "ls", "--quiet", "--no-trunc")}[kind]
        identifiers = self.command(*listing).split()
        pattern = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}" if kind == "volume" else r"[a-f0-9]{64}"
        if len(identifiers) > 5000 or len(set(identifiers)) != len(identifiers) or any(not re.fullmatch(pattern, item) for item in identifiers):
            raise SetupError("容器资源列表异常，未继续删除")
        rows = []
        for offset in range(0, len(identifiers), 50):
            batch = identifiers[offset:offset + 50]
            raw = self.command("inspect", "--type", kind, "--format", "{{json .}}", *batch)
            try:
                values = [json.loads(line) for line in raw.splitlines()]
                key = "Name" if kind == "volume" else "Id"
                if len(values) != len(batch) or {row[key] for row in values} != set(batch):
                    raise ValueError()
                rows.extend(values)
            except (KeyError, TypeError, ValueError):
                raise SetupError("容器资源检查不完整，未继续删除") from None
        return rows

    def owned_container(self, row):
        labels = row.get("Config", {}).get("Labels") or {}
        name = row.get("Name", "")
        if not isinstance(labels, dict) or not isinstance(name, str):
            raise SetupError("容器归属格式无效")
        compose = labels.get("com.docker.compose.project") == self.project
        sandbox = labels.get("gaoji.app.instance") == self.identifier
        if compose:
            service = labels.get("com.docker.compose.service")
            if (service not in {"bot", "napcat", "database"}
                    or labels.get("com.docker.compose.project.working_dir") != str(self.directory)
                    or labels.get("com.docker.compose.project.config_files") != str(self.directory / "compose.json")
                    or not re.fullmatch("/" + self.project + "-" + service + r"-(?:[1-9][0-9]*|run-[a-f0-9]+)", name)):
                raise SetupError("发现归属不完整的同名 Compose 容器，未删除")
        elif sandbox:
            kind, sid = labels.get("gaoji.app.resource"), labels.get("qqbot.id", "")
            valid = (kind == "helper" and re.fullmatch("/" + self.prefix + r"-helper-[a-f0-9]{16}", name)
                or kind == "sandbox" and re.fullmatch(r"s[a-f0-9]{6}", sid)
                and labels.get("gaoji.app.sandbox") == "true" and name == "/" + self.prefix + "-" + sid)
            if not valid:
                raise SetupError("发现归属不完整的沙盒容器，未删除")
        elif name.startswith(("/" + self.project + "-", "/" + self.prefix + "-")):
            raise SetupError("同名容器缺少本实例归属，未删除")
        return compose or sandbox

    def owned_volume(self, row):
        name, labels = row.get("Name", ""), row.get("Labels") or {}
        if not isinstance(labels, dict) or not isinstance(name, str):
            raise SetupError("工作卷归属格式无效")
        owned = labels.get("gaoji.app.instance") == self.identifier
        if not owned:
            if name.startswith(self.prefix + "-"):
                raise SetupError("同名工作卷缺少本实例归属，未删除")
            return False
        kind, sid = labels.get("gaoji.app.resource"), labels.get("qqbot.id", "")
        valid = (kind == "nix-cache" and name == self.prefix + "-nix"
            or kind == "workspace" and re.fullmatch(r"s[a-f0-9]{6}", sid)
            and re.fullmatch(r"[a-f0-9]{20}", labels.get("qqbot.owner", "")) and name == self.prefix + "-work-" + sid)
        if not valid or row.get("Driver") != "local" or row.get("Options"):
            raise SetupError("工作卷归属异常或使用外部驱动，未删除")
        return True

    def snapshot(self, *, retired_path=None):
        containers, volumes, networks = (self.objects(kind) for kind in ("container", "volume", "network"))
        selected = {"containers": [], "volumes": [], "networks": []}
        owned_ids = set()
        locations = [self.directory.resolve(), *([retired_path.resolve()] if retired_path else [])]
        try:
            volumes_by_name = {row["Name"]: row for row in volumes}
            own_volumes = {row["Name"] for row in volumes if self.owned_volume(row)}
            for row in containers:
                owned = self.owned_container(row)
                mounts = row["Mounts"]
                if not isinstance(mounts, list) or type(row["State"]["Running"]) is not bool:
                    raise ValueError()
                if owned and (row["State"]["Running"] or row["State"].get("Paused") or row["State"].get("Restarting")):
                    raise SetupError("本实例还有运行容器，请先完成卸载服务")
                for mount in mounts:
                    source = mount.get("Source")
                    if source and (not isinstance(source, str) or not source.startswith("/")):
                        raise ValueError()
                    source = Path(source).resolve() if source else None
                    if not owned and ((source and any(related(source, place) for place in locations)) or mount.get("Name") in own_volumes):
                        raise SetupError("其他容器仍引用本实例的数据或工作卷，未开始删除")
                    if owned and mount.get("Type") == "volume" and mount.get("Name") not in own_volumes:
                        raise SetupError("本实例容器挂载未归属的卷，未开始删除")
                    if owned and mount.get("Type") == "bind" and (source is None or not any(source == place or place in source.parents for place in locations)):
                        raise SetupError("本实例容器引用外部目录，未开始删除")
                if owned:
                    owned_ids.add(row["Id"])
                    stable = {key: row[key] for key in ("Id", "Name", "Created", "Mounts")}
                    # Docker may reorder mounts between inspections without changing them.
                    stable["Mounts"] = sorted(mounts, key=lambda item: json.dumps(item, sort_keys=True))
                    stable["labels"] = row["Config"]["Labels"]
                    selected["containers"].append({"id": row["Id"], "name": row["Name"].lstrip("/"), "fingerprint": fingerprint(stable)})
            for name in own_volumes:
                row = volumes_by_name[name]
                stable = {key: row.get(key) for key in ("Name", "CreatedAt", "Driver", "Labels", "Options", "Mountpoint")}
                if not row.get("CreatedAt") or not row.get("Mountpoint", "").startswith("/"):
                    raise ValueError()
                selected["volumes"].append({"id": name, "name": name, "fingerprint": fingerprint(stable)})
            for row in networks:
                labels = row.get("Labels") or {}
                if labels.get("com.docker.compose.project") != self.project:
                    if row.get("Name") == self.project + "_default":
                        raise SetupError("同名容器网络缺少本实例归属，未删除")
                    continue
                if row.get("Name") != self.project + "_default" or labels.get("com.docker.compose.network") != "default" or row.get("Driver") != "bridge":
                    raise SetupError("容器网络不是本实例的默认隔离网络，未删除")
                if not isinstance(row.get("Containers"), dict) or set(row["Containers"]) - owned_ids:
                    raise SetupError("其他容器仍接入本实例的网络，未开始删除")
                stable = {key: row.get(key) for key in ("Id", "Name", "Created", "Driver", "Labels", "Options", "IPAM")}
                selected["networks"].append({"id": row["Id"], "name": row["Name"], "fingerprint": fingerprint(stable)})
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SetupError("资源归属或挂载信息不完整，未开始删除") from None
        for values in selected.values():
            values.sort(key=lambda row: row["id"])
        return selected

    def remaining(self, approved, *, retired_path=None):
        current = self.snapshot(retired_path=retired_path)
        for kind, rows in current.items():
            expected = {row["id"]: row for row in approved[kind]}
            if any(row != expected.get(row["id"]) for row in rows):
                raise SetupError("资源在确认后变化或出现新资源，未扩大删除范围")
        return current

    def remove(self, approved, kind, identifier, *, retired_path=None):
        if kind not in {"containers", "volumes", "networks"} or not any(row["id"] == identifier for row in approved[kind]):
            raise SetupError("资源不在已确认的删除清单中")
        current = self.remaining(approved, retired_path=retired_path)
        if not any(row["id"] == identifier for row in current[kind]):
            return
        if kind != "containers" and current["containers"]:
            raise SetupError("容器尚未全部移除，未删除工作卷或网络")
        arguments = {"containers": ("rm", identifier), "volumes": ("volume", "rm", identifier), "networks": ("network", "rm", identifier)}[kind]
        self.command(*arguments)
        if any(row["id"] == identifier for row in self.remaining(approved, retired_path=retired_path)[kind]):
            raise SetupError("资源删除尚未核实，请继续原任务")
