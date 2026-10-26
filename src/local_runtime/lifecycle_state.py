"""Shared gates for an instance whose services are being removed or restored."""
from __future__ import annotations

from .files import read_json
from .setup_clients import SetupError


def check_lifecycle(directory, *, operation=None):
    marker = directory / "config/lifecycle.json"
    if marker.exists() or marker.is_symlink():
        value = read_json(marker)
        if operation is None or value.get("id") != operation:
            raise SetupError("实例正在卸载或已保留停用，请从原维护任务继续或重新启用")


def lifecycle_present(directory):
    marker = directory / "config/lifecycle.json"
    return marker.exists() or marker.is_symlink()
