"""Exercise an isolated libvirt session VM without touching existing domains."""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree


def run(*args: str, timeout: int = 60) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])}: {result.stderr.strip()[-500:]}")
    return result.stdout.strip()


def agent(name: str, payload: dict[str, object], *, timeout: int = 20) -> dict[str, object]:
    response = run(
        "virsh", "-c", "qemu:///session", "qemu-agent-command", name,
        json.dumps(payload, separators=(",", ":")),
        "--timeout", str(timeout), timeout=timeout + 5,
    )
    parsed = json.loads(response)
    if "error" in parsed:
        raise RuntimeError(str(parsed["error"]))
    return parsed.get("return", {})


def guest_exec(name: str, command: str, *, timeout: int = 40) -> tuple[int, bytes, bytes]:
    started = agent(name, {
        "execute": "guest-exec",
        "arguments": {
            "path": "/bin/sh", "arg": ["-lc", command], "capture-output": True,
        },
    })
    pid = started["pid"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = agent(name, {
            "execute": "guest-exec-status", "arguments": {"pid": pid},
        })
        if status.get("exited"):
            return (
                int(status.get("exitcode", -1)),
                base64.b64decode(status.get("out-data", "")),
                base64.b64decode(status.get("err-data", "")),
            )
        time.sleep(0.5)
    raise TimeoutError(f"guest command timed out after {timeout}s")


def domain_xml(name: str, disk: Path, seed: Path, socket: Path) -> str:
    domain = ElementTree.Element("domain", {"type": "kvm"})
    ElementTree.SubElement(domain, "name").text = name
    ElementTree.SubElement(domain, "memory", {"unit": "MiB"}).text = "2048"
    ElementTree.SubElement(domain, "vcpu").text = "1"
    os_node = ElementTree.SubElement(domain, "os")
    ElementTree.SubElement(os_node, "type", {"arch": "x86_64"}).text = "hvm"
    devices = ElementTree.SubElement(domain, "devices")
    ElementTree.SubElement(devices, "emulator").text = shutil.which("qemu-system-x86_64")
    for source_path, device, target, bus, fmt in [
        (disk, "disk", "vda", "virtio", "qcow2"),
        (seed, "cdrom", "sda", "sata", "raw"),
    ]:
        node = ElementTree.SubElement(devices, "disk", {"type": "file", "device": device})
        ElementTree.SubElement(node, "driver", {"name": "qemu", "type": fmt})
        ElementTree.SubElement(node, "source", {"file": str(source_path)})
        ElementTree.SubElement(node, "target", {"dev": target, "bus": bus})
        if device == "cdrom":
            ElementTree.SubElement(node, "readonly")
    network = ElementTree.SubElement(devices, "interface", {"type": "user"})
    ElementTree.SubElement(network, "model", {"type": "virtio"})
    channel = ElementTree.SubElement(devices, "channel", {"type": "unix"})
    ElementTree.SubElement(channel, "source", {"mode": "bind", "path": str(socket)})
    ElementTree.SubElement(channel, "target", {
        "type": "virtio", "name": "org.qemu.guest_agent.0",
    })
    console = ElementTree.SubElement(devices, "console", {"type": "pty"})
    ElementTree.SubElement(console, "target", {"type": "serial"})
    return ElementTree.tostring(domain, encoding="unicode")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    image = args.image.resolve(strict=True)
    if not image.name.endswith(".qcow2"):
        parser.error("base image must be a qcow2 file")
    name = "gaoji-smoke-" + secrets.token_hex(4)
    work = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=Path.home()))
    defined = False
    started = False
    try:
        disk = work / "disk.qcow2"
        seed = work / "seed.iso"
        user_data = work / "user-data"
        meta_data = work / "meta-data"
        domain_file = work / "domain.xml"
        run("qemu-img", "create", "-f", "qcow2", "-b", str(image),
            "-F", "qcow2", str(disk), "8G")
        user_data.write_text("""#cloud-config
package_update: true
packages:
  - qemu-guest-agent
users:
  - default
  - name: sandbox
    uid: 1000
    shell: /bin/bash
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
  - [mkdir, -p, /workspace]
  - [chown, sandbox:sandbox, /workspace]
""", encoding="utf-8")
        meta_data.write_text(f"instance-id: {name}\nlocal-hostname: {name}\n", encoding="utf-8")
        run("cloud-localds", str(seed), str(user_data), str(meta_data))
        domain_file.write_text(domain_xml(name, disk, seed, work / "agent.sock"), encoding="utf-8")
        print(f"VM {name}: defining and starting")
        run("virsh", "-c", "qemu:///session", "define", str(domain_file))
        defined = True
        run("virsh", "-c", "qemu:///session", "start", name)
        started = True
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                agent(name, {"execute": "guest-ping"}, timeout=5)
                break
            except (RuntimeError, subprocess.TimeoutExpired):
                time.sleep(3)
        else:
            raise TimeoutError("guest agent did not become ready within 300s")
        code, out, err = guest_exec(name, "cloud-init status --wait", timeout=180)
        if code:
            raise RuntimeError(f"cloud-init failed: {err.decode(errors='replace')[-500:]}")
        code, out, err = guest_exec(
            name,
            "runuser -u sandbox -- sh -lc 'printf vm-ok > /workspace/proof.txt && cat /workspace/proof.txt'",
        )
        if (code, out) != (0, b"vm-ok"):
            raise RuntimeError(f"guest execution failed: {code} {out!r} {err!r}")
        code, out, err = guest_exec(name, "base64 -w0 /workspace/proof.txt")
        if code or base64.b64decode(out) != b"vm-ok":
            raise RuntimeError(f"artifact export failed: {code} {err!r}")
        print("VM smoke passed: guest execution and artifact export")
    finally:
        clean = True
        if started:
            stopped = subprocess.run(
                ["virsh", "-c", "qemu:///session", "destroy", name],
                text=True, capture_output=True, check=False,
            )
            clean = stopped.returncode == 0
        if defined:
            undefined = subprocess.run(
                ["virsh", "-c", "qemu:///session", "undefine", name],
                text=True, capture_output=True, check=False,
            )
            clean = clean and undefined.returncode == 0
        if clean:
            shutil.rmtree(work)
        else:
            print(f"VM cleanup needs attention: {name}; files preserved at {work}")


if __name__ == "__main__":
    main()
