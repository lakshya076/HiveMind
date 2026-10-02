import json
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field


def get_hivemind_home() -> Path:
    """Returns the HiveMind home directory (~/.hivemind)."""
    return Path.home() / ".hivemind"


def get_default_cluster_path() -> Path:
    """Returns the default global cluster configuration path (~/.hivemind/cluster.json)."""
    return get_hivemind_home() / "cluster.json"


class WorkerNode(BaseModel):
    """Configuration for an individual worker machine in the cluster."""
    id: str = Field(..., description="Unique human-readable identifier for the worker")
    host: str = Field(..., description="IP address or hostname of the machine")
    port: int = Field(default=22, description="SSH port")
    user: str = Field(default="developer", description="SSH username")
    workspace_dir: str = Field(default="~/workspace/project", description="Target workspace directory on worker")
    agent_engine: str = Field(default="claude-code", description="AI Coding CLI engine (e.g. claude-code, agy, aider)")
    capabilities: List[str] = Field(default_factory=list, description="Hardware/software tags (e.g. gpu, 32-cores)")


class ClusterConfig(BaseModel):
    """Cluster definition containing a fleet of worker nodes."""
    cluster_name: str = Field(default="hive-fleet", description="Name of the cluster")
    repo_url: Optional[str] = Field(default=None, description="Shared git origin workers clone and push to")
    workers: List[WorkerNode] = Field(default_factory=list, description="List of configured worker nodes")

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "ClusterConfig":
        if path is not None:
            file_path = Path(path)
        else:
            file_path = get_default_cluster_path()
            if not file_path.exists():
                # Fallback to local ./cluster.json if global doesn't exist
                local_path = Path("cluster.json")
                if local_path.exists():
                    file_path = local_path

        if not file_path.exists():
            raise FileNotFoundError(f"Cluster configuration file '{file_path}' not found.")
        
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data)

    def save(self, path: Optional[str | Path] = None) -> Path:
        if path is not None:
            file_path = Path(path)
        else:
            file_path = get_default_cluster_path()
        
        # Ensure ~/.hivemind directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))
        return file_path
