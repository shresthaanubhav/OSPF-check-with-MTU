#!/usr/bin/env python3
"""OSPF monitoring via Netmiko — neighbors, routes, and state checks."""

import re
import sys
from pathlib import Path

import yaml
from netmiko import ConnectHandler

from concurrent.futures import ThreadPoolExecutor, as_completed
import io
from contextlib import redirect_stdout

DEVICES_FILE = Path(__file__).parent / "devices.yaml"
VALID_NEIGHBOR_STATES = {"FULL/DR", "FULL/BDR", "FULL/DROTHER"}

NEIGHBOR_CMD = "show ip ospf neighbor"
ROUTE_CMD = "show ip route ospf"
OSPF_INTERFACE_CMD = "show ip ospf interface"
INTERFACES_CMD = "show interfaces"
INTERFACE_CONFIG_CMD = "show running-config | section ^interface"

# Neighbor ID  Pri  State  Dead Time  Address  Interface
NEIGHBOR_LINE = re.compile(
    r"^(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$"
)
OSPF_INTERFACE_LINE = re.compile(r"^(\S+) is (up|down)")
INTERFACE_MTU_LINE = re.compile(r"^\s+MTU (\d+) bytes")
INTERFACE_CONFIG_HEADER = re.compile(r"^interface (\S+)")


def load_devices(path: Path) -> list[dict]:
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("devices", [])


def device_params(entry: dict) -> dict:
    return {
        "host": entry["hostname"],
        "username": entry["username"],
        "password": entry["password"],
        "secret": entry.get("secret", ""),
        "device_type": entry.get("device_type", "cisco_ios"),
    }


def parse_ospf_neighbors(output: str) -> list[dict]:
    neighbors = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Neighbor ID"):
            continue
        match = NEIGHBOR_LINE.match(line)
        if not match:
            continue
        neighbor_id, pri, state, dead_time, address, interface = match.groups()
        neighbors.append(
            {
                "neighbor_id": neighbor_id,
                "pri": pri,
                "state": state,
                "dead_time": dead_time,
                "address": address,
                "interface": interface.strip(),
            }
        )
    return neighbors


def check_neighbor_state(neighbor: dict) -> str | None:
    state = neighbor["state"]
    if state not in VALID_NEIGHBOR_STATES:
        return (
            f"ALERT: OSPF neighbor {neighbor['neighbor_id']} on "
            f"{neighbor['interface']} is in state {state!r} "
            f"(expected FULL/DR or FULL/BDR)"
        )
    return None


def parse_ospf_interface_names(output: str) -> list[str]:
    interfaces = []
    for line in output.splitlines():
        match = OSPF_INTERFACE_LINE.match(line.strip())
        if match:
            interfaces.append(match.group(1))
    return interfaces


def parse_ospf_interface_blocks(output: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    current_iface = None
    current_lines: list[str] = []

    for line in output.splitlines():
        match = OSPF_INTERFACE_LINE.match(line.strip())
        if match:
            if current_iface:
                blocks[current_iface] = "\n".join(current_lines)
            current_iface = match.group(1)
            current_lines = [line]
        elif current_iface:
            current_lines.append(line)

    if current_iface:
        blocks[current_iface] = "\n".join(current_lines)
    return blocks


def parse_interface_mtus(output: str) -> dict[str, int]:
    mtus: dict[str, int] = {}
    current_iface = None

    for line in output.splitlines():
        iface_match = OSPF_INTERFACE_LINE.match(line.strip())
        if iface_match:
            current_iface = iface_match.group(1)
            continue
        if current_iface:
            mtu_match = INTERFACE_MTU_LINE.match(line)
            if mtu_match:
                mtus[current_iface] = int(mtu_match.group(1))
                current_iface = None
    return mtus


def parse_mtu_ignore_config(output: str) -> dict[str, bool]:
    mtu_ignore: dict[str, bool] = {}
    current_iface = None

    for line in output.splitlines():
        header = INTERFACE_CONFIG_HEADER.match(line.strip())
        if header:
            current_iface = header.group(1)
            mtu_ignore.setdefault(current_iface, False)
            continue
        if current_iface and line.strip() == "ip ospf mtu-ignore":
            mtu_ignore[current_iface] = True

    return mtu_ignore


def ospf_mtu_from_block(block: str, interface_mtu: int | None) -> int | None:
    for line in block.splitlines():
        match = re.search(r"MTU (\d+)", line)
        if match:
            return int(match.group(1))
    return interface_mtu


def check_mtu_ignore(iface: str, enabled: bool) -> str | None:
    if enabled:
        return (
            f"NOTE: {iface} has 'ip ospf mtu-ignore' — OSPF will not "
            f"validate MTU during adjacency formation"
        )
    return None


def report_ospf_mtu(
    ospf_interfaces: list[str],
    ospf_blocks: dict[str, str],
    interface_mtus: dict[str, int],
    mtu_ignore: dict[str, bool],
) -> None:
    print(f"\n--- OSPF interface / MTU ---\n")
    if not ospf_interfaces:
        print("No OSPF-enabled interfaces found.")
        return

    for iface in ospf_interfaces:
        iface_mtu = interface_mtus.get(iface)
        ospf_mtu = ospf_mtu_from_block(ospf_blocks.get(iface, ""), iface_mtu)
        ignore = mtu_ignore.get(iface, False)

        print(f"{iface}:")
        print(f"  Interface MTU: {iface_mtu if iface_mtu is not None else 'unknown'}")
        print(f"  OSPF MTU:      {ospf_mtu if ospf_mtu is not None else 'unknown'}")
        print(
            f"  ip ospf mtu-ignore: {'configured' if ignore else 'not configured'}"
        )

        note = check_mtu_ignore(iface, ignore)
        if note:
            print(f"  >>> {note}")
        print()


def monitor_device(entry: dict) -> None:
    name = entry.get("name", entry["hostname"])
    print(f"\n{'=' * 60}")
    print(f"Device: {name} ({entry['hostname']})")
    print("=" * 60)

    try:
        with ConnectHandler(**device_params(entry)) as conn:
            conn.enable()

            print(f"\n--- {NEIGHBOR_CMD} ---\n")
            neighbor_output = conn.send_command(NEIGHBOR_CMD)
            print(neighbor_output)

            neighbors = parse_ospf_neighbors(neighbor_output)
            if not neighbors:
                print("\nNo OSPF neighbors found.")
            else:
                print(f"\n--- Parsed neighbors ({len(neighbors)}) ---")
                for n in neighbors:
                    print(
                        f"  ID: {n['neighbor_id']}  |  State: {n['state']}  |  "
                        f"Address: {n['address']}  |  Dead Time: {n['dead_time']}  |  "
                        f"Pri: {n['pri']}  |  Interface: {n['interface']}"
                    )
                    alert = check_neighbor_state(n)
                    if alert:
                        print(f"  >>> {alert}")

            print(f"\n--- {ROUTE_CMD} ---\n")
            route_output = conn.send_command(ROUTE_CMD)
            print(route_output)

            print(f"\n--- {OSPF_INTERFACE_CMD} ---\n")
            ospf_if_output = conn.send_command(OSPF_INTERFACE_CMD)
            print(ospf_if_output)

            ospf_interfaces = parse_ospf_interface_names(ospf_if_output)
            ospf_blocks = parse_ospf_interface_blocks(ospf_if_output)
            interface_mtus = parse_interface_mtus(
                conn.send_command(INTERFACES_CMD, read_timeout=60)
            )
            mtu_ignore = parse_mtu_ignore_config(
                conn.send_command(INTERFACE_CONFIG_CMD, read_timeout=60)
            )
            report_ospf_mtu(
                ospf_interfaces, ospf_blocks, interface_mtus, mtu_ignore
            )

    except Exception as exc:
        print(f"ERROR: Failed to connect to {name}: {exc}", file=sys.stderr)


def main() -> int:
    if not DEVICES_FILE.exists():
        print(f"ERROR: {DEVICES_FILE} not found", file=sys.stderr)
        return 1

    devices = load_devices(DEVICES_FILE)
    if not devices:
        print("No devices defined in devices.yaml", file=sys.stderr)
        return 1

    print(f"Monitoring OSPF on {len(devices)} device(s)...")

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(monitor_device, entry): entry for entry in devices}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                future.result()
            except Exception as exc:
                name = entry.get("name", entry["hostname"])
                print(f"ERROR: {name} raised exception: {exc}", file=sys.stderr)

    print(f"\n{'=' * 60}")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
