"""Deterministic stdlib-only guest helper; never bundle Bot code or credentials."""

import io
from pathlib import Path
import zipfile

MODULES = (
    "src/local_runtime/files.py", "src/project_content.py", "src/project_capacity.py",
    "src/sandbox_runtime/errors.py", "src/sandbox_runtime/project_volume_journal.py",
    "src/sandbox_runtime/project_files.py", "src/sandbox_runtime/project_rebuild.py",
    "src/sandbox_runtime/project_vm_guest.py", "src/sandbox_runtime/project_vm_gate.py",
    "src/sandbox_runtime/project_vm_file_guest.py",
    "src/sandbox_runtime/project_vm_dependency_guest.py",
    "src/sandbox_runtime/project_usage.py", "src/sandbox_runtime/project_vm_usage_guest.py",
)


def build_bundle():
    root = Path(__file__).resolve().parents[2]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        sources = {name: (root / name).read_bytes() for name in MODULES}
        sources.update({"src/__init__.py": b"", "src/local_runtime/__init__.py": b"",
                        "src/sandbox_runtime/__init__.py": b"",
                        "__main__.py": b"from src.sandbox_runtime.project_vm_file_guest import main\nmain()\n"})
        for name, data in sorted(sources.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return buffer.getvalue()
