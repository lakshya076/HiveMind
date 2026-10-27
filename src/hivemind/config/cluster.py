import ipaddress
import json
import os
import re
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")


def validate_worker_host(value: str) -> str:
    """Validate that worker host is a bare IP literal or hostname with no URL authority/path injection."""
    if not isinstance(value, str):
        raise ValueError("host must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("host cannot be empty")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ValueError(f"Invalid worker host {value!r}: whitespace and control characters are not allowed")
    if any(ch in cleaned for ch in ("/", "\\", "?", "#", "@")):
        raise ValueError(f"Invalid worker host {value!r}: URL authority/path characters are not allowed")

    unbracketed = cleaned[1:-1] if (cleaned.startswith("[") and cleaned.endswith("]")) else cleaned
    try:
        ip = ipaddress.ip_address(unbracketed)
        return str(ip)
    except ValueError:
        pass

    if ":" in cleaned or ".." in cleaned or not _HOSTNAME_RE.match(cleaned):
        raise ValueError(f"Invalid worker host {value!r}: must be a valid IP address or hostname")
    return cleaned


def get_hivemind_home() -> Path:
    """Returns the HiveMind home directory (~/.hivemind)."""
    return Path.home() / ".hivemind"


def get_default_cluster_path() -> Path:
    """Returns the default global cluster configuration path (~/.hivemind/cluster.json)."""
    return get_hivemind_home() / "cluster.json"


def get_fleet_key() -> Optional[str]:
    """Retrieve the fleet API key from ~/.hivemind/fleet.key or environment."""
    key_file = get_hivemind_home() / "fleet.key"
    if key_file.exists():
        try:
            val = key_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception:
            pass
    return os.environ.get("HIVEMIND_FLEET_KEY")


class WorkerNode(BaseModel):
    """Configuration for an individual worker machine in the cluster."""
    id: str = Field(..., min_length=1, max_length=128, description="Unique human-readable identifier for the worker")
    host: str = Field(..., description="IP address or hostname of the machine (Tailscale / LAN)")
    port: int = Field(default=7422, ge=1, le=65535, description="Worker HTTP daemon port (default: 7422)")
    user: Optional[str] = Field(default=None, description="Optional worker username")
    workspace_dir: str = Field(default="~/hive-workspace", description="Target workspace directory on worker")
    agent_engine: str = Field(default="claude-code", description="AI Coding CLI engine (e.g. claude-code, agy, aider)")
    capabilities: List[str] = Field(default_factory=list, description="Hardware/software tags (e.g. gpu, 32-cores)")

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return validate_worker_host(value)


class ClusterConfig(BaseModel):
    """Cluster definition containing a fleet of worker nodes."""
    cluster_name: str = Field(default="hive-fleet", description="Name of the cluster")
    repo_url: Optional[str] = Field(default=None, description="Shared git origin workers clone and push to")
    workers: List[WorkerNode] = Field(default_factory=list, description="List of configured worker nodes")

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "ClusterConfig":
        file_path = Path(path) if path is not None else get_default_cluster_path()
        if not file_path.exists():
            raise FileNotFoundError(f"Cluster configuration file '{file_path}' not found.")

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data)

    def save(self, path: Optional[str | Path] = None) -> Path:
        file_path = Path(path) if path is not None else get_default_cluster_path()
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))
        return file_path
