import subprocess
from typing import Any, Dict, List, Optional, Tuple

import httpx

from hivemind.config.cluster import get_fleet_key


WORKER_SERVICE_NAME = "HiveMind Worker Daemon"


class SystemProber:
    """Probes worker machines via HTTP daemon (or fallback) to detect hardware and tool capabilities."""

    @classmethod
    def probe_presence(
        cls, host: str, port: int = 7422, timeout: float = 1.8
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Check whether a hivemind-worker answers at http://<host>:<port>/ WITHOUT credentials.
        Used during discovery so the fleet key is never sent to unselected devices.
        Returns (info_dict, error_message).
        """
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(f"http://{host}:{port}/")
        except httpx.ConnectError:
            return None, f"Daemon not reachable on port {port} (install/start hivemind-worker)"
        except httpx.TimeoutException:
            return None, f"Timed out connecting to port {port}"
        except Exception as exc:
            return None, f"Probe error: {exc}"

        if resp.status_code == 403:
            return None, "Worker refused this machine's address (check allowed_networks on the worker)"
        if resp.status_code != 200:
            return None, f"Not a HiveMind worker (HTTP {resp.status_code})"
        try:
            data = resp.json()
        except ValueError:
            return None, "Not a HiveMind worker (non-JSON response)"
        if not isinstance(data, dict) or data.get("name") != WORKER_SERVICE_NAME:
            return None, "Not a HiveMind worker"
        return data, None

    @classmethod
    def probe_daemon(
        cls, host: str, port: int = 7422, timeout: float = 2.5
    ) -> Tuple[Optional[Dict[str, Any]], List[str], Optional[str]]:
        """
        Probe worker node HTTP daemon at http://<host>:<port>/health.
        Returns (health_data_dict, tags_list, error_message).
        """
        url = f"http://{host}:{port}/health"
        key = get_fleet_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 401:
                    return None, [], "Auth Failed: Worker rejected Fleet API Key. Verify ~/.hivemind/fleet.key."
                if resp.status_code != 200:
                    return None, [], f"HTTP {resp.status_code}: {resp.text}"

                data = resp.json()
                tags = []
                os_val = data.get("os", "unknown")
                arch_val = data.get("arch", "unknown")
                tags.append(f"os-{os_val}")
                tags.append(f"arch-{arch_val}")

                cores = data.get("cpu_cores", 1)
                tags.append(f"{cores}-cores")
                if cores >= 16:
                    tags.append("high-cpu")
                else:
                    tags.append("standard-cpu")

                ram_gb = data.get("ram_gb")
                if ram_gb:
                    tags.append(f"{ram_gb}gb-ram")

                for engine in data.get("engines", []):
                    tags.append(f"engine:{engine}")

                if data.get("git_ready"):
                    tags.append("git")

                return data, tags, None

        except httpx.ConnectError:
            return None, [], f"Daemon not reachable on port {port} (install/start hivemind-worker)"
        except httpx.TimeoutException:
            return None, [], f"Timed out connecting to port {port}"
        except Exception as exc:
            return None, [], f"Probe error: {exc}"

    @classmethod
    def probe_remote(cls, host: str, user: Optional[str] = None, port: int = 7422, timeout: float = 3.0) -> List[str]:
        """Backward-compatible helper returning tags list."""
        _, tags, _ = cls.probe_daemon(host, port, timeout)
        return tags

