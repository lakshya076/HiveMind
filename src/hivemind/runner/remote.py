import re
from typing import Optional

import httpx

from hivemind.config.cluster import WorkerNode, get_fleet_key
from hivemind.runner.state import WorkerRunState, validate_run_id


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "strategy"


def branch_name(worker_id: str, strategy: str, run_id: Optional[str] = None) -> str:
    base = f"{slugify(worker_id)}-{slugify(strategy)}"
    if run_id and run_id.strip():
        return f"exp/{slugify(run_id)}/{base}"
    return f"exp/{base}"


def _get_auth_headers() -> dict[str, str]:
    key = get_fleet_key()
    if key:
        return {"Authorization": f"Bearer {key}"}
    return {}


def start_worker_job(
    worker: WorkerNode,
    *,
    run_id: str,
    remote_run_id: Optional[str] = None,
    repo_url: str,
    prompt: str,
    strategy: str,
    strategy_index: int,
    strategy_count: int,
    test_command: Optional[str],
    bench_command: Optional[str],
    model: Optional[str],
    base_branch: Optional[str] = None,
) -> WorkerRunState:
    """Dispatch task to hivemind-worker HTTP daemon. Returns immediately.

    ``remote_run_id`` is the run ID used on the worker (default: ``run_id``).
    ``base_branch`` must only be sent to workers that advertise the base_branch
    feature (see dispatch.workers_missing_feature); older workers ignore it.
    """
    remote_run_id = validate_run_id(remote_run_id or run_id)
    branch = branch_name(worker.id, strategy, run_id=run_id)
    state = WorkerRunState(
        worker_id=worker.id,
        host=worker.host,
        port=worker.port,
        agent_engine=worker.agent_engine,
        strategy=strategy,
        branch=branch,
        base_branch=base_branch,
        status="starting",
        remote_run_dir=remote_run_id,
    )

    url = f"http://{worker.host}:{worker.port}/dispatch"
    payload = {
        "run_id": remote_run_id,
        "repo_url": repo_url,
        "prompt": prompt,
        "strategy": strategy,
        "strategy_index": strategy_index,
        "strategy_count": strategy_count,
        "branch": branch,
        "agent_engine": worker.agent_engine,
        "model": model,
        "test_command": test_command,
        "bench_command": bench_command,
    }
    if base_branch:
        payload["base_branch"] = base_branch

    try:
        headers = _get_auth_headers()
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code == 401:
                state.status = "failed"
                state.error = "Unauthorized: Fleet API key rejected by worker."
                return state
            if resp.status_code >= 400:
                state.status = "failed"
                state.error = f"Worker rejected task (HTTP {resp.status_code}): {resp.text}"
                return state

            data = resp.json()
            state.status = data.get("status", "running")
    except httpx.ConnectError:
        state.status = "failed"
        state.error = f"Cannot connect to worker at {worker.host}:{worker.port}. Is hivemind-worker running?"
    except Exception as exc:
        state.status = "failed"
        state.error = f"HTTP dispatch error: {exc}"

    return state


LOG_FILES = ("agent", "test", "bench", "wrapper")


def fetch_worker_log(
    worker: WorkerNode, run_id: str, file: str = "agent", max_bytes: int = 1024 * 1024
) -> tuple[Optional[dict], Optional[str]]:
    """Fetch the end of a run's log from GET /logs/{run_id}. Returns (payload, error)."""
    try:
        clean_run_id = validate_run_id(run_id)
    except ValueError as exc:
        return None, f"Invalid run_id: {exc}"
    url = f"http://{worker.host}:{worker.port}/logs/{clean_run_id}"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params={"file": file, "max_bytes": max_bytes}, headers=_get_auth_headers())
    except httpx.ConnectError:
        return None, f"Cannot connect to worker at {worker.host}:{worker.port}."
    except Exception as exc:
        return None, f"Log fetch error: {exc}"
    if resp.status_code == 200:
        return resp.json(), None
    if resp.status_code == 401:
        return None, "Unauthorized: Fleet API key rejected by worker."
    detail = ""
    try:
        detail = str(resp.json().get("detail") or "")
    except ValueError:
        pass
    if resp.status_code == 404 and detail == "Not Found":
        # FastAPI's answer for an unknown route: a worker older than 0.3.0.
        return None, "this worker can't send full logs (update hivemind-worker to 0.3.0 or later)"
    return None, detail or f"HTTP {resp.status_code}: {resp.text}"


def cancel_worker_job(worker: WorkerNode, run_id: str) -> tuple[bool, str]:
    """Ask the worker to cancel run_id. Returns (cancelled, message)."""
    try:
        clean_run_id = validate_run_id(run_id)
    except ValueError as exc:
        return False, f"Invalid run_id: {exc}"
    url = f"http://{worker.host}:{worker.port}/cancel/{clean_run_id}"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.delete(url, headers=_get_auth_headers())
    except httpx.ConnectError:
        return False, f"Cannot connect to worker at {worker.host}:{worker.port}."
    except Exception as exc:
        return False, f"Cancel error: {exc}"
    if resp.status_code == 200:
        return True, "cancelled"
    if resp.status_code == 404:
        return False, "not running on the worker (already finished, or unknown to it)"
    if resp.status_code == 401:
        return False, "Unauthorized: Fleet API key rejected by worker."
    return False, f"HTTP {resp.status_code}: {resp.text}"


def read_worker_result(worker: WorkerNode, run_id: str) -> tuple[str, Optional[dict], str]:
    """Poll hivemind-worker status endpoint for run_id. Return (status, payload_dict, log_tail)."""
    try:
        clean_run_id = validate_run_id(run_id)
    except ValueError as exc:
        return "unknown", None, f"Invalid run_id: {exc}"
    url = f"http://{worker.host}:{worker.port}/status/{clean_run_id}"
    headers = _get_auth_headers()

    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 404:
                # The worker records a run before /dispatch returns, so a 404 means it has lost
                # the run (reinstalled, workspace_root changed). Reporting "running" made
                # `hive status --watch` wait forever.
                return "failed", None, (
                    f"Worker {worker.id} has no record of run {clean_run_id} "
                    "(was it reinstalled, or its workspace_root changed?)"
                )
            if resp.status_code == 401:
                return "unknown", None, "Authentication failed (invalid fleet key)"
            if resp.status_code != 200:
                return "unknown", None, f"HTTP {resp.status_code}: {resp.text}"

            data = resp.json()
            status = str(data.get("status") or "unknown")
            log_tail = str(data.get("log_tail") or "")
            return status, data, log_tail
    except httpx.ConnectError:
        return "unknown", None, f"Connection to {worker.host}:{worker.port} refused"
    except Exception as exc:
        return "unknown", None, f"Status poll error: {exc}"
