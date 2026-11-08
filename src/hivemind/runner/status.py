from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
from typing import Optional

from hivemind.config.cluster import SUPPORTED_ENGINES, WorkerNode
from hivemind.runner.dispatch import DispatchError, validate_base_branch, validate_branch_ref
from hivemind.runner.remote import read_worker_result
from hivemind.runner.state import RunState, WorkerRunState

TERMINAL_STATUSES = {"succeeded", "failed", "no_changes", "cancelled"}
MAX_LOG_TAIL_CHARS = 65536
MAX_ERROR_CHARS = 8192


def worker_node_for(worker_state: WorkerRunState, workers_by_id: dict[str, WorkerNode]) -> WorkerNode:
    """The cluster's current entry for this worker, else the address saved with the run."""
    worker = workers_by_id.get(worker_state.worker_id)
    if worker is not None:
        return worker
    engine = worker_state.agent_engine.strip().lower() if worker_state.agent_engine else "claude-code"
    if engine not in SUPPORTED_ENGINES:
        engine = "claude-code"
    return WorkerNode(
        id=worker_state.worker_id,
        host=worker_state.host,
        port=worker_state.port or 7422,
        agent_engine=engine,
    )


def _poll_one_worker(
    worker_state: WorkerRunState,
    run_id: str,
    workers_by_id: dict[str, WorkerNode],
) -> tuple[str, Optional[dict], str]:
    run_identifier = worker_state.remote_run_dir or run_id
    return read_worker_result(worker_node_for(worker_state, workers_by_id), run_identifier)


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
        if log_tail and len(log_tail) > MAX_LOG_TAIL_CHARS:
            log_tail = log_tail[-MAX_LOG_TAIL_CHARS:]

        if payload:
            worker_state.log_tail = log_tail or worker_state.log_tail
            worker_state.status = str(payload.get("status") or status)
            raw_error = payload.get("error")
            worker_state.error = str(raw_error)[:MAX_ERROR_CHARS] if raw_error else None
            reported_branch = payload.get("branch")
            if isinstance(reported_branch, str) and reported_branch.strip():
                try:
                    worker_state.branch = validate_branch_ref(reported_branch)
                except DispatchError:
                    pass
            worker_state.pushed = payload.get("pushed")
            worker_state.commit = payload.get("commit")
            worker_state.test_exit_code = payload.get("test_exit_code")
            worker_state.agent_exit_code = payload.get("agent_exit_code")
            worker_state.bench_exit_code = payload.get("bench_exit_code")
            reported_base = payload.get("base_branch")
            if isinstance(reported_base, str) and reported_base.strip():
                try:
                    worker_state.base_branch = validate_base_branch(reported_base)
                except DispatchError:
                    pass
            if "benchmark_data" in payload and payload.get("benchmark_data") is not None:
                worker_state.benchmark_data = payload.get("benchmark_data")
        elif worker_state.status not in TERMINAL_STATUSES:
            # Only unfinished runs change: a finished result is never overwritten by a
            # worker that is offline or has since lost the run.
            worker_state.log_tail = log_tail or worker_state.log_tail
            worker_state.status = status
            if status in ("unknown", "failed") and log_tail:
                worker_state.error = log_tail[:MAX_ERROR_CHARS]

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
