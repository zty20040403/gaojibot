"""Pinned prebuilt images with an explicit, observable source-build fallback."""
from __future__ import annotations

import json
from pathlib import Path
import re
import selectors
import subprocess
import time

from .releases import read_regular, ReleaseError


def install_options(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"mode", "source"}:
        raise ReleaseError("安装选项无效")
    result = {"mode": value.get("mode", "auto"), "source": value.get("source", "official")}
    if result["mode"] not in {"auto", "prebuilt", "source"} or result["source"] not in {"official", "tuna"}:
        raise ReleaseError("安装来源无效")
    return result


def catalog_image(value, checksum, platform="linux/amd64", kind="runtime"):
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("checksum") != checksum or not isinstance(value.get("images"), dict):
        raise ReleaseError("预构建镜像清单与运行包不一致")
    for arch, entries in value["images"].items():
        if arch not in {"linux/amd64", "linux/arm64"} or not isinstance(entries, dict) or set(entries) != {"runtime", "controller"}:
            raise ReleaseError("预构建镜像架构清单无效")
        for image in entries.values():
            if not isinstance(image, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.:-]*/[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}", image):
                raise ReleaseError("预构建镜像必须固定摘要")
    return value["images"].get(platform, {}).get(kind)


def import_policy(work: Path, release: Path, checksum: str):
    from .files import write_json
    for name in ("runtime-images.json", "install-options.json"):
        path = work / name
        if not path.exists():
            continue
        value = json.loads(read_regular(path, 32768))
        if name == "runtime-images.json":
            catalog_image(value, checksum)
        else:
            install_options(value)
        write_json(release / ("." + name), value)


def prepare_image(docker, release: Path, checksum, target, run, progress):
    options_file, catalog_file = release / ".install-options.json", release / ".runtime-images.json"
    options = install_options(json.loads(read_regular(options_file, 4096)) if options_file.exists() else None)
    remote = catalog_image(json.loads(read_regular(catalog_file, 32768)), checksum) if catalog_file.exists() else None
    if remote and options["mode"] != "source":
        progress("正在下载预构建运行镜像（固定摘要）")
        run([docker, "pull", "--platform", "linux/amd64", remote], timeout=1800)
        raw = run([docker, "image", "inspect", remote], timeout=30, capture=True)
        metadata = json.loads(raw)[0]
        if metadata.get("Config", {}).get("Labels", {}).get("gaoji.desktop.runtime-digest") != checksum or metadata.get("Architecture") != "amd64":
            raise ReleaseError("预构建镜像的版本或架构校验失败，未使用该镜像")
        run([docker, "tag", remote, target], timeout=30)
    elif options["mode"] == "prebuilt":
        raise ReleaseError("此版本尚未附带已发布的预构建镜像；请选择源码构建，或使用附带镜像的发行版")
    else:
        progress("正在源码构建运行镜像 · " + ("清华 Debian 镜像源" if options["source"] == "tuna" else "官方 HTTPS 源"))
        run([docker, "build", "--progress=plain", "--platform", "linux/amd64", "--build-arg", "DOWNLOAD_SOURCE=" + options["source"],
             "--label", "gaoji.desktop.runtime-digest=" + checksum, "--tag", target, "--file", str(release / "Dockerfile"), str(release)], timeout=3600)


def progress_detail(line: str):
    # The public status contains categories and package names, never raw log URLs.
    if match := re.search(r"\bGet:\d+\s+\S+\s+\S+\s+\S+\s+([a-z0-9][a-z0-9+.-]{0,80})\s", line):
        return "下载系统依赖：" + match[1]
    for pattern, label in ((r"Downloading|Downloading layer|Pulling fs layer", "下载镜像或 Python 依赖"),
                           (r"Extracting|Unpacking", "解压依赖"), (r"Setting up", "配置系统依赖"),
                           (r"Building CXX|Building C object|Linking CXX", "编译语音组件"),
                           (r"uv sync|Resolved .* packages|Prepared .* packages", "安装 Python 依赖"),
                           (r"exporting layers|exporting to image", "保存运行镜像"),
                           (r"Pull complete|Already exists|CACHED", "复用或完成镜像层")):
        if re.search(pattern, line, re.I):
            return label
    return None


def failure_detail(line: str):
    for pattern, message in (
        (r"no space left on device|disk quota exceeded", "构建磁盘空间不足，请先检查 Docker 可用空间"),
        (r"curl: \(28\)", "构建依赖下载超时，当前下载连接过慢或中断"),
        (r"could not resolve host|temporary failure in name resolution|no such host", "下载地址解析失败，请检查服务器 DNS"),
        (r"certificate verify failed|certificate verification failed|SSL certificate problem", "下载连接的证书验证失败，请检查服务器时间和证书"),
        (r"unauthorized:|pull access denied|toomanyrequests:", "镜像仓库拒绝下载或限制频率，请核对仓库权限和限额"),
        (r"did NOT match|checksum.*mismatch|hash mismatch", "下载文件校验失败，已停止使用该文件"),
    ):
        if re.search(pattern, line, re.I):
            return message
    return None


def run_observed(arguments, *, timeout, environment, log, report):
    started, last_output, pending = time.monotonic(), time.monotonic(), b""
    detail, last_report, failure = "正在连接下载源或准备构建", 0.0, ""
    with subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment) as child:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            try:
                while True:
                    now = time.monotonic()
                    if now - started > timeout:
                        raise subprocess.TimeoutExpired(arguments, timeout)
                    events = selector.select(timeout=1)
                    for key, _ in events:
                        block = key.fileobj.read1(8192)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        log.write(block)
                        last_output = time.monotonic()
                        pending += block
                        lines = re.split(b"[\r\n]", pending)
                        pending = lines.pop()[-8192:]
                        for line in lines:
                            decoded = line.decode("utf-8", errors="replace")
                            detail = progress_detail(decoded) or detail
                            failure = failure_detail(decoded) or failure
                    if now - last_report >= 1:
                        report({"detail": detail, "elapsed_seconds": int(now - started), "quiet_seconds": int(now - last_output), "failure": failure})
                        last_report = now
                    if not selector.get_map() and child.poll() is not None:
                        failure = failure_detail(pending.decode("utf-8", errors="replace")) or failure
                        report({"detail": detail, "elapsed_seconds": int(now - started), "quiet_seconds": int(now - last_output),
                            "failure": failure if child.returncode else ""})
                        return child.returncode
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
