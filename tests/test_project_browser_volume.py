"""Real host journals and admission races; kernel/container effects are fixtures."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

from src.local_runtime.files import read_json
from src.sandbox_runtime.errors import SandboxError
from src.sandbox_runtime.project_volumes import ProjectVolumes, MIB
from src.sandbox_runtime.wire import ProtocolError, encoded
from tests.test_project_volumes import HostFixture
from tests import test_project_vm_mounts as vm_fixtures

DOMAIN, RESERVATION = vm_fixtures.DOMAIN, vm_fixtures.RESERVATION


class BrowserVolumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "volumes"
        self.host = HostFixture()
        self.volumes = ProjectVolumes(self.root, "a" * 32, host=self.host)
        self.volumes.create("scope", 1, quota_bytes=64 * MIB, fence=1)
        self.volumes.mutate("scope", 1, "mount", fence=2)
        self.volumes.begin_workspace("scope", 1, generation=1, fence=3,
            action="start", image="gaoji-app-sandbox:" + "b" * 64)
        self.volumes.finish_workspace("scope", 1, generation=1, fence=3, action="start")
        record = read_json(self.root / "p1/volume.json")
        self.identity = {"instance_id": "a" * 32, "owner": "scope", "project_id": 1,
                         "generation": 1, "fence": 3}
        self.config = {"schema": 1, "binding": self.identity, "workspace": "/workspace",
            "executable_path": "", "allow_private_network": False,
            "max_profile_bytes": 256 * MIB, "cache_bytes": 32 * MIB}
        self.runtime = {"backend": "oci", "sandbox_id": record["workspace"]["sandbox_id"],
                        "runtime_id": "c" * 64}

    def reserve(self):
        return self.volumes.reserve("scope", 1, generation=1, fence=4,
            action="reserve", reservation="b" * 32)

    def test_unstarted_browser_blocks_maintenance_after_reopening_volume_store(self):
        journal = self.volumes.browsers
        session = journal.begin(self.config, self.runtime)
        before = read_json(self.root / "p1/volume.json")
        self.volumes = ProjectVolumes(self.root, "a" * 32, host=self.host)
        with self.assertRaises(ProtocolError):
            self.reserve()
        with self.assertRaises(ProtocolError):
            self.volumes.begin_workspace("scope", 1, generation=1, fence=4,
                action="stop", image="gaoji-app-sandbox:" + "b" * 64)
        self.assertEqual(before, read_json(self.root / "p1/volume.json"))
        sid = session["session_id"]
        journal.closing(self.identity, sid, cancel_pending=True)
        journal.closed(self.identity, sid, {"binding": self.identity, "runtime": self.runtime,
            "session_id": sid, "processes_stopped": True})
        self.assertTrue(self.reserve()["maintenance_active"])
        with self.assertRaises(ProtocolError):
            journal.begin({**self.config, "binding": {**self.identity, "fence": 4}}, self.runtime)

    def test_closed_browser_with_unpublished_receipt_still_blocks_maintenance(self):
        journal = self.volumes.browsers
        session = journal.begin(self.config, self.runtime)
        sid, worker = session["session_id"], "d" * 32
        journal.attach(self.identity, sid,
            {"schema": 1, "binding": self.identity, "state": "ready", "worker_id": worker})
        journal.prepare(self.identity, sid, 1, "browser_snapshot", {})
        reply = {"schema": 1, "binding": self.identity, "worker_id": worker, "sequence": 1,
            "arguments_sha256": journal.current(self.identity)["last"]["arguments_sha256"],
            "state": "completed", "result": {"text": "local fixture"}}
        journal.observe(self.identity, sid, 1, reply)
        journal.closing(self.identity, sid)
        journal.closed(self.identity, sid, {"binding": self.identity, "runtime": self.runtime,
            "session_id": sid, "processes_stopped": True})
        with self.assertRaises(ProtocolError):
            self.reserve()
        journal.acknowledge(self.identity, sid, 1, hashlib.sha256(encoded(reply)).hexdigest())
        self.assertTrue(self.reserve()["maintenance_active"])

    def test_browser_launch_and_maintenance_compete_for_the_same_lock(self):
        start = threading.Barrier(2)

        def run(action):
            start.wait(timeout=5)
            try:
                action()
            except SandboxError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run, lambda: self.volumes.browsers.begin(self.config, self.runtime))
            second = pool.submit(run, self.reserve)
            outcomes = first.result(timeout=10), second.result(timeout=10)
        self.assertEqual(sum(outcomes), 1)
        current = self.volumes.browsers.current(self.identity)
        volume = read_json(self.root / "p1/volume.json")
        self.assertNotEqual(current is not None, bool(volume.get("maintenance")))

    def test_binding_cannot_change_generation_epoch_or_runtime(self):
        for delta in ({"generation": 2}, {"fence": 4}, {"owner": "another"},
                      {"instance_id": "b" * 32}):
            config = {**self.config, "binding": {**self.identity, **delta}}
            with self.subTest(delta=delta), self.assertRaises(SandboxError):
                self.volumes.browsers.begin(config, self.runtime)
        for runtime in ({**self.runtime, "sandbox_id": "s000000"},
                        {**self.runtime, "backend": "vm", "runtime_id": DOMAIN}):
            with self.assertRaises(ProtocolError):
                self.volumes.browsers.begin(self.config, runtime)
        with self.assertRaises(ProtocolError):
            self.volumes.browsers.begin({**self.config, "workspace": str(self.root)}, self.runtime)
        self.assertIsNone(self.volumes.browsers.current(self.identity))


class VmBrowserVolumeTests(unittest.TestCase):
    def test_vm_requires_initialized_exact_domain_and_released_maintenance(self):
        fixture = vm_fixtures.GuestJournalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        grant = fixture.prepare()
        sid = grant["binding"].sandbox_id
        identity = {"instance_id": fixture.store.instance_id, "owner": "scope", "project_id": 1,
                    "generation": 1, "fence": 4}
        config = {"schema": 1, "binding": identity, "workspace": "/workspace",
            "executable_path": "", "allow_private_network": False,
            "max_profile_bytes": 256 * MIB, "cache_bytes": 32 * MIB}
        runtime = {"backend": "vm", "sandbox_id": sid, "runtime_id": DOMAIN}
        with self.assertRaises(ProtocolError):
            fixture.store.browsers.begin(config, runtime)
        fixture.host.attached = {"gaoji-vm-" + sid}
        fixture.acknowledge()
        fixture.store.reserve("scope", 1, generation=1, fence=6,
            action="release", reservation=RESERVATION)
        config = deepcopy(config)
        config["binding"]["fence"] = 6
        with self.assertRaises(ProtocolError):
            fixture.store.browsers.begin(config, {**runtime,
                "runtime_id": "33333333-3333-4333-8333-333333333333"})
        session = fixture.store.browsers.begin(config, runtime)
        self.assertEqual(session["state"], "starting")
        with self.assertRaises(ProtocolError):
            fixture.reopen().reserve("scope", 1, generation=1, fence=7,
                action="reserve", reservation=RESERVATION)
