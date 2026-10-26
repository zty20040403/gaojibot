"""Opt-in isolated Docker Desktop sandbox and file export acceptance."""
from test_desktop_local_updates_linux import DesktopUpdateLinuxTests as _Base


class DesktopSandboxLinuxTests(_Base):
    driver = "qa-desktop-sandbox.py"
    evidence_name = "local-sandbox-linux.json"
    flow_timeout = 4200


del _Base
