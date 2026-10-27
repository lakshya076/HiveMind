from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Optional, Sequence

from hivemind.config.cluster import ClusterConfig, WorkerNode
from hivemind.runner.remote import start_worker_job
from hivemind.runner.state import RunState, WorkerRunState, new_run_id

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")


class DispatchError(ValueError):
    pass


def validate_branch_ref(branch: str) -> str:
    cleaned = (branch or "").strip()
    if (
        not cleaned
        or not cleaned.startswith("exp/")
        or len(cleaned) <= 4
        or cleaned.startswith("-")
        or cleaned.endswith(("/", ".lock", "."))
        or ".." in cleaned
        or "//" in cleaned
        or "@{" in cleaned
        or not _BRANCH_RE.match(cleaned)
    ):
        raise DispatchError(
            f"Invalid branch name {branch!r}: worker branches must be valid refs under 'exp/'."
        )
    return cleaned


def select_workers(config: ClusterConfig, worker_ids: Optional[Sequence[str]]) -> list[WorkerNode]:
    if not config.workers:
        raise DispatchError("Cluster has no workers. Run hive discover first.")
    if not worker_ids:
        return list(config.workers)

    wanted = [wid.strip() for wid in worker_ids if wid.strip()]
    by_id = {w.id: w for w in config.workers}
    missing = [wid for wid in wanted if wid not in by_id]
    if missing:
        raise DispatchError(f"Unknown worker id(s): {', '.join(missing)}")
    return [by_id[wid] for wid in wanted]


def assign_strategies(workers: Sequence[WorkerNode], named: Sequence[str]) -> list[str]:
    strategies: list[str] = []
    for index, _worker in enumerate(workers):
        if index < len(named) and named[index].strip():
            strategies.append(named[index].strip())
        else:
            strategies.append(f"strategy-{index + 1}")
    return strategies


def resolve_repo_url(config: ClusterConfig, repo_override: Optional[str]) -> str:
    repo_url = (repo_override or config.repo_url or "").strip()
    if not repo_url:
        raise DispatchError(
            "No git origin configured. Set repo_url in cluster.json or pass --repo."
        )
    if repo_url.startswith("-"):
        raise DispatchError(f"Invalid repo URL {repo_url!r}: cannot start with '-'.")
    if repo_url.startswith(("\\\\", "//")):
        raise DispatchError(f"Invalid repo URL {repo_url!r}: UNC network share paths are not allowed.")
    lower = repo_url.lower()
    if lower.startswith(("file:////", "file://\\\\")):
        raise DispatchError(f"Invalid repo URL {repo_url!r}: UNC file URLs are not allowed.")
    if lower.startswith(("ext::", "fd::")) or "::" in lower.split("://", 1)[0]:
        raise DispatchError(f"Invalid repo URL {repo_url!r}: forbidden git remote helper protocol.")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in repo_url):
        raise DispatchError(f"Invalid repo URL {repo_url!r}: whitespace and control characters are not allowed.")
    return repo_url


def dispatch_run(
    config: ClusterConfig,
    prompt: str,
    *,
    repo_url: str,
    workers: Sequence[WorkerNode],
    strategies: Sequence[str],
    test_command: Optional[str] = None,
    bench_command: Optional[str] = None,
    model: Optional[str] = None,
) -> RunState:
    if not prompt.strip():
        raise DispatchError("Prompt is empty.")
    if not workers:
        raise DispatchError("No workers selected.")

    run_id = new_run_id()
    run = RunState(
        run_id=run_id,
        prompt=prompt.strip(),
        repo_url=repo_url,
        test_command=test_command,
        bench_command=bench_command,
        model=model,
        workers=[],
    )

    count = len(workers)

    def launch(index: int, worker: WorkerNode) -> WorkerRunState:
        return start_worker_job(
            worker,
            run_id=run_id,
            repo_url=repo_url,
            prompt=prompt.strip(),
            strategy=strategies[index],
            strategy_index=index + 1,
            strategy_count=count,
            test_command=test_command,
            bench_command=bench_command,
            model=model,
        )

    results: list[Optional[WorkerRunState]] = [None] * count
    with ThreadPoolExecutor(max_workers=max(1, count)) as pool:
        futures = {
            pool.submit(launch, index, worker): index
            for index, worker in enumerate(workers)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001
                worker = workers[index]
                results[index] = WorkerRunState(
                    worker_id=worker.id,
                    host=worker.host,
                    port=worker.port,
                    user=worker.user,
                    workspace_dir=worker.workspace_dir,
                    agent_engine=worker.agent_engine,
                    strategy=strategies[index],
                    branch="",
                    status="failed",
                    error=str(exc),
                )

    run.workers = [item for item in results if item is not None]
    run.save()
    return run
