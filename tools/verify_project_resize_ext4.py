"""Real ext4 resize and content verification on disposable regular-file disks.

This proves filesystem commands and the guest journal, not KVM/block-device,
mount, cgroup, power-loss or libvirt acceptance. Never mount a host workspace.
Requires Python and e2fsprogs; run as root inside a disposable Linux container.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sandbox_runtime import project_vm_guest as disk
from src.sandbox_runtime.project_vm_gate import ProjectGuestGate
from src.sandbox_runtime.project_vm_resize_guest import ProjectGuestResize

MIB = 1024**2


class EmptyFixtureCommands:
    def require_cgroups(self):
        pass

    def stop(self):
        pass

    def empty(self):
        return True

    def sandbox_processes(self):
        return False


class FileDisk(disk.LinuxGuestDisk):
    def __init__(self, image, serial):
        self.image, self.serial = image, serial
        self.resizes = 0
        self.lose_ack = True

    def device(self):
        return {"serial": self.serial, "bytes": self.image.stat().st_size, "number": "fixture:regular-file"}

    def mounts(self):
        return []

    def prepare_mountpoint(self):
        pass

    def resize(self, quota):
        capacity = self.image.stat().st_size
        super().resize(quota)
        # resize2fs truncates a regular file on shrink, unlike a block device.
        # Preserve the simulated device capacity until the separate host step.
        if self.image.stat().st_size < capacity:
            truncate(self.image, capacity)
        self.resizes += 1
        if self.lose_ack:
            self.lose_ack = False
            raise OSError("fixture lost resize acknowledgement")


def truncate(path, size):
    with path.open("r+b") as stream:
        stream.truncate(size)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Use a disposable root Linux container, not a production host")
    with tempfile.TemporaryDirectory(prefix="gaoji-real-resize-") as temporary:
        root = Path(temporary)
        image = root / "project.ext4"
        image.touch(mode=0o600)
        truncate(image, 64 * MIB)
        fs_uuid = "11111111-1111-4111-8111-111111111111"
        config = {"project_id": 1, "generation": 1, "filesystem_uuid": fs_uuid,
                  "quota_bytes": 64 * MIB, "initialize": False,
                  "serial": disk.disk_serial(1, 1, fs_uuid)}
        disk.DEVICE, disk.CONFIG_PATH = image, root / "config.json"
        disk.write_private_json(disk.CONFIG_PATH, config)
        host = FileDisk(image, config["serial"])
        host.run("mkfs.ext4", "-q", "-F", "-m", "0", "-U", fs_uuid, str(image))
        marker = root / "retained.txt"
        marker.write_text("project data survives grow and shrink\n")
        host.run("debugfs", "-w", "-R", f"write {marker} /retained.txt", str(image))

        def gate():
            current = disk.load_config()
            return ProjectGuestGate(current, state_path=root / "gate.json", lock_root=root,
                commands=EmptyFixtureCommands(), workspace=disk.ProjectGuestWorkspace(current, host=host))

        def resize(phase, old, new, epoch):
            return ProjectGuestResize(gate()).operate(phase, epoch, old * MIB, new * MIB)

        gate().quiesce(1)
        resize("prepare", 64, 96, 2)
        truncate(image, 96 * MIB)
        try:
            resize("apply", 64, 96, 2)
        except OSError as exc:
            if str(exc) != "fixture lost resize acknowledgement":
                raise
        else:
            raise AssertionError("Fault injection was not reached")
        resize("apply", 64, 96, 2)
        grown = resize("commit", 64, 96, 2)
        assert host.resizes == 1 and grown["filesystem_bytes"] == 96 * MIB
        resize("prepare", 96, 64, 3)
        reduced = resize("apply", 96, 64, 3)
        assert reduced["filesystem_bytes"] == 64 * MIB and image.stat().st_size == 96 * MIB
        truncate(image, 64 * MIB)
        shrunk = resize("commit", 96, 64, 3)
        assert host.resizes == 2 and shrunk["filesystem_bytes"] == 64 * MIB
        assert host.filesystem()["UUID"] == fs_uuid
        assert host.run("debugfs", "-R", "cat /retained.txt", str(image)) == marker.read_text()
        checked = subprocess.run(["e2fsck", "-f", "-n", str(image)], capture_output=True, check=False)
        assert checked.returncode == 0, checked.stderr.decode(errors="replace")
        print(json.dumps({"backend": "real_ext4_regular_file", "initial_bytes": 64 * MIB,
            "grown_bytes": grown["filesystem_bytes"], "shrunk_bytes": shrunk["filesystem_bytes"],
            "lost_ack_replayed_without_resize": True, "retained_content_verified": True,
            "filesystem_check_exit": checked.returncode, "virtual_machine_tested": False}))


if __name__ == "__main__":
    main()
