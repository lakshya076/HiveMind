import subprocess
from typing import Optional

from hivemind.config.cluster import WorkerNode
from hivemind.runner.remote import read_worker_result
from hivemind.runner.state import RunState
from hivemind.ssh import SSHError


def refresh_run(run: RunState, workers_by_id: dict[str, WorkerNode]) -> RunState:
    """Poll each worker for result.json and update local run state."""
    for worker_state in run.workers:
        if worker_state.status in {"succeeded", "failed"} and worker_state.remote_run_dir:
            # Still poll so a previously "running" machine that finished is updated;
            # already-terminal rows can be refreshed too in case of late writes.
            pass
        worker = workers_by_id.get(worker_state.worker_id)
        if worker is None:
            worker = WorkerNode(
                id=worker_state.worker_id,
                host=worker_state.host,
                port=worker_state.port,
                user=worker_state.user,
                workspace_dir=worker_state.workspace_dir,
                agent_engine=worker_state.agent_engine,
            )
        if not worker_state.remote_run_dir:
            continue
        try:
            status, payload, log_tail = read_worker_result(worker, worker_state.remote_run_dir)
        except SSHError as exc:
            worker_state.log_tail = str(exc)
            if worker_state.status not in {"succeeded", "failed"}:
                worker_state.status = "unknown"
                worker_state.error = str(exc)
            continue

        worker_state.log_tail = log_tail or worker_state.log_tail
        if payload:
            worker_state.status = str(payload.get("status") or status)
            worker_state.error = payload.get("error") or None
            worker_state.branch = payload.get("branch") or worker_state.branch
            worker_state.test_exit_code = payload.get("test_exit_code")
            worker_state.agent_exit_code = payload.get("agent_exit_code")
            worker_state.benchmark_path = payload.get("benchmark_path")
        elif worker_state.status not in {"succeeded", "failed"}:
            worker_state.status = status

    run.save()
    return run


def local_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def local_repo_origin() -> Optional[str]:
    result = local_git("remote", "get-url", "origin", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def local_base_branch() -> str:
    for name in ("main", "master"):
        exists = local_git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        if exists.returncode == 0:
            return name
        remote = local_git("show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}", check=False)
        if remote.returncode == 0:
            return name
    raise RuntimeError("Local repo has neither main nor master.")


def normalize_git_url(url: str) -> str:
    value = url.strip().rstrip("/")
    for prefix in ("ssh://git@", "https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.startswith("git@"):
        value = value[4:].replace(":", "/", 1)
    if value.endswith(".git"):
        value = value[:-4]
    return value.lower()
