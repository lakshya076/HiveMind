import socket
import subprocess
import ipaddress
import concurrent.futures
from dataclasses import dataclass
from typing import List, Optional, Callable
import psutil


@dataclass
class DiscoveredDevice:
    ip: str
    hostname: str
    ssh_open: bool
    is_local_host: bool = False
    mac_address: Optional[str] = None
    response_time_ms: Optional[float] = None
    os_type: Optional[str] = None


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
    def get_arp_table() -> dict[str, str]:
        """Parses the OS ARP table to resolve MAC addresses and active IPs."""
        arp_cache = {}
        try:
            output = subprocess.check_output("arp -a", shell=True, text=True, stderr=subprocess.DEVNULL)
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    ip_candidate = parts[0].strip()
                    mac_candidate = parts[1].strip()
                    try:
                        ipaddress.IPv4Address(ip_candidate)
                        arp_cache[ip_candidate] = mac_candidate
                    except ValueError:
                        pass
        except Exception:
            pass
        return arp_cache

    @staticmethod
    def check_ssh_port(ip: str, port: int = 22, timeout: float = 2.5) -> bool:
        """Probes TCP port 22 to check if SSH server is responding."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                res = s.connect_ex((ip, port))
                return res == 0
        except Exception:
            return False

    @staticmethod
    def resolve_hostname(ip: str, timeout: float = 0.5) -> str:
        """Attempts to resolve the hostname via reverse DNS or mDNS."""
        try:
            socket.setdefaulttimeout(timeout)
            name, _, _ = socket.gethostbyaddr(ip)
            return name
        except Exception:
            return "Unknown"

    @classmethod
    def probe_host(cls, ip: str, local_ips: List[str], arp_table: dict) -> Optional[DiscoveredDevice]:
        """Probes a single host for reachability and SSH service."""
        is_local = ip in local_ips
        ssh_open = cls.check_ssh_port(ip, port=22, timeout=0.6)
        
        # If SSH is open or the host is listed in the dynamic ARP cache or it's localhost
        is_in_arp = ip in arp_table
        if is_local or ssh_open or is_in_arp:
            hostname = socket.gethostname() if is_local else cls.resolve_hostname(ip)
            mac = arp_table.get(ip)
            return DiscoveredDevice(
                ip=ip,
                hostname=hostname,
                ssh_open=ssh_open,
                is_local_host=is_local,
                mac_address=mac,
            )
        return None

    @classmethod
    def scan_subnet(
        cls,
        network: ipaddress.IPv4Network,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_threads: int = 64,
    ) -> List[DiscoveredDevice]:
        """Concurrently scans all host IPs in the given subnet."""
        local_ips = [iface["ip"] for iface in cls.get_local_interfaces()]
        arp_table = cls.get_arp_table()
        hosts = [str(ip) for ip in network.hosts()]
        total_hosts = len(hosts)
        
        discovered: List[DiscoveredDevice] = []
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
            future_to_ip = {
                executor.submit(cls.probe_host, ip, local_ips, arp_table): ip
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

        # Sort: Local host first, then devices with open SSH, then IP order
        discovered.sort(key=lambda d: (not d.is_local_host, not d.ssh_open, ipaddress.IPv4Address(d.ip)))
        return discovered
