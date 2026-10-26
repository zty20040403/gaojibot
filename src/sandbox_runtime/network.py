"""Root-owned OCI egress policy; never fall back to an unrestricted bridge."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess

NETWORK = "gaoji-sandbox"
BRIDGE = "gaoji-sbx0"
LABEL = "gaoji.sandbox.network-policy"
VERSION = "1"
CHAIN = "GAOJI-SANDBOX"
BLOCKED = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
           "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "198.18.0.0/15",
           "224.0.0.0/4", "240.0.0.0/4")
OPTIONS = {"com.docker.network.bridge.name": BRIDGE,
           "com.docker.network.bridge.enable_icc": "false",
           "com.docker.network.bridge.enable_ip_masquerade": "true"}


def validate_network(value: object) -> None:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError("Sandbox network inspection is incomplete")
    row = value[0]
    if (row.get("Name") != NETWORK or row.get("Driver") != "bridge"
            or row.get("EnableIPv6") is not False or row.get("Internal") is not False
            or (row.get("Labels") or {}).get(LABEL) != VERSION
            or any((row.get("Options") or {}).get(key) != val for key, val in OPTIONS.items())):
        raise ValueError("Sandbox network is not managed by the isolated egress policy")
    subnets = (row.get("IPAM") or {}).get("Config") or []
    if len(subnets) != 1:
        raise ValueError("Sandbox network must have exactly one private IPv4 subnet")
    subnet = ipaddress.ip_network(subnets[0].get("Subnet", ""))
    if subnet.version != 4 or not any(subnet.subnet_of(ipaddress.ip_network(cidr))
                                     for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("Sandbox network must use an RFC1918 subnet")


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=30, check=check)


def provision(*, docker: str = "docker", iptables: str = "iptables", ip6tables: str = "ip6tables") -> None:
    def table(binary, *args, check=True):
        return run([binary, "-w", "10", *args], check=check)

    def ensure_rule(binary, chain, rule):
        if table(binary, "-C", chain, *rule, check=False).returncode:
            table(binary, "-I", chain, "1", *rule)

    existing = run([docker, "network", "inspect", NETWORK], check=False)
    if existing.returncode:
        # Only a genuinely absent name is safe to create; daemon/permission
        # failures must not be mistaken for an empty network list.
        names = run([docker, "network", "ls", "--format", "{{.Name}}"])
        if NETWORK in names.stdout.splitlines():
            raise RuntimeError("Existing sandbox network cannot be inspected")
        if Path("/sys/class/net", BRIDGE).exists():
            raise RuntimeError("Sandbox bridge name is already used by an unmanaged interface")
    else:
        validate_network(json.loads(existing.stdout))
    # Check ownership before changing a host rule. From here, failed setup
    # leaves this owned bridge blocked; other interfaces are never flushed.
    guard = ["-i", BRIDGE, "-m", "comment", "--comment", "gaoji-setup-guard", "-j", "DROP"]
    for binary in (iptables, ip6tables):
        for chain in ("FORWARD", "INPUT"):
            ensure_rule(binary, chain, guard)
    table(iptables, "-S", "DOCKER-USER")
    if existing.returncode:
        run([docker, "network", "create", "--driver", "bridge", "--ipv6=false",
             "--label", f"{LABEL}={VERSION}",
             *[arg for key, val in OPTIONS.items() for arg in ("--opt", f"{key}={val}")], NETWORK])
    validate_network(json.loads(run([docker, "network", "inspect", NETWORK]).stdout))
    if table(iptables, "-S", CHAIN, check=False).returncode:
        table(iptables, "-N", CHAIN)
    table(iptables, "-F", CHAIN)
    for cidr in BLOCKED:
        table(iptables, "-A", CHAIN, "-d", cidr, "-j", "DROP")
    for rule in (("-o", BRIDGE), ("-m", "addrtype", "--dst-type", "LOCAL"),
                 ("-m", "conntrack", "--ctstatus", "DNAT")):
        table(iptables, "-A", CHAIN, *rule, "-j", "DROP")
    table(iptables, "-A", CHAIN, "-j", "RETURN")
    hook = ["-i", BRIDGE, "-j", CHAIN]
    for parent in ("DOCKER-USER", "FORWARD"):
        while not table(iptables, "-C", parent, *hook, check=False).returncode:
            table(iptables, "-D", parent, *hook)
        table(iptables, "-I", parent, "1", *hook)
        table(iptables, "-C", parent, *hook)
    # Keep INPUT blocked (host access) and IPv6 blocked (including link-local).
    table(iptables, "-D", "FORWARD", *guard)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--iptables", default="iptables")
    parser.add_argument("--ip6tables", default="ip6tables")
    parser.add_argument("--lock-file", default="/run/gaoji-sandbox-network.lock")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Sandbox network policy must be installed by root")
    signal.alarm(110)
    lock = Path(args.lock_file)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        provision(docker=args.docker, iptables=args.iptables, ip6tables=args.ip6tables)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
