"""Private, two-phase filesystem resize; never changes a host block device.

Growth: prepare -> host grows device -> apply -> commit.
Shrink: prepare (unmount) -> apply -> host shrinks device -> commit.
The host must persist its own matching intent and verify the qcow2 geometry.
"""

if __package__:
    from . import project_vm_guest as disk
    from .project_vm_gate import _read_private
else:
    import project_disk as disk
    from project_gate import _read_private

HELPER_PATH = "/usr/local/lib/gaoji/project_resize.py"
JOURNAL_NAME = "gaoji-project-resize.json"


def journal(gate):
    try:
        value = _read_private(gate.state_path.parent / JOURNAL_NAME)
    except FileNotFoundError:
        return None
    fields = {*gate.identity, "epoch", "from_quota_bytes", "quota_bytes", "mounted", "phase"}
    if (not isinstance(value, dict) or set(value) != fields
            or any(type(value[key]) is not type(expected) or value[key] != expected
                   for key, expected in gate.identity.items())
            or type(value["epoch"]) is not int or not 1 <= value["epoch"] < 2**53
            or type(value["mounted"]) is not bool
            or not isinstance(value["phase"], str)
            or value["phase"] not in {"prepared", "filesystem_ready", "completed"}):
        raise disk.GuestDiskError("Invalid project resize journal")
    for key in ("from_quota_bytes", "quota_bytes"):
        disk.validate_config({**gate.config, "quota_bytes": value[key]})
    if value["from_quota_bytes"] == value["quota_bytes"]:
        raise disk.GuestDiskError("Resize journal has no capacity change")
    return value


def require_settled(gate):
    state = journal(gate)
    if state is not None and (state["phase"] != "completed" or gate.config["quota_bytes"] != state["quota_bytes"]):
        raise disk.GuestDiskError("Project disk resize is unfinished; execution stays closed")


class ProjectGuestResize:
    def __init__(self, gate):
        self.gate = gate
        self.workspace = gate.workspace
        self.host = self.workspace.host
        self.path = gate.state_path.parent / JOURNAL_NAME

    def _save(self, value):
        disk.write_private_json(self.path, value)

    def _geometry(self, state, *, device_sizes, filesystem_sizes):
        device = self.host.device()
        if (device.get("serial") != self.gate.identity["serial"]
                or type(device.get("bytes")) is not int or device["bytes"] not in device_sizes
                or not isinstance(device.get("number"), str) or ":" not in device["number"]):
            raise disk.GuestDiskError("Resize device identity or capacity is not the journalled target")
        mounted = self.workspace._mount(device) is not None
        filesystem = self.host.filesystem()
        size = self.host.filesystem_bytes()
        if (filesystem.get("TYPE") != "ext4" or filesystem.get("UUID") != state["filesystem_uuid"]
                or type(size) is not int or size not in filesystem_sizes):
            raise disk.GuestDiskError("Resize filesystem identity or geometry is unverified")
        return device["bytes"], size, mounted

    def _receipt(self, state):
        if state["phase"] == "completed":
            sizes = {state["quota_bytes"]}
        else:
            sizes = {state["from_quota_bytes"], state["quota_bytes"]}
        filesystem_sizes = sizes if state["phase"] == "prepared" else {state["quota_bytes"]}
        device, filesystem, mounted = self._geometry(state, device_sizes=sizes, filesystem_sizes=filesystem_sizes)
        if (state["phase"] == "filesystem_ready" and state["quota_bytes"] < state["from_quota_bytes"]
                and device != state["quota_bytes"] and mounted):
            raise disk.GuestDiskError("Host truncation requires an unmounted project filesystem")
        return {**state, "was_mounted": state["mounted"], "mounted": mounted,
                "device_bytes": device, "filesystem_bytes": filesystem,
                "verified": True, "mode": "quiesced"}

    def operate(self, phase, epoch, from_quota_bytes, quota_bytes):
        gate = self.gate
        gate._epoch(epoch)
        if not isinstance(phase, str) or phase not in {"prepare", "apply", "commit"}:
            raise disk.GuestDiskError("Invalid project resize phase")
        for value in (from_quota_bytes, quota_bytes):
            disk.validate_config({**gate.config, "quota_bytes": value})
        if from_quota_bytes == quota_bytes:
            raise disk.GuestDiskError("Project resize requires a different quota")
        intent = {**gate.identity, "epoch": epoch, "from_quota_bytes": from_quota_bytes, "quota_bytes": quota_bytes}
        with gate._lock("control"), gate._lock("io"), gate._lock("disk"):
            current = gate._state()
            if current["mode"] != "quiesced" or not 0 < current["epoch"] <= epoch:
                raise disk.GuestDiskError("Resize requires completed writer quiescence")
            gate.commands.require_cgroups()
            if not gate.commands.empty() or gate.commands.sandbox_processes():
                raise disk.GuestDiskError("Guest writers prevent project resize")
            # Re-read disk configuration under the same locks as resize/mount.
            if disk.load_config() != gate.config:
                raise disk.GuestDiskError("Project configuration changed before resize")
            state = journal(gate)
            same = state is not None and all(state[key] == value for key, value in intent.items())
            if not same:
                if (phase != "prepare" or gate.config["quota_bytes"] != from_quota_bytes
                        or (state is not None and (state["phase"] != "completed" or epoch <= state["epoch"]))):
                    raise disk.GuestDiskError("Reconcile the original resize before starting another")
                device = self.workspace._device()
                self.workspace._filesystem()
                mounted = self.workspace._mount(device) is not None
                used = self.workspace.verify()["used_bytes"] if mounted else self.host.filesystem_used_bytes()
                if type(used) is not int or not 0 <= used <= from_quota_bytes or used >= quota_bytes:
                    raise disk.GuestDiskError("Requested quota does not fit verified filesystem usage")
                state = {**intent, "mounted": mounted, "phase": "prepared"}
                self._save(state)
            if gate.config["quota_bytes"] not in {from_quota_bytes, quota_bytes}:
                raise disk.GuestDiskError("Resize configuration does not match its intent")
            if state["phase"] == "completed":
                receipt = self._receipt(state)
                if gate.config["quota_bytes"] != quota_bytes or receipt["mounted"] != state["mounted"]:
                    raise disk.GuestDiskError("Completed resize no longer matches the workspace")
                return receipt
            shrinking = quota_bytes < from_quota_bytes
            if phase == "prepare":
                if state["phase"] == "prepared" and shrinking:
                    _, _, mounted = self._geometry(state, device_sizes={from_quota_bytes},
                                                   filesystem_sizes={from_quota_bytes})
                    if mounted:
                        self.workspace.unmount()
                return self._receipt(state)
            if phase == "apply":
                if state["phase"] == "prepared":
                    _, size, mounted = self._geometry(state,
                        device_sizes={from_quota_bytes} if shrinking else {quota_bytes},
                        filesystem_sizes={from_quota_bytes, quota_bytes})
                    if shrinking and mounted:
                        raise disk.GuestDiskError("Shrinking requires an unmounted project filesystem")
                    if size != quota_bytes:
                        if not mounted:
                            self.host.check()
                        self.host.resize(quota_bytes)
                    self._geometry(state, device_sizes={from_quota_bytes} if shrinking else {quota_bytes},
                                   filesystem_sizes={quota_bytes})
                    state["phase"] = "filesystem_ready"
                    self._save(state)
                return self._receipt(state)
            if state["phase"] != "filesystem_ready":
                raise disk.GuestDiskError("Filesystem resize must be verified before committing quota")
            _, _, mounted = self._geometry(state, device_sizes={quota_bytes}, filesystem_sizes={quota_bytes})
            if mounted and not state["mounted"]:
                raise disk.GuestDiskError("An unmounted project was unexpectedly mounted during resize")
            config = {**gate.config, "quota_bytes": quota_bytes}
            disk.write_private_json(disk.CONFIG_PATH, config)
            gate.config = config
            self.workspace.config = config
            if state["mounted"]:
                self.workspace.mount()
            else:
                self.host.prepare_mountpoint()
            self._geometry(state, device_sizes={quota_bytes}, filesystem_sizes={quota_bytes})
            state["phase"] = "completed"
            self._save(state)
            return self._receipt(state)
