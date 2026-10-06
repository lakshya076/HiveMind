import json
import os
import shutil
import socket
import subprocess
from typing import List, Optional
from .scanner import DiscoveredDevice


class TailscaleScanner:
    """Discovers peer devices connected via Tailscale mesh network."""

    @staticmethod
    def get_tailscale_binary() -> Optional[str]:
        """Locates the Tailscale CLI binary across Windows, Linux, and macOS."""
        # 1. Check system PATH
        binary = shutil.which("tailscale")
        if binary:
            return binary

        # 2. Check standard Windows installation paths
        win_paths = [
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Tailscale\tailscale.exe"),
        ]
        for path in win_paths:
            if os.path.isfile(path):
                return path

        # 3. Check standard Linux/macOS paths
        unix_paths = [
            "/usr/bin/tailscale",
            "/usr/local/bin/tailscale",
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        ]
        for path in unix_paths:
            if os.path.isfile(path):
                return path

        return None

    @classmethod
    def is_available(cls) -> bool:
        """Returns True if the Tailscale CLI is installed and responsive."""
        binary = cls.get_tailscale_binary()
        if not binary:
            return False
        try:
            res = subprocess.run(
                [binary, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            return res.returncode == 0
        except Exception:
            return False

    @classmethod
    def discover_peers(cls, probe_ssh: bool = True) -> List[DiscoveredDevice]:
        """Queries the local Tailscale daemon and returns all active Tailnet peers."""
        binary = cls.get_tailscale_binary()
        if not binary:
            return []

        try:
            res = subprocess.run(
                [binary, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=4.0,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return []
            data = json.loads(res.stdout)
        except Exception:
            return []

        devices: List[DiscoveredDevice] = []

        # 1. Add Self (Master node)
        self_data = data.get("Self", {})
        self_ips = self_data.get("TailscaleIPs", [])
        self_v4 = next((ip for ip in self_ips if ":" not in ip), None)
        if self_v4:
            devices.append(
                DiscoveredDevice(
                    ip=self_v4,
                    hostname=self_data.get("HostName", "Master"),
                    ssh_open=False,
                    is_local_host=True,
                    os_type=self_data.get("OS", "unknown").lower(),
                )
            )

        # 2. Add Remote Peers
        peers = data.get("Peer", {})
        for peer_key, peer_info in peers.items():
            if not peer_info.get("Online", False):
                continue

            peer_ips = peer_info.get("TailscaleIPs", [])
            peer_v4 = next((ip for ip in peer_ips if ":" not in ip), None)
            if not peer_v4:
                continue

            hostname = peer_info.get("HostName", "Unknown")
            os_type = peer_info.get("OS", "unknown").lower()

            # Check if Port 22 is open on the peer
            ssh_open = False
            if probe_ssh:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1.5)
                    ssh_open = s.connect_ex((peer_v4, 22)) == 0
                    s.close()
                except Exception:
                    ssh_open = False

            devices.append(
                DiscoveredDevice(
                    ip=peer_v4,
                    hostname=hostname,
                    ssh_open=ssh_open,
                    is_local_host=False,
                    os_type=os_type,
                )
            )

        return devices
