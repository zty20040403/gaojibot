"""Atomic browser admission against the existing project-volume maintenance lock."""

import hashlib

from .project_browser_journal import ProjectBrowserJournal, _runtime
from .project_browser_worker import binding
from .project_volume_journal import ProjectVolumeJournal
from .wire import ProtocolError


class ProjectVolumeBrowserJournal(ProjectBrowserJournal):
    def __init__(self, volumes):
        self.volumes = volumes
        super().__init__(volumes.root / "browser-state", volumes.instance_id)

    def begin(self, config, runtime, *, launch_id=None):
        identity, runtime = binding(config["binding"]), _runtime(runtime)
        # Both admission paths acquire volume -> browser. A maintenance intent
        # cannot pass between checking the workspace and persisting its writer.
        with self.volumes._locked(identity["owner"], identity["project_id"]) as (_, record):
            self.volumes._generation(record, identity["generation"])
            if (record is None or record.get("deleted") or record["pending"]
                    or record.get("maintenance") or identity["fence"] != record["fence"]
                    or config.get("workspace") != "/workspace"):
                raise ProtocolError("Project workspace is not admitted for a browser")
            vm = self.volumes.storage_kind == "qcow2"
            workspace = record.get("workspace") or {}
            sandbox_id = record.get("sandbox_id") if vm else workspace.get("sandbox_id")
            if (runtime["backend"] != ("vm" if vm else "oci")
                    or runtime["sandbox_id"] != sandbox_id
                    or (vm and (not record.get("guest_initialized")
                                or record.get("guest_domain_uuid") != runtime["runtime_id"]))):
                raise ProtocolError("Browser runtime does not match its project workspace")
            # The native driver must additionally verify the live container or
            # domain and its mounted profile before starting any process.
            return super().begin(config, runtime, launch_id=launch_id)

    def require_idle(self, volume):
        """Called under the volume lock, before recording any maintenance intent."""
        with self._locked_project(volume["project_id"]) as (_, record):
            if record is not None:
                identity = record["binding"]
                if (hashlib.sha256(identity["owner"].encode()).hexdigest() != volume["owner_hash"]
                        or identity["generation"] > volume.get("generation", 1)
                        or identity["fence"] > volume["fence"]):
                    raise ProtocolError("Browser receipt conflicts with the current project volume")
            if not self._maintenance_ready(record):
                raise ProtocolError("Close the project browser and persist its result before maintenance")


class ProjectHostVolumeJournal(ProjectVolumeJournal):
    """Only host disk stores own browser receipts; guest file bundles do not."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.browsers = ProjectVolumeBrowserJournal(self)

    def _begin(self, directory, record, operation, fence, arguments):
        self.browsers.require_idle(record)
        return super()._begin(directory, record, operation, fence, arguments)
