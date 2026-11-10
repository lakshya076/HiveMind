from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Any, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from hivemind.config.cluster import get_hivemind_home, validate_worker_host

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


def _write_private_file(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing to write through symlink: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _run_file_for(run_id: str) -> Path:
    """Return the confined state file path for run_id inside ~/.hivemind/runs/."""
    clean_id = validate_run_id(run_id)
    runs_dir = get_runs_dir().expanduser()
    if runs_dir.is_symlink():
        raise ValueError(f"Refusing symlinked runs directory: {runs_dir}")
    root = runs_dir.resolve()
    raw_target = runs_dir / f"{clean_id}.json"
    if raw_target.is_symlink():
        raise ValueError(f"Refusing symlinked run state file: {raw_target}")
    target = raw_target.resolve()
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
    port: int = Field(default=7422, ge=1, le=65535)
    agent_engine: str
    strategy: str
    branch: str
    base_branch: Optional[str] = None  # base the worker reports it started from
    remote_run_dir: str = ""
    status: str = "starting"
    pushed: Optional[bool] = None
    commit: Optional[str] = None
    error: Optional[str] = None
    test_exit_code: Optional[int] = None
    agent_exit_code: Optional[int] = None
    bench_exit_code: Optional[int] = None
    benchmark_data: Optional[Any] = None
    log_tail: Optional[str] = None

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        return validate_worker_host(value)


class RunState(BaseModel):
    run_id: str
    prompt: str
    repo_url: str
    base_branch: Optional[str] = None  # requested with --base; None means origin's default branch
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
        _write_private_file(file_path, self.model_dump_json(indent=2))
        _write_private_file(get_runs_dir() / LATEST_POINTER, self.run_id)
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
        if pointer.is_symlink():
            raise ValueError(f"Refusing symlinked latest pointer: {pointer}")
        if not pointer.exists():
            raise FileNotFoundError("No HiveMind runs found. Dispatch a job first.")
        run_id = pointer.read_text(encoding="utf-8").strip()
        if not run_id:
            raise FileNotFoundError("Latest-run pointer is empty. Dispatch a job first.")
        return cls.load(run_id)

    @classmethod
    def load_all(cls) -> List["RunState"]:
        """Every saved run, newest first. Unreadable or hand-edited files are skipped."""
        runs: List[RunState] = []
        for path in get_runs_dir().glob("*.json"):
            try:
                runs.append(cls.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        runs.sort(key=lambda r: (r.created_at, r.run_id), reverse=True)
        return runs
