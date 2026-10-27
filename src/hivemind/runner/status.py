from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
from typing import Optional

from hivemind.config.cluster import WorkerNode
from hivemind.runner.remote import read_worker_result
from hivemind.runner.state import RunState, WorkerRunState

TERMINAL_STATUSES = {"succeeded", "failed", "no_changes", "cancelled"}


def _poll_one_worker(
    worker_state: WorkerRunState,
    run_id: str,
    workers_by_id: dict[str, WorkerNode],
) -> tuple[str, Optional[dict], str]:
    worker = workers_by_id.get(worker_state.worker_id)
    if worker is None:
        worker = WorkerNode(
            id=worker_state.worker_id,
            host=worker_state.host,
            port=worker_state.port or 7422,
            user=worker_state.user,
            workspace_dir=worker_state.workspace_dir,
            agent_engine=worker_state.agent_engine,
        )
    run_identifier = worker_state.remote_run_dir or run_id
    return read_worker_result(worker, run_identifier)


def refresh_run(run: RunState, workers_by_id: dict[str, WorkerNode]) -> RunState:
    """Poll each worker in parallel for live task status and update local run state."""
    if not run.workers:
        run.save()
        return run

    count = len(run.workers)
    polled: list[Optional[tuple[str, Optional[dict], str]]] = [None] * count
    with ThreadPoolExecutor(max_workers=min(32, max(1, count))) as pool:
        futures = {
            pool.submit(_poll_one_worker, ws, run.run_id, workers_by_id): idx
            for idx, ws in enumerate(run.workers)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                polled[idx] = future.result()
            except Exception as exc:  # noqa: BLE001
                polled[idx] = ("unknown", None, f"Status poll error: {exc}")

    for idx, worker_state in enumerate(run.workers):
        res = polled[idx]
        if res is None:
            continue
        status, payload, log_tail = res

        worker_state.log_tail = log_tail or worker_state.log_tail
        if payload:
            worker_state.status = str(payload.get("status") or status)
            worker_state.error = payload.get("error") or None
            worker_state.branch = payload.get("branch") or worker_state.branch
            worker_state.pushed = payload.get("pushed")
            worker_state.commit = payload.get("commit")
            worker_state.test_exit_code = payload.get("test_exit_code")
            worker_state.agent_exit_code = payload.get("agent_exit_code")
            worker_state.bench_exit_code = payload.get("bench_exit_code")
            if "benchmark_data" in payload and payload.get("benchmark_data") is not None:
                worker_state.benchmark_data = payload.get("benchmark_data")
            if payload.get("benchmark_path") is not None:
                worker_state.benchmark_path = payload.get("benchmark_path")
        elif worker_state.status not in TERMINAL_STATUSES:
            worker_state.status = status
            if status == "unknown" and log_tail:
                worker_state.error = log_tail

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


def local_working_tree_dirty() -> bool:
    result = local_git("status", "--porcelain", "--untracked-files=no", check=False)
    return bool(result.stdout.strip())


def local_base_branch() -> str:
    sym = local_git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if sym.returncode == 0 and sym.stdout.strip():
        ref = sym.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix) and ref[len(prefix):]:
            return ref[len(prefix):]

    for name in ("main", "master"):
        exists = local_git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        if exists.returncode == 0:
            return name
        remote = local_git("show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}", check=False)
        if remote.returncode == 0:
            return name

    head = local_git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if head.returncode == 0 and head.stdout.strip():
        cur = head.stdout.strip()
        if not cur.startswith("exp/"):
            return cur

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
