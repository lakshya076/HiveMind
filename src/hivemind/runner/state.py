from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from hivemind.config.cluster import get_hivemind_home

LATEST_POINTER = "latest"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid4().hex[:8]}"


def get_runs_dir() -> Path:
    path = get_hivemind_home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


class WorkerRunState(BaseModel):
    worker_id: str
    host: str
    port: int
    user: str
    workspace_dir: str
    agent_engine: str
    strategy: str
    branch: str
    remote_run_dir: str = ""
    status: str = "starting"
    pushed: Optional[bool] = None
    commit: Optional[str] = None
    error: Optional[str] = None
    test_exit_code: Optional[int] = None
    agent_exit_code: Optional[int] = None
    bench_exit_code: Optional[int] = None
    benchmark_data: Optional[Any] = None
    benchmark_path: Optional[str] = None
    log_tail: Optional[str] = None


class RunState(BaseModel):
    run_id: str
    prompt: str
    repo_url: str
    test_command: Optional[str] = None
    bench_command: Optional[str] = None
    model: Optional[str] = None
    created_at: str = Field(default_factory=utc_now)
    workers: List[WorkerRunState] = Field(default_factory=list)

    @property
    def path(self) -> Path:
        return get_runs_dir() / f"{self.run_id}.json"

    def save(self) -> Path:
        file_path = self.path
        file_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        (get_runs_dir() / LATEST_POINTER).write_text(self.run_id, encoding="utf-8")
        return file_path

    @classmethod
    def load(cls, run_id: str) -> "RunState":
        file_path = get_runs_dir() / f"{run_id}.json"
        if not file_path.exists():
            raise FileNotFoundError(f"Run '{run_id}' not found at {file_path}")
        return cls.model_validate_json(file_path.read_text(encoding="utf-8"))

    @classmethod
    def load_latest(cls) -> "RunState":
        pointer = get_runs_dir() / LATEST_POINTER
        if not pointer.exists():
            raise FileNotFoundError("No HiveMind runs found. Dispatch a job first.")
        run_id = pointer.read_text(encoding="utf-8").strip()
        if not run_id:
            raise FileNotFoundError("Latest-run pointer is empty. Dispatch a job first.")
        return cls.load(run_id)
