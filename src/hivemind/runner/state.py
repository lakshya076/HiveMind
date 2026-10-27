from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from hivemind.config.cluster import get_hivemind_home

LATEST_POINTER = "latest"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_run_id(value: str) -> str:
    """Ensure run_id is a single safe identifier with no path traversal."""
    if not isinstance(value, str):
        raise ValueError("run_id must be a string")
    cleaned = value.strip()
    if not cleaned or ".." in cleaned or not _RUN_ID_RE.match(cleaned):
        raise ValueError(
            f"Invalid run_id {value!r}: must be 1-128 characters of [A-Za-z0-9._-] and cannot contain '..'"
        )
    return cleaned


def _run_file_for(run_id: str) -> Path:
    """Return the confined state file path for run_id inside ~/.hivemind/runs/."""
    clean_id = validate_run_id(run_id)
    root = get_runs_dir().expanduser().resolve()
    target = (root / f"{clean_id}.json").resolve()
    if target.parent != root or not target.is_relative_to(root):
        raise ValueError(f"Refusing run_id {run_id!r} outside runs directory")
    return target


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

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, value: str) -> str:
        return validate_run_id(value)

    @property
    def path(self) -> Path:
        return _run_file_for(self.run_id)

    def save(self) -> Path:
        file_path = self.path
        file_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        (get_runs_dir() / LATEST_POINTER).write_text(self.run_id, encoding="utf-8")
        return file_path

    @classmethod
    def load(cls, run_id: str) -> "RunState":
        file_path = _run_file_for(run_id)
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
