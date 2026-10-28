"""Pinned worker source archive; no application configuration or credentials."""

import io
from pathlib import Path
import re
import tomllib
import zipfile

MODULES = (
    "src/browser_runtime.py",
    "src/sandbox_runtime/errors.py",
    "src/sandbox_runtime/wire.py",
    "src/sandbox_runtime/project_browser_worker.py",
)


def guest_requirements():
    """Export only the wheel-locked browser dependency closure, not the Bot."""
    lock = tomllib.loads((Path(__file__).resolve().parents[2] / "uv.lock").read_text())
    packages = {}
    for package in lock["package"]:
        packages.setdefault(package["name"], []).append(package)
    pending, found = ["playwright"], {}
    while pending:
        name = pending.pop()
        if name in found:
            continue
        choices = packages.get(name, [])
        if len(choices) != 1 or choices[0].get("source") != {"registry": "https://pypi.org/simple"}:
            raise ValueError("Guest browser requires an unambiguous registry lock")
        package = choices[0]
        if re.fullmatch(r"[a-z0-9-]+", name) is None or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", package["version"]) is None:
            raise ValueError("Unsupported browser package identity")
        hashes = sorted({wheel["hash"] for wheel in package.get("wheels", [])})
        if not hashes or any(re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None for value in hashes):
            raise ValueError("Browser dependencies require wheel hashes")
        found[name] = name + "==" + package["version"] + " " + " ".join("--hash=" + h for h in hashes)
        pending.extend(dependency["name"] for dependency in package.get("dependencies", []))
    return "\n".join(found[name] for name in sorted(found)) + "\n"


def build_bundle(*, guest_control=False):
    root = Path(__file__).resolve().parents[2]
    modules = MODULES if not guest_control else tuple("src/sandbox_runtime/" + name + ".py" for name in (
        "project_vm_browser_guest", "project_vm_browser_install", "project_vm_gate", "project_vm_guest", "project_vm_resize_guest"))
    main = "project_vm_browser_guest" if guest_control else "project_browser_worker"
    sources = {name: (root / name).read_bytes() for name in modules}
    sources.update({"src/__init__.py": b"", "src/sandbox_runtime/__init__.py": b"",
                    "__main__.py": f"from src.sandbox_runtime.{main} import main\nmain()\n".encode()})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(sources.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return buffer.getvalue()
