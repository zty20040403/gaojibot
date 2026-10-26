"""Fixed disposable guest fixtures for the real App purge acceptance."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def main(request):
    assert set(request) == {"action", "vm_id", "job", "run", "instance_id"}
    assert request["action"] in {"seed", "snapshot", "verify-removed", "verify-tombstone", "cleanup"}
    assert re.fullmatch(r"gaoji-local-qa-[a-f0-9]{32}", request["vm_id"])
    assert Path("/etc/gaoji-local-qa").read_text().strip() == request["vm_id"]
    assert all(re.fullmatch(r"[a-f0-9]{32}", request[k]) for k in ("job", "run", "instance_id"))
    root = Path("/var/lib/gaoji-app")
    deployment = json.loads((root / "jobs" / (request["job"] + ".json")).read_text())
    identifier, run = request["instance_id"], request["run"]
    assert deployment["instance_id"] == identifier
    assert deployment["docker_host"].startswith("unix:///")
    assert Path(deployment["docker"]).is_absolute()
    assert re.fullmatch(r"gaoji-app-runtime:[a-f0-9]{64}", deployment["runtime_image"])
    directory = root / "data/instances" / identifier
    if not deployment.get("purged_at"):
        settings = json.loads((directory / "instance.json").read_text())["settings"]
        assert settings["models"] == [] and settings["bot_qq"] == ""
        assert json.loads((directory / "config/desired.json").read_text())["bot_enabled"] is False
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8", "HOME": "/root",
           "DOCKER_HOST": deployment["docker_host"]}

    def docker(*args):
        result = subprocess.run([deployment["docker"], *args], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if result.returncode:
            raise RuntimeError("Fixed QA Docker command failed: " + result.stderr.decode(errors="replace")[:1000])
        return result.stdout.decode().strip()

    def inspect(kind, name):
        return json.loads(docker("inspect", "--type", kind, "--format", "{{json .}}", name))

    def listing(kind):
        args = {"container": ("ps", "-aq", "--no-trunc"), "volume": ("volume", "ls", "-q"),
                "network": ("network", "ls", "-q", "--no-trunc")}[kind]
        return sorted(docker(*args).split())

    outside = "gaoji-qa-bystander-" + run
    own_volume = "gaoji-app-" + identifier + "-work-s" + run[:6]
    outside_dir = Path("/var/lib/gaoji-qa-" + run)
    marker = ("Unrelated QA data " + run + "\n").encode()
    if request["action"] == "seed":
        assert not deployment.get("purged_at")
        config = json.loads((directory / "instance.json").read_text())
        assert config["settings"]["models"] == [] and config["settings"]["bot_qq"] == ""
        assert json.loads((directory / "config/desired.json").read_text())["bot_enabled"] is False
        outside_dir.mkdir(mode=0o700, exist_ok=True)
        assert not outside_dir.is_symlink()
        filename = outside_dir / "keep.txt"
        if filename.exists():
            assert filename.read_bytes() == marker
        else:
            with filename.open("xb") as stream:
                stream.write(marker)
            filename.chmod(0o600)
        for volume, labels in (
            (outside, {"gaoji.qa.run": run}),
            (own_volume, {"gaoji.app.instance": identifier, "gaoji.app.resource": "workspace",
                          "qqbot.id": "s" + run[:6], "qqbot.owner": run[:20], "gaoji.qa.run": run}),
        ):
            if volume not in listing("volume"):
                arguments = ["volume", "create"]
                for key, value in labels.items():
                    arguments += ["--label", key + "=" + value]
                docker(*arguments, volume)
            assert inspect("volume", volume)["Labels"] == labels
        names = docker("ps", "-a", "--format", "{{.Names}}").split()
        if outside not in names:
            docker("run", "-d", "--name", outside, "--label", "gaoji.qa.run=" + run,
                "--restart", "no", "--network", "none", "--read-only", "--user", "0:0",
                "--cpus", "0.1", "--memory", "64m", "--pids-limit", "32", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--mount", "type=volume,source=" + outside + ",target=/qa",
                "--entrypoint", "python", deployment["runtime_image"], "-c",
                "import pathlib,time; pathlib.Path('/qa/keep.txt').write_text('QA volume " + run + "'); time.sleep(86400)")
        assert inspect("container", outside)["Config"]["Labels"]["gaoji.qa.run"] == run
        # A stopped task workspace must be included even without a live sandbox.
        helper = "gaoji-app-" + identifier + "-helper-" + run[:16]
        docker("run", "--rm", "--name", helper, "--network", "none", "--read-only", "--user", "0:0",
            "--memory", "64m", "--cpus", "0.5", "--pids-limit", "32", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--label", "gaoji.app.instance=" + identifier,
            "--label", "gaoji.app.resource=helper", "--mount", "type=volume,source=" + own_volume + ",target=/qa",
            "--entrypoint", "python", deployment["runtime_image"], "-c",
            "from pathlib import Path; Path('/qa/remove.txt').write_text('owned task fixture')")

    if request["action"] == "cleanup":
        assert deployment.get("purged_at") and not directory.exists()
        if outside in docker("ps", "-a", "--format", "{{.Names}}").split():
            row = inspect("container", outside)
            assert row["Config"]["Labels"]["gaoji.qa.run"] == run
            assert row["Name"] == "/" + outside
            docker("rm", "-f", row["Id"])
        if outside in listing("volume"):
            assert inspect("volume", outside)["Labels"] == {"gaoji.qa.run": run}
            docker("volume", "rm", outside)
        if outside_dir.exists():
            assert not outside_dir.is_symlink()
            assert sorted(p.name for p in outside_dir.iterdir()) == ["keep.txt"]
            assert (outside_dir / "keep.txt").read_bytes() == marker
            (outside_dir / "keep.txt").unlink()
            outside_dir.rmdir()
        print(json.dumps({"fixture_cleaned": True}))
        return

    bystander = None
    if request["action"] != "verify-tombstone":
        row = inspect("container", outside)
        assert row["Config"]["Labels"]["gaoji.qa.run"] == run and row["State"]["Running"]
        content = docker("exec", outside, "python", "-c", "from pathlib import Path; print(Path('/qa/keep.txt').read_text())")
        assert content == "QA volume " + run
        assert (outside_dir / "keep.txt").read_bytes() == marker
        bystander = {"id": row["Id"], "started_at": row["State"]["StartedAt"], "pid": row["State"]["Pid"],
            "restarts": row["RestartCount"], "file_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "outside_file_sha256": hashlib.sha256(marker).hexdigest()}
    resources = {kind: listing(kind) for kind in ("container", "volume", "network")}
    networks = []
    for network_id in resources["network"]:
        network = inspect("network", network_id)
        networks.append({"id": network["Id"], "name": network["Name"],
            "driver": network["Driver"], "scope": network["Scope"]})
    networks.sort(key=lambda network: network["name"])
    images = sorted(set(docker("image", "ls", "-q", "--no-trunc").split()))
    units = ["gaoji-app-" + identifier, "gaoji-app-control-" + identifier,
             "gaoji-app-sandbox-" + identifier, "gaoji-app-install-" + request["job"]]
    unit_status = {}
    if request["action"] in {"verify-removed", "verify-tombstone"}:
        assert deployment.get("purged_at") and re.fullmatch(r"[a-f0-9]{32}", deployment["purge_task"])
        assert not directory.exists() and not directory.is_symlink()
        assert not (root / "data/.purge" / deployment["purge_task"]).exists()
        assert not (root / "control-access" / (request["job"] + ".json")).exists()
        for folder, kind in (("lifecycles", "lifecycle"), ("updates", "update")):
            for filename in (root / folder).glob("*.json"):
                record = json.loads(filename.read_text())
                record = record.get("request", {}) if kind == "update" else record
                if record.get("job") == request["job"]:
                    assert re.fullmatch(r"[a-f0-9]{32}", record["id"])
                    units.append("gaoji-app-" + kind + "-" + record["id"])
        for name in units:
            name += ".service"
            status = subprocess.run(["systemctl", "show", name,
                "--property=LoadState,MainPID,FragmentPath,ActiveState"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            values = dict(line.split("=", 1) for line in status.stdout.decode().splitlines() if "=" in line)
            assert values.get("MainPID") == "0" and values.get("FragmentPath") == "", (name, values)
            assert values.get("LoadState") == "not-found" and values.get("ActiveState") in {"inactive", "failed"}, (name, values)
            path = Path("/usr/local/lib/systemd/system") / name
            link = path.parent / "multi-user.target.wants" / name
            assert not path.exists() and not link.exists() and not link.is_symlink()
            unit_status[name] = values
    print(json.dumps({"instance_id": identifier, "resources": resources, "images": images,
        "networks": networks,
        "bystander": bystander,
        "own_volume": own_volume, "directory_exists": directory.exists(), "units": unit_status,
        "purged_at": deployment.get("purged_at"), "purge_task": deployment.get("purge_task"),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}))
