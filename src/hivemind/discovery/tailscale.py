import json
import os
import shutil
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
    def status_json(cls) -> Optional[dict]:
        """Parsed ``tailscale status --json``, or None if the CLI is missing or fails."""
        binary = cls.get_tailscale_binary()
        if not binary:
            return None
        try:
            res = subprocess.run(
                [binary, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=4.0,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return None
            data = json.loads(res.stdout)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def backend_state(cls) -> Optional[str]:
        """Tailscale's BackendState ("Running", "Stopped", "NeedsLogin", "NoState", ...), or None."""
        data = cls.status_json()
        return data.get("BackendState") if data else None

    @classmethod
    def is_available(cls) -> bool:
        """True only if Tailscale is installed AND connected; a stopped or logged-out client has no peers."""
        return cls.backend_state() == "Running"

    @classmethod
    def discover_peers(cls) -> List[DiscoveredDevice]:
        """Queries the local Tailscale daemon and returns all active Tailnet peers."""
        data = cls.status_json()
        if not data:
            return []

        devices: List[DiscoveredDevice] = []

        # Fields are JSON null (not missing) when Tailscale is stopped or logged out.
        # 1. Add Self (Master node)
        self_data = data.get("Self") or {}
        self_ips = self_data.get("TailscaleIPs") or []
        self_v4 = next((ip for ip in self_ips if ":" not in ip), None)
        if self_v4:
            devices.append(
                DiscoveredDevice(
                    ip=self_v4,
                    hostname=self_data.get("HostName") or "Master",
                    daemon_open=False,
                    is_local_host=True,
                    os_type=(self_data.get("OS") or "unknown").lower(),
                )
            )

        # 2. Add Remote Peers
        peers = data.get("Peer") or {}
        for peer_info in peers.values():
            if not peer_info.get("Online", False):
                continue

            peer_ips = peer_info.get("TailscaleIPs") or []
            peer_v4 = next((ip for ip in peer_ips if ":" not in ip), None)
            if not peer_v4:
                continue

            hostname = peer_info.get("HostName") or "Unknown"
            os_type = (peer_info.get("OS") or "unknown").lower()

            devices.append(
                DiscoveredDevice(
                    ip=peer_v4,
                    hostname=hostname,
                    daemon_open=False,  # discover probes the daemon itself
                    is_local_host=False,
                    os_type=os_type,
                )
            )

        return devices
