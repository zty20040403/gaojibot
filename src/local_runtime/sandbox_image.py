"""Pinned Nix image builds inside an isolated, resumable Docker container."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import time
import uuid

from .files import file_lock, read_json, write_json
from .validation import InstanceError


FILES = ("default.nix", "image.nix", "build.sh", "builder.lock.json")
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_IMAGE_BYTES = 8 * 1024**3
MIN_FREE_BYTES = 12 * 1024**3
MIN_ENGINE_MEMORY = 6 * 1024**3


class SandboxImageError(InstanceError):
    pass


def regular(path: Path, maximum: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise SandboxImageError("沙盒构建文件不完整或不是普通文件")
    return path.read_bytes()


def recipe(directory: Path) -> tuple[str, dict]:
    hashes = {name: hashlib.sha256(regular(directory / name, 128 * 1024)).hexdigest() for name in FILES}
    lock = json.loads(regular(directory / "builder.lock.json", 4096))
    if (not isinstance(lock, dict) or set(lock) != {"schema", "platform", "image"} or lock["schema"] != 1
            or lock["platform"] != "linux/amd64" or not re.fullmatch(
                r"docker\.io/nixos/nix:[0-9.]+@sha256:[a-f0-9]{64}", lock["image"])):
        raise SandboxImageError("沙盒构建器必须使用固定的官方 Linux amd64 镜像")
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest, lock


def archive_image(path: Path, image: str) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_ARCHIVE_BYTES:
        raise SandboxImageError("沙盒镜像归档无效或超过 8 GiB 上限")
    try:
        with tarfile.open(path, "r|gz") as archive:
            members = []
            files, metadata = set(), {}
            total = metadata_bytes = 0
            for member in archive:
                if len(members) >= 2048:
                    raise SandboxImageError("沙盒镜像归档条目过多")
                total += member.size
                if member.size < 0 or total > MAX_IMAGE_BYTES + 32 * 1024**2:
                    raise SandboxImageError("沙盒镜像解压大小超过上限")
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                    raise SandboxImageError("沙盒镜像归档路径异常")
                members.append(member.name)
                if member.isfile():
                    files.add(member.name)
                    if member.name == "manifest.json" or re.fullmatch(r"[a-f0-9]{64}\.json", member.name):
                        maximum = 65536 if member.name == "manifest.json" else 4 * 1024**2
                        metadata_bytes += member.size
                        if member.size > maximum or metadata_bytes > 32 * 1024**2:
                            raise SandboxImageError("沙盒镜像元数据过大")
                        with archive.extractfile(member) as source:
                            metadata[member.name] = source.read(maximum + 1)
            if len(set(members)) != len(members):
                raise SandboxImageError("沙盒镜像归档存在重复条目")
            def read(name, maximum):
                content = metadata[name]
                if len(content) > maximum:
                    raise SandboxImageError("沙盒镜像元数据无效")
                return content
            manifest = json.loads(read("manifest.json", 65536))
            if (not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict)
                    or manifest[0].get("RepoTags") != [image]):
                raise SandboxImageError("沙盒镜像标签与本次构建不一致")
            name = manifest[0]["Config"]
            if not re.fullmatch(r"[a-f0-9]{64}\.json", name):
                raise SandboxImageError("沙盒镜像配置摘要无效")
            content = read(name, 4 * 1024**2)
            if hashlib.sha256(content).hexdigest() != name[:-5]:
                raise SandboxImageError("沙盒镜像配置摘要不一致")
            config = json.loads(content)
            if (not isinstance(config, dict) or not isinstance(config.get("config"), dict)
                    or not isinstance(config["config"].get("Labels"), dict)
                    or config.get("os") != "linux" or config.get("architecture") != "amd64"
                    or config.get("config", {}).get("User") != "1000:1000"
                    or config.get("config", {}).get("Labels", {}).get("io.gaoji.app.recipe") != image.split(":", 1)[1]):
                raise SandboxImageError("沙盒镜像平台、用户或构建标记不正确")
            rootfs = config.get("rootfs", {})
            layers = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
            if (not isinstance(rootfs, dict) or rootfs.get("type") != "layers" or not isinstance(layers, list)
                    or not layers or len(layers) != len(manifest[0].get("Layers", []))
                    or any(not isinstance(item, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", item) for item in layers)):
                raise SandboxImageError("沙盒镜像文件层记录无效")
            if any(not isinstance(item, str) or item not in files
                   for item in manifest[0]["Layers"]):
                raise SandboxImageError("沙盒镜像缺少文件层")
            return {"config_id": "sha256:" + name[:-5], "config": config["config"], "layers": layers}
    except (OSError, tarfile.TarError, KeyError, ValueError, TypeError) as error:
        raise SandboxImageError("沙盒镜像归档验证失败，未加载镜像") from error


class SandboxImage:
    def __init__(self, source: Path, cache: Path, *, docker: str, docker_host: str):
        self.recipe_id, self.builder = recipe(source)
        if not Path(docker).is_absolute() or not re.fullmatch(r"unix:///[^\x00-\x20]+", docker_host):
            raise SandboxImageError("沙盒构建只接受本机 Docker socket")
        self.source, self.cache, self.docker, self.docker_host = source, cache, docker, docker_host
        self.root = cache / self.recipe_id
        self.image = "gaoji-app-sandbox:" + self.recipe_id
        self.container = "gaoji-app-image-build-" + self.recipe_id
        self.archive = self.root / "out/image.tar.gz"
        self.marker = self.root / "image.json"
        self.env = {"PATH": "/var/lib/gaoji-app/tools/bin:/run/current-system/sw/bin:/usr/bin:/bin",
                    "HOME": str(self.root / "docker-home"), "DOCKER_HOST": docker_host, "LANG": "C.UTF-8"}

    def run(self, *arguments: str, timeout: int = 30, combined: bool = False) -> str:
        try:
            result = subprocess.run([self.docker, *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT if combined else subprocess.PIPE,
                timeout=timeout, env=self.env, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            write_json(self.root / "docker-error.json", {"operation": arguments[0], "exception": type(error).__name__,
                "timeout_seconds": timeout, "errno": getattr(error, "errno", None)})
            raise SandboxImageError("沙盒镜像操作结果尚未确认，重试会先检查原构建，不会重复安装") from None
        if result.returncode:
            write_json(self.root / "docker-error.json", {"operation": arguments[0], "exit_code": result.returncode,
                "detail": ((result.stdout or b"") + (result.stderr or b""))[-65536:].decode("utf-8", errors="replace")})
            message = {
                "pull": "固定工具镜像下载失败，请检查服务器联网后重试；已下载部分会复用",
                "create": "沙盒构建容器创建失败，请检查容器引擎和可用资源后重试",
                "load": "工具镜像加载失败，请检查容器引擎空间后重试；不会重新编译已有产物",
            }.get(arguments[0], "沙盒镜像操作失败，已保留本次构建供检查或重试")
            raise SandboxImageError(message)
        return result.stdout.decode("utf-8", errors="replace").strip()

    def directory(self, path: Path, mode: int = 0o700) -> None:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise SandboxImageError("沙盒构建目录归属异常")
        os.chmod(path, mode)

    def existing_image(self) -> dict | None:
        listing = self.run("image", "ls", "--quiet", "--no-trunc", self.image)
        if not listing:
            return None
        value = json.loads(self.run("image", "inspect", "--format", "{{json .}}", self.image))
        identifier = value.get("Id", "")
        if (not re.fullmatch(r"sha256:[a-f0-9]{64}", identifier) or value.get("Os") != "linux" or value.get("Architecture") != "amd64"
                or value.get("Config", {}).get("User") != "1000:1000"
                or value.get("Config", {}).get("Labels", {}).get("io.gaoji.app.recipe") != self.recipe_id):
            raise SandboxImageError("已有同名镜像不符合本应用记录，不覆盖或删除")
        size = value.get("Size")
        if type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES:
            raise SandboxImageError("沙盒镜像大小异常或超过 8 GiB，暂不启用")
        if self.marker.exists() and read_json(self.marker).get("image_id") != identifier:
            raise SandboxImageError("沙盒镜像与上次验证记录不一致，暂不启用")
        return {"schema": 1, "recipe": self.recipe_id, "image": self.image, "image_id": identifier,
                "bytes": size, "platform": "linux/amd64"}

    def verify_loaded(self, expected: dict, ready: dict) -> dict:
        # containerd uses a manifest ID; the classic store uses a config ID.
        # Compare executable configuration and every uncompressed layer digest.
        value = json.loads(self.run("image", "inspect", "--format", "{{json .}}", ready["image_id"]))
        actual = value.get("Config", {})
        wanted = expected["config"]
        empty = (None, "", [], {}, False)
        config_matches = isinstance(actual, dict) and all(
            actual.get(key) == wanted.get(key) or (actual.get(key) in empty and wanted.get(key) in empty)
            for key in actual.keys() | wanted.keys())
        if (value.get("Id") != ready["image_id"] or value.get("Os") != "linux"
                or value.get("Architecture") != "amd64" or not config_matches
                or value.get("RootFS", {}).get("Type") != "layers"
                or value.get("RootFS", {}).get("Layers") != expected["layers"]):
            raise SandboxImageError("加载后的沙盒镜像配置或文件层与构建产物不一致，暂不启用")
        return {**ready, "config_id": expected["config_id"]}

    def verify_integrity(self, ready: dict) -> dict:
        name = "gaoji-app-image-check-" + uuid.uuid4().hex
        label = "gaoji.app.image-check=" + self.recipe_id
        try:
            self.run("run", "--rm", "--platform", "linux/amd64", "--pull=never",
                "--name", name, "--label", label, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64",
                "--user", "0:0", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                "--tmpfs", "/root:rw,nosuid,nodev,size=32m", "--entrypoint", "/bin/nix",
                ready["image_id"], "--extra-experimental-features", "nix-command read-only-local-store",
                "store", "verify", "--store", "local?read-only=true", "--all", "--no-trust", timeout=300)
        except SandboxImageError as error:
            raise SandboxImageError("沙盒镜像内容校验未通过，未启用；请查看私有构建日志") from error
        finally:
            # A Docker client timeout does not stop its container. Only reap the
            # exact randomly named check belonging to this attempt, never an image user.
            identifier = self.run("ps", "-a", "--no-trunc", "--quiet", "--filter", "name=^/" + name + "$",
                                  "--filter", "label=" + label)
            if identifier:
                if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                    raise SandboxImageError("镜像校验容器状态异常，未执行清理")
                check = json.loads(self.run("inspect", "--format", "{{json .}}", identifier))
                if (check.get("Id") != identifier or check.get("Name") != "/" + name
                        or check.get("Config", {}).get("Image") != ready["image_id"]
                        or check.get("Config", {}).get("Labels", {}).get("gaoji.app.image-check") != self.recipe_id):
                    raise SandboxImageError("镜像校验容器归属异常，未执行清理")
                self.run("rm", "-f", identifier)
        return {**ready, "integrity": "nix-store-v1"}

    def builder_mount_arguments(self):
        return ["--mount", f"type=bind,source={self.root / 'recipe'},target=/recipe,readonly",
                "--mount", f"type=bind,source={self.root / 'out'},target=/out"]

    def builder_mounts_match(self, value):
        mounts = value.get("Mounts", [])
        expected = {"/recipe": (str(self.root / "recipe"), False), "/out": (str(self.root / "out"), True)}
        actual = {item.get("Destination"): (item.get("Source"), item.get("RW")) for item in mounts if item.get("Type") == "bind"}
        return actual == expected and len(mounts) == 2

    def existing_builder(self) -> dict | None:
        listing = self.run("ps", "-a", "--no-trunc", "--quiet", "--filter", "name=^/" + self.container + "$")
        if not listing:
            return None
        if not re.fullmatch(r"[a-f0-9]{64}", listing):
            raise SandboxImageError("沙盒构建容器清单异常")
        values = json.loads(self.run("inspect", listing))
        if len(values) != 1:
            raise SandboxImageError("沙盒构建容器记录异常")
        value = values[0]
        config = value.get("Config", {})
        labels = config.get("Labels", {})
        if (value.get("Name") != "/" + self.container or value.get("Id") != listing
                or labels.get("gaoji.app.image-build") != self.recipe_id or config.get("Image") != self.builder["image"]
                or config.get("Entrypoint") != ["/bin/sh"] or config.get("Cmd") != ["/recipe/build.sh"]
                or "GAOJI_RECIPE_ID=" + self.recipe_id not in config.get("Env", [])
                or not self.builder_mounts_match(value) or value.get("HostConfig", {}).get("Privileged")):
            raise SandboxImageError("已有构建容器不属于本次受限构建，不覆盖或删除")
        return value

    def prepare_builder(self) -> None:
        if shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
            raise SandboxImageError("沙盒构建至少需要 12 GiB 可用空间，不会自动清理用户文件")
        memory = self.run("info", "--format", "{{.MemTotal}}")
        if not memory.isdecimal() or int(memory) < MIN_ENGINE_MEMORY:
            raise SandboxImageError("首次安装沙盒需要容器引擎至少 6 GiB 内存；普通聊天可不启用沙盒")
        self.directory(self.root / "recipe", 0o755)
        self.directory(self.root / "out")
        for name in FILES:
            target = self.root / "recipe" / name
            if target.is_symlink():
                raise SandboxImageError("构建输入路径异常")
            content = regular(self.source / name, 128 * 1024)
            if target.exists():
                if regular(target, 128 * 1024) != content:
                    raise SandboxImageError("已有构建输入与固定配方不一致，未覆盖")
                continue
            # These are public, checksummed build sources, never instance keys.
            with target.open("xb") as output:
                output.write(content)
                os.fchmod(output.fileno(), 0o444)
        try:
            # A verified digest already loaded locally needs no registry call.
            self.run("image", "inspect", self.builder["image"], timeout=20)
        except SandboxImageError:
            for attempt in range(3):
                try:
                    self.run("pull", "--platform", "linux/amd64", self.builder["image"], timeout=600)
                    break
                except SandboxImageError:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        self.run("create", "--platform", "linux/amd64", "--pull=never", "--name", self.container,
            "--label", "gaoji.app.image-build=" + self.recipe_id,
            "--memory", "4g", "--memory-swap", "4g", "--cpus", "2", "--pids-limit", "256",
            "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
            "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "KILL",
            "--security-opt", "no-new-privileges", "--init", "--restart", "no", "--stop-timeout", "15",
            "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
            *self.builder_mount_arguments(),
            "--env", "GAOJI_RECIPE_ID=" + self.recipe_id, "--entrypoint", "/bin/sh", self.builder["image"], "/recipe/build.sh", timeout=180)

    def clean_builder(self) -> None:
        builder = self.existing_builder()
        if builder:
            if builder["State"].get("Running"):
                raise SandboxImageError("原构建仍在运行，未重复启动或清理")
            self.run("rm", builder["Id"])
        for path in (self.archive, self.archive.with_name("image.tar.gz.partial")):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SandboxImageError("构建临时归档路径异常，未删除")
                path.unlink()

    def ensure(self, progress=None, *, admit=None) -> dict:
        progress = progress or (lambda _text: None)
        self.directory(self.cache)
        self.directory(self.root)
        self.directory(self.root / "docker-home")
        with file_lock(self.root / ".build.lock", timeout=0.1):
            existing = self.existing_image()
            if existing:
                if not self.marker.exists():
                    if not self.archive.exists():
                        raise SandboxImageError("同名沙盒镜像缺少安装校验记录，未直接启用")
                    existing = self.verify_loaded(archive_image(self.archive, self.image), existing)
                if self.marker.exists():
                    recorded = read_json(self.marker)
                    existing["config_id"] = recorded.get("config_id", "")
                    if recorded.get("integrity") == "nix-store-v1":
                        existing["integrity"] = "nix-store-v1"
                if existing.get("integrity") != "nix-store-v1":
                    progress("正在离线校验沙盒工具内容")
                    existing = self.verify_integrity(existing)
                progress("复用已验证的沙盒镜像")
                write_json(self.marker, existing)
                self.clean_builder()
                return existing
            if admit is not None:
                admit(4 * 1024**3)
            builder = self.existing_builder()
            if builder is None:
                progress("正在准备固定版本的沙盒构建器")
                self.prepare_builder()
                builder = self.existing_builder()
            if builder is None:
                raise SandboxImageError("尚未确认沙盒构建容器已创建")
            if not builder["State"].get("Running") and not (builder["State"].get("Status") == "exited"
                    and builder["State"].get("ExitCode") == 0 and self.archive.exists()):
                self.run("start", builder["Id"])
            progress("正在安装 Nix、中文文档与开发工具，首次构建可能较久")
            started = time.monotonic()
            deadline, next_progress = started + 3700, started + 15
            while True:
                builder = self.existing_builder()
                if builder is None:
                    raise SandboxImageError("沙盒构建容器消失，状态未确认")
                if not builder["State"].get("Running"):
                    if builder["State"].get("Status") != "exited" or builder["State"].get("ExitCode") != 0:
                        log = self.run("logs", "--tail", "80", builder["Id"], timeout=15, combined=True)
                        (self.root / "build-error.log").write_text(log[:65536], encoding="utf-8")
                        raise SandboxImageError("沙盒工具镜像未构建成功，已保留私有构建日志和缓存，可重试")
                    break
                now = time.monotonic()
                if now >= deadline:
                    raise SandboxImageError("沙盒构建仍在运行，重试会接管原构建，不会重新创建")
                if now >= next_progress:
                    progress(f"沙盒工具构建仍在运行，本次等待 {int(now - started)} 秒")
                    next_progress = now + 15
                time.sleep(2)
            progress("正在校验并加载沙盒镜像")
            expected = archive_image(self.archive, self.image)
            self.run("load", "--input", str(self.archive), timeout=600)
            ready = self.existing_image()
            if ready is None:
                raise SandboxImageError("加载后未找到沙盒镜像，暂不启用")
            ready = self.verify_loaded(expected, ready)
            progress("正在离线校验沙盒工具内容")
            ready = self.verify_integrity(ready)
            write_json(self.marker, ready)
            self.clean_builder()
            progress("沙盒镜像已校验并安装")
            return ready
