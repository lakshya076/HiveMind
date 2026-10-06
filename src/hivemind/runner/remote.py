import json
import re
from typing import Optional

import httpx

from hivemind.config.cluster import WorkerNode, get_fleet_key
from hivemind.runner.state import WorkerRunState


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "strategy"


def branch_name(worker_id: str, strategy: str) -> str:
    return f"exp/{slugify(worker_id)}-{slugify(strategy)}"


def _get_auth_headers() -> dict[str, str]:
    key = get_fleet_key()
    if key:
        return {"Authorization": f"Bearer {key}"}
    return {}


def start_worker_job(
    worker: WorkerNode,
    *,
    run_id: str,
    repo_url: str,
    prompt: str,
    strategy: str,
    strategy_index: int,
    strategy_count: int,
    test_command: Optional[str],
    bench_command: Optional[str],
    model: Optional[str],
) -> WorkerRunState:
    """Dispatch task to hivemind-worker HTTP daemon. Returns immediately."""
    branch = branch_name(worker.id, strategy)
    state = WorkerRunState(
        worker_id=worker.id,
        host=worker.host,
        port=worker.port,
        user=worker.user or "developer",
        workspace_dir=worker.workspace_dir,
        agent_engine=worker.agent_engine,
        strategy=strategy,
        branch=branch,
        status="starting",
        remote_run_dir=run_id,
    )

    url = f"http://{worker.host}:{worker.port}/dispatch"
    payload = {
        "run_id": run_id,
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
        "workspace_dir": worker.workspace_dir,
    }

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


def read_worker_result(worker: WorkerNode, run_id: str) -> tuple[str, Optional[dict], str]:
    """Poll hivemind-worker status endpoint for run_id. Return (status, payload_dict, log_tail)."""
    url = f"http://{worker.host}:{worker.port}/status/{run_id}"
    headers = _get_auth_headers()

    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 404:
                return "running", None, ""
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

