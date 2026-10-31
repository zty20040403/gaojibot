"""Root-owned KVM egress policy: VMs reach the public internet and the relay ports only.

QEMU user networking (slirp) opens ordinary host sockets from the qemu process,
so guest traffic to 10.0.2.2 lands on the host's 127.0.0.1 and everything else
uses the host's routes, including the tailnet. The libvirt session daemon and
its qemu children run in one systemd unit; this table filters that unit's
cgroup. The cgroup is resolved when the table is loaded, so it is reinstalled
on every start of the unit and after every nftables (re)load.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import subprocess

TABLE = "gaoji_vm"
BLOCKED4 = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
            "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
            "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4")
BLOCKED6 = ("::/127", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48",
            "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")


def nameservers(resolv_conf: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """slirp forwards guest DNS (10.0.2.3) to these servers, e.g. 127.0.0.53."""
    found = []
    for line in resolv_conf.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            found.append(ipaddress.ip_address(fields[1].split("%")[0]))
    return found


def render(cgroup: str, ports: list[int], dns: list = ()) -> str:
    path = cgroup.strip("/")
    if not path or any(char in path for char in '"\\\n') or "//" in path:
        raise ValueError(f"Invalid VM cgroup: {cgroup!r}")
    if not ports or any(type(port) is not int or not 0 < port < 65536 for port in ports):
        raise ValueError("VM relay ports must be TCP port numbers")
    level = len(path.split("/"))
    relay = ", ".join(map(str, sorted(set(ports))))
    reject = "reject with icmpx admin-prohibited"
    blocked4, blocked6 = ", ".join(BLOCKED4), ", ".join(BLOCKED6)
    resolvers = ""
    for family, version in (("ip", 4), ("ip6", 6)):
        servers = sorted({str(address) for address in dns if address.version == version})
        if servers:
            resolvers += (f"    {family} daddr {{ {', '.join(servers)} }}"
                          " meta l4proto { tcp, udp } th dport 53 accept\n")
    return f"""table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
  chain output {{
    type filter hook output priority filter; policy accept;
    socket cgroupv2 level {level} "{path}" jump guest
  }}
  chain guest {{
    ip daddr 127.0.0.1 tcp dport {{ {relay} }} accept
{resolvers}    fib daddr type local {reject}
    ip daddr {{ {blocked4} }} {reject}
    ip6 daddr {{ {blocked6} }} {reject}
  }}
}}
"""


def run(command: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(command, input=input_text, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stderr.strip()}")
    return result.stdout


def provision(*, unit: str, ports: list[int], if_running: bool, nft: str = "nft",
              systemctl: str = "systemctl", resolv_conf: str = "/etc/resolv.conf") -> None:
    cgroup = run([systemctl, "show", "--property=ControlGroup", "--value", unit]).strip()
    if not cgroup:
        if if_running:
            return
        raise RuntimeError(f"{unit} has no cgroup; refusing to start VMs unfiltered")
    try:
        with open(resolv_conf, encoding="utf-8") as stream:
            dns = nameservers(stream.read())
        run([nft, "-f", "-"], input_text=render(cgroup, ports, dns))
    except Exception:
        if if_running:
            # Rules were flushed and cannot be restored: stop the VMs rather than leave them open.
            subprocess.run([systemctl, "stop", "--no-block", unit], timeout=30, check=False)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nft", default="nft")
    parser.add_argument("--systemctl", default="systemctl")
    parser.add_argument("--resolv-conf", default="/etc/resolv.conf")
    parser.add_argument("--unit", required=True)
    parser.add_argument("--host-port", type=int, action="append", default=[], dest="ports")
    parser.add_argument("--if-running", action="store_true",
                        help="Reinstall only when the unit is running (nftables reload hook).")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("VM network policy must be installed by root")
    provision(unit=args.unit, ports=args.ports, if_running=args.if_running,
              nft=args.nft, systemctl=args.systemctl, resolv_conf=args.resolv_conf)


if __name__ == "__main__":
    main()
