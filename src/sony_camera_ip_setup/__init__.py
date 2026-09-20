# Copyright (c) 2026 Sascha Ludwig, astrastudio broadcast solutions
# SPDX-License-Identifier: MIT

"""Sony Camera IP Setup — unofficial RM-IP Setup alternative.

Discover and configure Sony SRG/BRC camera IP settings. This package
implements Sony's Camera IP Setting Command, the same UDP protocol used
by the Windows-only RM-IP Setup Tool.

Protocol overview
-----------------
Transport:
    UDP broadcast to 255.255.255.255 and each local subnet broadcast,
    destination port 52380.

Frame layout (from Sony command lists):
    STX (0x02)
    ASCII field
    0xFF          field separator (same byte as the VISCA terminator)
    ...
    ETX (0x03)

Inquiry:
    ENQ:network

Inquiry reply fields:
    MAC, MODEL, SOFTVERSION, IPADR, MASK, GATEWAY, NAME, WRITE
    Some firmware revisions also send INFO:network.

Network setting:
    MAC, IPADR, MASK, GATEWAY, NAME
    The camera applies the change only if MAC matches its own address.

Setting reply:
    ACK:<mac>   success, optional detail text
    NAK:<mac>   rejected

WRITE lock:
    The camera accepts a new IP/name only while WRITE:on.
    Sony turns WRITE off automatically about 20 minutes after power-on.
    Power-cycle the camera to open the window again.

This is not affiliated with Sony. VISCA pan/tilt/zoom control uses port
52381 and is out of scope.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import select
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable

__version__ = "1.0.0"

# Sony Camera IP Setting Command, UDP port (VISCA control uses 52381).
SETUP_PORT = 52380

# Binary frame markers from the Sony command list.
STX = 0x02
ETX = 0x03
SEP = 0xFF

BROADCAST_IP = "255.255.255.255"
DEFAULT_TIMEOUT = 2.0

# Sony allows at most 8 alphanumeric characters or spaces for NAME.
MAX_NAME_LENGTH = 8

# Accept both colon and hyphen notation; output is always AA-BB-CC-DD-EE-FF.
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}$")


class RmIpError(Exception):
    """Raised when a Sony IP-setup operation fails."""


@dataclass
class NetworkInterface:
    """One local IPv4 interface that can send setup broadcasts."""

    name: str
    ip: str
    netmask: str
    broadcast: str


@dataclass
class CameraInfo:
    """Network identity reported by a camera inquiry reply."""

    mac: str
    ip: str
    mask: str
    gateway: str
    name: str
    model: str = ""
    software_version: str = ""
    # True only while Sony still accepts IP/name changes (WRITE:on).
    writable: bool = False
    extra: dict[str, str] = field(default_factory=dict)
    source: str = ""

    def summary(self) -> str:
        """Return a single-line description for CLI output."""
        write_state = "WRITE:on" if self.writable else "WRITE:off"
        parts = [
            f"{self.name or '(no name)'}",
            f"MAC {self.mac}",
            f"IP {self.ip}/{self.mask}",
            f"GW {self.gateway or '-'}",
            write_state,
        ]
        if self.model:
            parts.append(self.model)
        if self.software_version:
            parts.append(f"FW {self.software_version}")
        return "  ".join(parts)


# ---------------------------------------------------------------------------
# Validation and packet codec
# ---------------------------------------------------------------------------


def normalize_mac(mac: str) -> str:
    """Return a MAC address as uppercase AA-BB-CC-DD-EE-FF."""
    cleaned = mac.strip().replace(":", "-")
    if not MAC_RE.match(cleaned):
        raise RmIpError(f"Invalid MAC address: {mac}")
    return cleaned.upper()


def validate_ipv4(value: str, label: str) -> str:
    """Parse *value* as IPv4 and re-raise failures as RmIpError."""
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise RmIpError(f"Invalid {label}: {value}") from exc


def validate_name(name: str) -> str:
    """Enforce Sony's NAME limits: max 8 letters, digits or spaces."""
    if len(name) > MAX_NAME_LENGTH:
        raise RmIpError(
            f"Camera name must be at most {MAX_NAME_LENGTH} characters"
        )
    if not re.fullmatch(r"[A-Za-z0-9 ]*", name):
        raise RmIpError(
            "Camera name may only contain letters, digits and spaces"
        )
    return name


def encode_fields(fields: Iterable[str]) -> bytes:
    """Build a Sony setup frame: STX + field + 0xFF ... + ETX."""
    payload = bytearray([STX])
    for item in fields:
        payload.extend(item.encode("ascii"))
        payload.append(SEP)
    payload.append(ETX)
    return bytes(payload)


def decode_fields(packet: bytes) -> list[str]:
    """Parse a Sony setup frame into ASCII fields.

    STX/ETX are optional so slightly malformed replies still parse.
    Empty fragments between separators are ignored.
    """
    if not packet:
        raise RmIpError("Empty setup packet")
    data = packet
    if data[0] == STX:
        data = data[1:]
    if data and data[-1] == ETX:
        data = data[:-1]
    parts = data.split(bytes([SEP]))
    fields = []
    for part in parts:
        if not part:
            continue
        try:
            fields.append(part.decode("ascii").strip())
        except UnicodeDecodeError as exc:
            raise RmIpError(f"Non-ASCII setup field: {part!r}") from exc
    return fields


def parse_camera_reply(packet: bytes, source: str = "") -> CameraInfo | None:
    """Parse an inquiry reply.

    Returns None for our own broadcast echo (ENQ:network) and for ACK/NAK
    packets so discovery can share the same receive loop as set.
    """
    fields = decode_fields(packet)
    values: dict[str, str] = {}
    for item in fields:
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        values[key.upper()] = value.strip()

    # Inquiry replies always include MAC. ACK/NAK also contain MAC, so
    # those must be filtered out separately.
    if "MAC" not in values:
        return None
    if any(key in values for key in ("ACK", "NAK", "NACK")):
        return None

    write_raw = values.get("WRITE", "").lower()
    return CameraInfo(
        mac=normalize_mac(values["MAC"]),
        # Official field name is IPADR; accept IP as a fallback.
        ip=values.get("IPADR", values.get("IP", "")),
        mask=values.get("MASK", ""),
        gateway=values.get("GATEWAY", ""),
        name=values.get("NAME", ""),
        model=values.get("MODEL", ""),
        software_version=values.get("SOFTVERSION", ""),
        writable=write_raw in {"on", "1", "true"},
        extra=values,
        source=source,
    )


def parse_setting_reply(packet: bytes) -> tuple[str, str, str]:
    """Return ``(status, mac, detail)`` from an ACK/NAK reply.

    Sony documents an optional quoted detail message after the MAC.
    """
    fields = decode_fields(packet)
    status = ""
    mac = ""
    details: list[str] = []
    for item in fields:
        upper = item.upper()
        if upper.startswith("ACK:"):
            status = "ACK"
            mac = normalize_mac(item.split(":", 1)[1].split()[0].strip("\"'"))
        elif upper.startswith("NAK:") or upper.startswith("NACK:"):
            status = "NAK"
            mac = normalize_mac(item.split(":", 1)[1].split()[0].strip("\"'"))
        elif item.strip("\"'"):
            details.append(item.strip("\"'"))
    if not status:
        raise RmIpError(f"Unexpected setting reply: {fields}")
    return status, mac, " ".join(details)


def build_inquiry() -> bytes:
    """Build the ENQ:network discovery packet."""
    return encode_fields(["ENQ:network"])


def build_network_setting(
    mac: str,
    ip: str,
    mask: str,
    gateway: str,
    name: str,
) -> bytes:
    """Build a network-setting packet addressed to *mac*.

    Use GATEWAY:0.0.0.0 when the camera has no gateway, as documented
    by Sony.
    """
    return encode_fields(
        [
            f"MAC:{normalize_mac(mac)}",
            f"IPADR:{validate_ipv4(ip, 'IP address')}",
            f"MASK:{validate_ipv4(mask, 'subnet mask')}",
            f"GATEWAY:{validate_ipv4(gateway, 'gateway')}",
            f"NAME:{validate_name(name)}",
        ]
    )


def hexdump(data: bytes) -> str:
    """Format *data* as space-separated hex bytes for verbose logs."""
    return " ".join(f"{byte:02X}" for byte in data)


# ---------------------------------------------------------------------------
# Local network helpers
# ---------------------------------------------------------------------------


def _parse_netmask(raw: str) -> str | None:
    """Accept dotted masks and the hex form used by macOS ifconfig."""
    raw = raw.strip()
    if raw.startswith("0x"):
        value = int(raw, 16)
        return str(ipaddress.IPv4Address(value))
    try:
        return str(ipaddress.IPv4Address(raw))
    except ipaddress.AddressValueError:
        return None


def list_ipv4_interfaces() -> list[NetworkInterface]:
    """Read local IPv4 interfaces from ``ifconfig`` (macOS and Linux).

    Loopback is skipped. If ifconfig omits the broadcast address it is
    computed from IP and netmask.
    """
    try:
        output = subprocess.check_output(["ifconfig"], text=True)
    except (OSError, subprocess.CalledProcessError):
        return []

    interfaces: list[NetworkInterface] = []
    current = "unknown"
    for line in output.splitlines():
        # Interface names start at column 0, e.g. "en0: flags=...".
        if line and not line[0].isspace() and ":" in line:
            current = line.split(":", 1)[0]
            continue
        match = re.search(
            r"inet\s+(\d+\.\d+\.\d+\.\d+)"
            r"(?:\s+netmask\s+(\S+))?"
            r"(?:\s+broadcast\s+(\d+\.\d+\.\d+\.\d+))?",
            line,
        )
        if not match:
            continue
        ip = match.group(1)
        if ip.startswith("127."):
            continue
        netmask = _parse_netmask(match.group(2) or "255.255.255.0")
        if netmask is None:
            continue
        broadcast = match.group(3)
        if not broadcast:
            network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
            broadcast = str(network.broadcast_address)
        interfaces.append(
            NetworkInterface(
                name=current,
                ip=ip,
                netmask=netmask,
                broadcast=broadcast,
            )
        )
    return interfaces


def create_setup_socket(bind_ip: str = "") -> socket.socket:
    """Create a non-blocking UDP socket bound to the Sony setup port.

    Replies are themselves broadcasts to port 52380, so we must bind
    that port. SO_REUSEADDR/SO_REUSEPORT allow a second local listener
    (for example another copy of this tool) without failing the bind.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            # Some kernels advertise the option but reject the call.
            pass
    sock.bind((bind_ip, SETUP_PORT))
    sock.setblocking(False)
    return sock


def collect_targets(interfaces: list[NetworkInterface]) -> list[str]:
    """Return broadcast destinations for discovery and set packets.

    255.255.255.255 is what Sony documents. Directed subnet broadcasts
    are added as well because some hosts only forward those.
    """
    targets = [BROADCAST_IP]
    for iface in interfaces:
        if iface.broadcast not in targets:
            targets.append(iface.broadcast)
    return targets


def send_broadcasts(
    sock: socket.socket,
    packet: bytes,
    targets: Iterable[str],
    verbose: bool = False,
) -> None:
    """Send *packet* to every broadcast target. Failures are non-fatal."""
    for target in targets:
        try:
            sock.sendto(packet, (target, SETUP_PORT))
            if verbose:
                print(
                    f"TX {target}:{SETUP_PORT}  {hexdump(packet)}",
                    file=sys.stderr,
                )
        except OSError as exc:
            if verbose:
                print(f"TX {target}:{SETUP_PORT} failed: {exc}", file=sys.stderr)


def receive_packets(
    sock: socket.socket,
    timeout: float,
    verbose: bool = False,
) -> list[tuple[bytes, str]]:
    """Collect UDP payloads until *timeout* seconds have elapsed.

    Sony uses UDP, so several devices (or our own echo) may answer.
    """
    packets: list[tuple[bytes, str]] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            continue
        try:
            data, addr = sock.recvfrom(4096)
        except BlockingIOError:
            continue
        source = f"{addr[0]}:{addr[1]}"
        if verbose:
            print(f"RX {source}  {hexdump(data)}", file=sys.stderr)
        packets.append((data, source))
    return packets


# ---------------------------------------------------------------------------
# High-level camera operations
# ---------------------------------------------------------------------------


def discover_cameras(
    timeout: float = DEFAULT_TIMEOUT,
    bind_ip: str = "",
    retries: int = 2,
    verbose: bool = False,
) -> list[CameraInfo]:
    """Broadcast ENQ:network and return every unique camera that answers.

    Sony recommends application-level retransmission because UDP does
    not confirm delivery. Results are keyed by MAC so retries and
    multiple broadcast targets do not create duplicates.
    """
    interfaces = list_ipv4_interfaces()
    if bind_ip:
        interfaces = [iface for iface in interfaces if iface.ip == bind_ip]
        if not interfaces:
            raise RmIpError(f"Bind address {bind_ip} is not a local IPv4 address")

    targets = collect_targets(interfaces)
    found: dict[str, CameraInfo] = {}
    inquiry = build_inquiry()

    with create_setup_socket(bind_ip) as sock:
        for attempt in range(max(1, retries)):
            send_broadcasts(sock, inquiry, targets, verbose=verbose)
            for packet, source in receive_packets(sock, timeout, verbose=verbose):
                try:
                    camera = parse_camera_reply(packet, source=source)
                except RmIpError:
                    continue
                if camera is None:
                    continue
                found[camera.mac] = camera
            if found:
                break
            if verbose and attempt + 1 < retries:
                print("No reply yet, retrying inquiry...", file=sys.stderr)

    return sorted(found.values(), key=lambda cam: (cam.ip, cam.mac))


def apply_network_setting(
    camera: CameraInfo,
    ip: str,
    mask: str | None = None,
    gateway: str | None = None,
    name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    bind_ip: str = "",
    retries: int = 2,
    verbose: bool = False,
) -> tuple[str, str]:
    """Send a network-setting packet and wait for ACK from *camera*.

    Omitted mask, gateway or name keep the values from the last inquiry.
    """
    if not camera.writable:
        raise RmIpError(
            "Camera WRITE flag is off. Power-cycle the camera and "
            "change the IP within 20 minutes."
        )

    new_ip = validate_ipv4(ip, "IP address")
    new_mask = validate_ipv4(mask or camera.mask or "255.255.255.0", "subnet mask")
    new_gateway = validate_ipv4(gateway or camera.gateway or "0.0.0.0", "gateway")
    new_name = validate_name(name if name is not None else (camera.name or "CAM1"))
    packet = build_network_setting(camera.mac, new_ip, new_mask, new_gateway, new_name)

    interfaces = list_ipv4_interfaces()
    if bind_ip:
        interfaces = [iface for iface in interfaces if iface.ip == bind_ip]
    targets = collect_targets(interfaces)

    last_error = "No ACK received"
    with create_setup_socket(bind_ip) as sock:
        for _ in range(max(1, retries)):
            send_broadcasts(sock, packet, targets, verbose=verbose)
            for reply, _source in receive_packets(sock, timeout, verbose=verbose):
                try:
                    status, mac, detail = parse_setting_reply(reply)
                except RmIpError:
                    # Ignore inquiry echoes and unrelated LAN traffic.
                    continue
                if mac != camera.mac:
                    continue
                if status != "ACK":
                    raise RmIpError(
                        f"Camera rejected the setting ({status}"
                        f"{': ' + detail if detail else ''})"
                    )
                return status, detail
            last_error = "No ACK received"
    raise RmIpError(last_error)


def select_camera(
    cameras: list[CameraInfo],
    mac: str | None,
    current_ip: str | None,
    name: str | None,
) -> CameraInfo:
    """Pick exactly one camera from *cameras* using the given filters."""
    matches = cameras
    if mac:
        wanted = normalize_mac(mac)
        matches = [cam for cam in matches if cam.mac == wanted]
    if current_ip:
        wanted_ip = validate_ipv4(current_ip, "current IP")
        matches = [cam for cam in matches if cam.ip == wanted_ip]
    if name is not None:
        matches = [cam for cam in matches if cam.name == name]

    if not matches:
        raise RmIpError("No matching camera found. Run discover first.")
    if len(matches) > 1:
        listing = "\n".join(f"  {cam.summary()}" for cam in matches)
        raise RmIpError(
            "Several cameras match. Specify --mac or --current-ip.\n" + listing
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------


def print_interfaces() -> None:
    """Print local IPv4 interfaces so the user can check the subnet."""
    interfaces = list_ipv4_interfaces()
    if not interfaces:
        print("No non-loopback IPv4 interfaces found.")
        return
    print("Local IPv4 interfaces:")
    for iface in interfaces:
        print(
            f"  {iface.name:8}  {iface.ip:15}  "
            f"mask {iface.netmask:15}  broadcast {iface.broadcast}"
        )


def hint_same_subnet() -> None:
    """Remind the user that setup needs the same Ethernet segment, not subnet."""
    print(
        "\nDiscovery uses Ethernet broadcast. The computer and camera must "
        "share the same switch or VLAN; the IP subnet does not have to match.\n"
        "Factory default is 192.168.0.100/24. If nothing answers, check LAN "
        "mode, power-cycle the camera (WRITE is only on for 20 minutes), "
        "or add a temporary alias:\n"
        "  sudo ifconfig en0 alias 192.168.0.10 netmask 255.255.255.0"
    )


def command_discover(args: argparse.Namespace) -> int:
    """CLI handler for ``sony-camera-ip-setup discover``."""
    print_interfaces()
    print()
    try:
        cameras = discover_cameras(
            timeout=args.timeout,
            bind_ip=args.bind or "",
            retries=args.retries,
            verbose=args.verbose,
        )
    except OSError as exc:
        print(f"Could not open UDP port {SETUP_PORT}: {exc}", file=sys.stderr)
        return 1
    except RmIpError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not cameras:
        print("No Sony camera answered on UDP 52380.")
        hint_same_subnet()
        return 2

    print(f"Found {len(cameras)} device(s):")
    for camera in cameras:
        print(f"  {camera.summary()}")
        if not camera.writable:
            print(
                "    Note: WRITE is off, so the IP cannot be changed "
                "until you power-cycle the camera."
            )
    return 0


def command_set(args: argparse.Namespace) -> int:
    """CLI handler for ``sony-camera-ip-setup set``.

    Discovers first, applies the change, then discovers again so the
    user can confirm the new values. After an IP change the follow-up
    inquiry may fail if this host is not yet on the new subnet.
    """
    try:
        cameras = discover_cameras(
            timeout=args.timeout,
            bind_ip=args.bind or "",
            retries=args.retries,
            verbose=args.verbose,
        )
        camera = select_camera(cameras, args.mac, args.current_ip, args.match_name)
        print(f"Current: {camera.summary()}")
        if args.dry_run:
            packet = build_network_setting(
                camera.mac,
                args.ip,
                args.mask or camera.mask or "255.255.255.0",
                args.gateway or camera.gateway or "0.0.0.0",
                args.name if args.name is not None else (camera.name or "CAM1"),
            )
            print(f"Would send: {hexdump(packet)}")
            return 0

        status, detail = apply_network_setting(
            camera,
            ip=args.ip,
            mask=args.mask,
            gateway=args.gateway,
            name=args.name,
            timeout=args.timeout,
            bind_ip=args.bind or "",
            retries=args.retries,
            verbose=args.verbose,
        )
        extra = f" ({detail})" if detail else ""
        print(f"{status} from {camera.mac}{extra}")

        # Give the camera a moment to apply the new address before probing.
        time.sleep(0.5)
        refreshed = discover_cameras(
            timeout=args.timeout,
            bind_ip=args.bind or "",
            retries=args.retries,
            verbose=args.verbose,
        )
        updated = next((cam for cam in refreshed if cam.mac == camera.mac), None)
        if updated:
            print(f"Now:     {updated.summary()}")
        else:
            print(
                "Setting was acknowledged, but the camera did not answer "
                "the follow-up inquiry. Check that this Mac is in the new subnet."
            )
        return 0
    except (RmIpError, OSError) as exc:
        print(exc, file=sys.stderr)
        if isinstance(exc, RmIpError) and "WRITE" in str(exc):
            hint_same_subnet()
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Shared options live on a parent parser so both
    ``sony-camera-ip-setup -v discover`` and
    ``sony-camera-ip-setup discover -v`` work.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Seconds to wait for UDP replies (default: 2.0)",
    )
    common.add_argument(
        "--retries",
        type=int,
        default=2,
        help="How often to resend UDP packets (default: 2)",
    )
    common.add_argument(
        "--bind",
        help="Local IPv4 address to bind, e.g. 192.168.0.10",
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print raw TX/RX packets",
    )

    parser = argparse.ArgumentParser(
        description=(
            "Sony Camera IP Setup: find SRG/BRC cameras and set their "
            "IP address (unofficial RM-IP Setup alternative)."
        ),
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser(
        "discover",
        parents=[common],
        help="List cameras on the LAN",
    )
    discover.set_defaults(func=command_discover)

    configure = sub.add_parser(
        "set",
        parents=[common],
        help="Change a camera IP address",
    )
    configure.add_argument("--ip", required=True, help="New IPv4 address")
    configure.add_argument("--mask", help="New subnet mask (default: keep current)")
    configure.add_argument(
        "--gateway",
        help="New gateway (default: keep current, or 0.0.0.0)",
    )
    configure.add_argument("--name", help="New camera name, max 8 characters")
    configure.add_argument("--mac", help="Target camera MAC, e.g. AA-BB-CC-DD-EE-FF")
    configure.add_argument("--current-ip", help="Target camera by its current IP")
    configure.add_argument("--match-name", help="Target camera by its current name")
    configure.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the packet but do not send the change",
    )
    configure.set_defaults(func=command_set)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the selected subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
