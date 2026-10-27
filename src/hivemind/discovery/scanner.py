import concurrent.futures
from dataclasses import dataclass
import ipaddress
import re
import socket
import subprocess
from typing import Callable, List, Optional
import psutil

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_MAC_RE = re.compile(r"\b([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})\b")


@dataclass
class DiscoveredDevice:
    ip: str
    hostname: str
    ssh_open: bool
    is_local_host: bool = False
    mac_address: Optional[str] = None
    response_time_ms: Optional[float] = None
    os_type: Optional[str] = None


def parse_arp_output(
    output: str,
    network: Optional[ipaddress.IPv4Network] = None,
) -> dict[str, str]:
    """Parse Windows, Linux, or macOS `arp -a` output and filter out broadcast/multicast entries."""
    arp_cache: dict[str, str] = {}
    for line in output.splitlines():
        lower = line.lower()
        if "incomplete" in lower or "invalid" in lower:
            continue
        ip_match = _IPV4_RE.search(line)
        mac_match = _MAC_RE.search(line)
        if not ip_match or not mac_match:
            continue
        ip_str = ip_match.group(1)
        mac_str = mac_match.group(1).lower().replace("-", ":")
        if mac_str == "ff:ff:ff:ff:ff:ff" or mac_str == "00:00:00:00:00:00":
            continue
        try:
            ip_obj = ipaddress.IPv4Address(ip_str)
        except ValueError:
            continue
        if (
            ip_obj.is_multicast
            or ip_obj.is_loopback
            or ip_obj.is_unspecified
            or ip_obj.is_link_local
            or ip_str == "255.255.255.255"
            or ip_str.endswith(".255")
        ):
            continue
        if network is not None and (
            ip_obj == network.network_address or ip_obj == network.broadcast_address
        ):
            continue
        arp_cache[ip_str] = mac_str
    return arp_cache


class NetworkScanner:
    """Discovers active machines on the local Wi-Fi / Ethernet subnet."""

    @staticmethod
    def get_local_interfaces() -> List[dict]:
        """Finds all non-loopback, non-link-local IPv4 network interfaces."""
        interfaces = []
        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    netmask = addr.netmask
                    if ip.startswith("127.") or ip.startswith("169.254."):
                        continue
                    try:
                        network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                        interfaces.append({
                            "name": name,
                            "ip": ip,
                            "netmask": netmask,
                            "network": network,
                            "cidr": str(network),
                        })
                    except Exception:
                        pass
        return interfaces

    @staticmethod
    def get_arp_table(network: Optional[ipaddress.IPv4Network] = None) -> dict[str, str]:
        """Parses the OS ARP table to resolve MAC addresses and active IPs."""
        try:
            res = subprocess.run(
                ["arp", "-a"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            return parse_arp_output(res.stdout or "", network=network)
        except Exception:
            return {}

    @staticmethod
    def check_tcp_port(ip: str, port: int = 7422, timeout: float = 0.6) -> bool:
        """Probes a TCP port on `ip` without mutating process-global socket timeout."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                return s.connect_ex((ip, port)) == 0
        except Exception:
            return False

    @classmethod
    def check_ssh_port(cls, ip: str, port: int = 22, timeout: float = 2.5) -> bool:
        """Backward-compatible wrapper around check_tcp_port."""
        return cls.check_tcp_port(ip, port=port, timeout=timeout)

    @staticmethod
    def resolve_hostname(ip: str, timeout: float = 0.5) -> str:
        """Attempts to resolve the hostname via reverse DNS without calling socket.setdefaulttimeout."""
        try:
            name, _, _ = socket.gethostbyaddr(ip)
            return name
        except Exception:
            return "Unknown"

    @classmethod
    def probe_host(
        cls,
        ip: str,
        local_ips: List[str],
        arp_table: dict,
        daemon_port: int = 7422,
    ) -> Optional[DiscoveredDevice]:
        """Probes a single host for reachability on the worker daemon port or ARP presence."""
        is_local = ip in local_ips
        daemon_open = False if is_local else cls.check_tcp_port(ip, port=daemon_port, timeout=0.6)
        is_in_arp = ip in arp_table

        if is_local or daemon_open or is_in_arp:
            hostname = socket.gethostname() if is_local else cls.resolve_hostname(ip)
            mac = arp_table.get(ip)
            return DiscoveredDevice(
                ip=ip,
                hostname=hostname,
                ssh_open=daemon_open,
                is_local_host=is_local,
                mac_address=mac,
            )
        return None

    @classmethod
    def _candidate_hosts(
        cls,
        network: ipaddress.IPv4Network,
        arp_table: dict[str, str],
        interface_ip: Optional[str] = None,
        max_hosts: int = 1024,
    ) -> List[str]:
        """Return candidate host IPs to probe, capping oversized networks (e.g. /16) to a /24 + ARP entries."""
        if network.num_addresses <= max_hosts:
            return [str(ip) for ip in network.hosts()]

        anchor = interface_ip or str(next(network.hosts(), network.network_address))
        scoped = ipaddress.IPv4Network(f"{anchor}/24", strict=False)
        hosts = [str(ip) for ip in scoped.hosts() if ip in network]
        seen = set(hosts)
        for arp_ip in arp_table:
            try:
                addr = ipaddress.IPv4Address(arp_ip)
            except ValueError:
                continue
            if addr in network and arp_ip not in seen:
                hosts.append(arp_ip)
                seen.add(arp_ip)
        return hosts

    @classmethod
    def scan_subnet(
        cls,
        network: ipaddress.IPv4Network,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_threads: int = 64,
        daemon_port: int = 7422,
        interface_ip: Optional[str] = None,
        max_hosts: int = 1024,
    ) -> List[DiscoveredDevice]:
        """Concurrently scans host IPs in the given subnet."""
        local_ips = [iface["ip"] for iface in cls.get_local_interfaces()]
        if interface_ip and interface_ip not in local_ips:
            local_ips.append(interface_ip)
        arp_table = cls.get_arp_table(network=network)
        hosts = cls._candidate_hosts(
            network,
            arp_table,
            interface_ip=interface_ip,
            max_hosts=max_hosts,
        )
        total_hosts = max(1, len(hosts))

        discovered: List[DiscoveredDevice] = []
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
            future_to_ip = {
                executor.submit(cls.probe_host, ip, local_ips, arp_table, daemon_port): ip
                for ip in hosts
            }
            for future in concurrent.futures.as_completed(future_to_ip):
                completed += 1
                if progress_callback:
                    progress_callback(completed, total_hosts)
                try:
                    result = future.result()
                    if result:
                        discovered.append(result)
                except Exception:
                    pass

        # Sort: Local host first, then devices with open worker port, then IP order
        discovered.sort(key=lambda d: (not d.is_local_host, not d.ssh_open, ipaddress.IPv4Address(d.ip)))
        return discovered
