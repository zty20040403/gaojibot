"""Materialize the file credential required by the existing fleet client."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from .components import component_keys, ComponentError


@contextmanager
def cluster_credentials(token: str):
    os.environ.pop("AI_CLUSTER_CONTROL_TOKEN_FILE", None)
    if os.environ.get("AI_CLUSTER_ENABLED") != "true":
        yield
        return
    token = component_keys({"cluster": token})["cluster"]
    if not token:
        raise ComponentError("集群 Token 尚未配置")
    # /tmp is a private container tmpfs. The sole persistent credential remains
    # in the revisioned runtime configuration, covered by existing backups.
    with tempfile.TemporaryDirectory(prefix="gaoji-cluster-") as directory:
        path = Path(directory) / "token"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token)
        os.environ["AI_CLUSTER_CONTROL_TOKEN_FILE"] = str(path)
        try:
            yield
        finally:
            os.environ.pop("AI_CLUSTER_CONTROL_TOKEN_FILE", None)
