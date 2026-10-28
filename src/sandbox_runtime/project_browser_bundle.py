"""Pinned worker source archive; no application configuration or credentials."""

import io
from pathlib import Path
import zipfile

MODULES = (
    "src/browser_runtime.py",
    "src/sandbox_runtime/errors.py",
    "src/sandbox_runtime/wire.py",
    "src/sandbox_runtime/project_browser_worker.py",
)


def build_bundle(*, guest_control=False):
    root = Path(__file__).resolve().parents[2]
    modules = MODULES if not guest_control else tuple("src/sandbox_runtime/" + name + ".py" for name in (
        "project_vm_browser_guest", "project_vm_gate", "project_vm_guest"))
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
