from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re
from typing import Optional, Sequence

from hivemind.config.cluster import SUPPORTED_ENGINES, ClusterConfig, WorkerNode
from hivemind.discovery.prober import SystemProber
from hivemind.runner.remote import slugify, start_worker_job
from hivemind.runner.state import RunState, WorkerRunState, new_run_id

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
# Same limits the worker enforces at /dispatch (hivemind-worker models.py / agent.py),
# checked here so a mistake fails before any worker is contacted.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
MAX_PROMPT_CHARS = 200_000
MAX_STRATEGY_HINT_CHARS = 2000
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
MAX_PROTECT_PATHS = 32
MAX_PROTECT_PATH_CHARS = 512


def _copy_worker_slot(
    worker: WorkerNode,
    *,
    engine: Optional[str] = None,
    engine_explicit: bool = False,
    model: Optional[str] = None,
    hint: Optional[str] = None,
) -> WorkerNode:
    w = WorkerNode(
        id=worker.id,
        host=worker.host,
        port=worker.port,
        agent_engine=engine or worker.agent_engine,
        capabilities=list(worker.capabilities),
    )
    w._engine_explicit = engine_explicit or getattr(worker, "_engine_explicit", False)
    w._model_override = model if model is not None else getattr(worker, "_model_override", None)
    w._strategy_hint = hint if hint is not None else getattr(worker, "_strategy_hint", None)
    return w


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


def validate_base_branch(branch: Optional[str]) -> Optional[str]:
    """A branch to start runs from: any valid ref (it is only read, never pushed)."""
    if branch is None or not branch.strip():
        return None
    cleaned = branch.strip()
    if (
        cleaned.startswith("-")
        or cleaned.endswith(("/", ".lock", "."))
        or ".." in cleaned
        or "//" in cleaned
        or "@{" in cleaned
        or not _BRANCH_RE.match(cleaned)
    ):
        raise DispatchError(f"Invalid base branch {branch!r}.")
    return cleaned


def validate_protect_paths(paths: Optional[Sequence[str]]) -> Optional[list[str]]:
    """Repo-relative files or directories the worker restores after the agent runs.

    Same rules the worker enforces at /dispatch: backslashes count as separators, and
    absolute paths, drive letters, '..', '.', '.git' and option-like paths are refused.
    """
    if isinstance(paths, (str, bytes)):
        raise DispatchError("protect_paths must be a list of paths, not a single string.")
    if not paths:
        return None
    if len(paths) > MAX_PROTECT_PATHS:
        raise DispatchError(f"At most {MAX_PROTECT_PATHS} protect paths are allowed.")
    cleaned_paths = []
    for path in paths:
        if not isinstance(path, str):
            raise DispatchError(f"Invalid protect path {path!r}: must be a string.")
        cleaned = path.strip().replace("\\", "/").rstrip("/")
        if (
            not cleaned
            or len(cleaned) > MAX_PROTECT_PATH_CHARS
            or cleaned.startswith(("/", "-", ":"))
            or _DRIVE_RE.match(cleaned)
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned)
            or any(part in ("", ".", "..", ".git") for part in cleaned.split("/"))
        ):
            raise DispatchError(
                f"Invalid protect path {path!r}: use a path relative to the repository root, e.g. tests/ "
                "(no '..', '.', '.git', leading '-' or '/', drive letter or control characters)."
            )
        cleaned_paths.append(cleaned)
    deduped: list[str] = []
    seen_lower: set[str] = set()
    for p in cleaned_paths:
        low = p.lower()
        if low not in seen_lower:
            seen_lower.add(low)
            deduped.append(p)
    return [
        p for p in deduped
        if not any(other != p and p.lower().startswith(other.lower() + "/") for other in deduped)
    ]


def workers_missing_feature(
    workers: Sequence[WorkerNode], feature: str, needs: str = "0.3.0 or later"
) -> list[str]:
    """One problem line per distinct worker that doesn't advertise ``feature`` in /health.

    Workers ignore dispatch fields they don't know, so sending e.g. base_branch to an
    older worker would make it quietly start from the default branch and report success.
    A worker that can't be checked counts as missing the feature.
    """
    unique = list({w.id: w for w in workers}.values())
    with ThreadPoolExecutor(max_workers=min(32, max(1, len(unique)))) as pool:
        probes = list(pool.map(lambda w: SystemProber.probe_daemon(w.host, w.port, 3.5), unique))
    problems = []
    for worker, (data, _tags, err) in zip(unique, probes):
        if not data:
            problems.append(f"{worker.id}: could not check ({err})")
        elif feature not in (data.get("features") or []):
            problems.append(f"{worker.id}: hivemind-worker {data.get('version', '?')} does not support it (needs {needs})")
    return problems


def workers_missing_engine(workers: Sequence[WorkerNode]) -> list[str]:
    """One problem line per worker slot whose target ``agent_engine`` is not listed in /health ``engines``."""
    unique = list({w.id: w for w in workers}.values())
    with ThreadPoolExecutor(max_workers=min(32, max(1, len(unique)))) as pool:
        probes = list(pool.map(lambda w: SystemProber.probe_daemon(w.host, w.port, 3.5), unique))
    health_by_id = {w.id: (data, err) for w, (data, _tags, err) in zip(unique, probes)}
    problems: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    for worker in workers:
        wanted = "claude-code" if worker.agent_engine == "claude" else worker.agent_engine
        if (worker.id, wanted) in seen_pairs:
            continue
        seen_pairs.add((worker.id, wanted))
        data, err = health_by_id[worker.id]
        if not data:
            problems.append(f"{worker.id}: could not check ({err})")
        else:
            installed = data.get("engines") or []
            if wanted not in installed:
                avail = ", ".join(installed) if installed else "none"
                problems.append(f"{worker.id}: engine {worker.agent_engine!r} is not installed (available: {avail})")
    return problems


def select_workers(
    config: ClusterConfig,
    worker_ids: Optional[Sequence[str]],
    required_tags: Optional[Sequence[str]] = None,
) -> list[WorkerNode]:
    if not config.workers:
        raise DispatchError("Cluster has no workers. Run hive discover first.")
    if not worker_ids:
        selected = list(config.workers)
    else:
        wanted = [wid.strip() for wid in worker_ids if wid.strip()]
        by_id = {w.id: w for w in config.workers}
        missing = [wid for wid in wanted if wid not in by_id]
        if missing:
            raise DispatchError(f"Unknown worker id(s): {', '.join(missing)}")
        selected = [by_id[wid] for wid in wanted]

    if required_tags:
        req = [t.strip().lower() for t in required_tags if t and t.strip()]
        if req:
            req_set = set(req)
            selected = [w for w in selected if req_set <= {c.strip().lower() for c in w.capabilities}]
            if not selected:
                raise DispatchError(f"No workers match required capability tag(s): {', '.join(req)}")
    return selected


@dataclass(frozen=True)
class StrategySpec:
    name: str
    engine: Optional[str] = None
    model: Optional[str] = None
    hint: Optional[str] = None


def parse_strategy_spec(raw: str) -> StrategySpec:
    """Parse ``name[@engine[:model]][=hint]`` into its components."""
    head, hint = raw.split("=", 1) if "=" in raw else (raw, None)
    if hint is not None:
        hint = hint.strip() or None
        if hint is not None and (
            len(hint) > MAX_STRATEGY_HINT_CHARS
            or any((ord(ch) < 0x20 and ch not in ("\t", "\n", "\r")) or ord(ch) == 0x7F for ch in hint)
        ):
            raise DispatchError(
                f"Invalid strategy hint in {raw!r}: at most {MAX_STRATEGY_HINT_CHARS} characters and no control characters."
            )
    if "@" in head:
        name_part, eng_part = head.split("@", 1)
    else:
        name_part, eng_part = head, None
    name = name_part.strip()
    if not name:
        raise DispatchError(f"Strategy name cannot be empty in {raw!r}.")
    engine: Optional[str] = None
    model: Optional[str] = None
    if eng_part is not None:
        if ":" in eng_part:
            eng_raw, mod_raw = eng_part.split(":", 1)
        else:
            eng_raw, mod_raw = eng_part, None
        engine = eng_raw.strip().lower()
        if engine not in SUPPORTED_ENGINES:
            raise DispatchError(
                f"Unknown engine {eng_raw!r} in strategy {raw!r}; supported are {', '.join(SUPPORTED_ENGINES)}."
            )
        if mod_raw is not None:
            model = mod_raw.strip()
            if not model or not _MODEL_RE.match(model):
                raise DispatchError(
                    f"Invalid model name {mod_raw!r} in strategy {raw!r}: use letters, digits and ._:/@+- only."
                )
    return StrategySpec(name=name, engine=engine, model=model, hint=hint)


def assign_strategies(
    workers: Sequence[WorkerNode], named: Sequence[str]
) -> tuple[list[WorkerNode], list[str]]:
    """Pair each strategy with a worker slot.

    Supports ``name[@engine[:model]][=hint]`` syntax per strategy.
    With more strategies than workers, strategies wrap round the workers, so one
    worker runs several (its daemon queues them beyond max_concurrent_runs).
    Unnamed slots get strategy-<n>. Returns parallel (slot_workers, strategies).
    """
    if not workers:
        raise DispatchError("No workers selected.")
    raw_specs = [s.strip() for s in named if s and s.strip()]
    parsed = [parse_strategy_spec(s) for s in raw_specs]
    count = max(len(workers), len(parsed))
    slot_workers: list[WorkerNode] = []
    strategies: list[str] = []

    for i in range(count):
        base_worker = workers[i % len(workers)]
        if i < len(parsed):
            spec = parsed[i]
            if spec.engine or spec.model or spec.hint:
                w = _copy_worker_slot(
                    base_worker,
                    engine=spec.engine,
                    engine_explicit=bool(spec.engine),
                    model=spec.model,
                    hint=spec.hint,
                )
            else:
                w = base_worker
            strategies.append(spec.name)
        else:
            w = base_worker
            strategies.append(f"strategy-{i + 1}")
        slot_workers.append(w)

    seen: set[tuple[str, str]] = set()
    for worker, strategy in zip(slot_workers, strategies):
        key = (worker.id, slugify(strategy))
        if key in seen:
            raise DispatchError(
                f"Worker '{worker.id}' would run strategy '{strategy}' twice; give each strategy a distinct name."
            )
        seen.add(key)
    return slot_workers, strategies


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
    strategy_hints: Optional[Sequence[Optional[str]]] = None,
    test_command: Optional[str] = None,
    bench_command: Optional[str] = None,
    model: Optional[str] = None,
    base_branch: Optional[str] = None,
    protect_paths: Optional[Sequence[str]] = None,
    timeout: Optional[float] = None,
    check_engines: bool = False,
) -> RunState:
    base_branch = validate_base_branch(base_branch)
    protect_paths = validate_protect_paths(protect_paths)
    if not prompt.strip():
        raise DispatchError("Prompt is empty.")
    if len(prompt.strip()) > MAX_PROMPT_CHARS:
        raise DispatchError(f"Prompt is {len(prompt.strip())} characters; workers accept at most {MAX_PROMPT_CHARS}.")
    if model is not None and not _MODEL_RE.match(model):
        raise DispatchError(
            f"Invalid model name {model!r}: use letters, digits and ._:/@+- only, starting with a letter or digit."
        )
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 86400:
            raise DispatchError("Timeout must be between 1 and 86400 seconds.")
        timeout = float(timeout)
    if not workers:
        raise DispatchError("No workers selected.")

    resolved_workers: list[WorkerNode] = []
    resolved_strategies: list[str] = []
    resolved_hints: list[Optional[str]] = []
    resolved_models: list[Optional[str]] = []
    any_explicit_engine = check_engines

    for idx, (w, raw_strat) in enumerate(zip(workers, strategies)):
        if "@" in raw_strat or "=" in raw_strat:
            spec = parse_strategy_spec(raw_strat)
            strat_name = spec.name
            if spec.engine:
                w = _copy_worker_slot(
                    w,
                    engine=spec.engine,
                    engine_explicit=True,
                    model=spec.model,
                    hint=spec.hint,
                )
            slot_model = spec.model or getattr(w, "_model_override", None) or model
            slot_hint = spec.hint or getattr(w, "_strategy_hint", None)
        else:
            strat_name = raw_strat
            slot_model = getattr(w, "_model_override", None) or model
            slot_hint = getattr(w, "_strategy_hint", None)
        if strategy_hints and idx < len(strategy_hints) and strategy_hints[idx]:
            slot_hint = strategy_hints[idx]
        if getattr(w, "_engine_explicit", False):
            any_explicit_engine = True
        resolved_workers.append(w)
        resolved_strategies.append(strat_name)
        resolved_hints.append(slot_hint)
        resolved_models.append(slot_model)

    if any_explicit_engine:
        problems = workers_missing_engine(resolved_workers)
        if problems:
            raise DispatchError("Requested engine is not available on every worker:\n  " + "\n  ".join(problems))
    if base_branch:
        problems = workers_missing_feature(resolved_workers, "base_branch")
        if problems:
            raise DispatchError(
                f"Can't start from base branch {base_branch!r} on every worker:\n  " + "\n  ".join(problems)
            )
    if protect_paths:
        problems = workers_missing_feature(resolved_workers, "protect", "a version with the protect feature")
        if problems:
            raise DispatchError("Can't protect paths on every worker:\n  " + "\n  ".join(problems))
    if any(resolved_hints):
        problems = workers_missing_feature(resolved_workers, "strategy_hint", "a version with the strategy_hint feature")
        if problems:
            raise DispatchError("Can't send strategy hints to every worker:\n  " + "\n  ".join(problems))
    if timeout is not None:
        problems = workers_missing_feature(resolved_workers, "timeout", "a version with the timeout feature")
        if problems:
            raise DispatchError("Can't set a per-dispatch timeout on every worker:\n  " + "\n  ".join(problems))

    run_id = new_run_id()
    run = RunState(
        run_id=run_id,
        prompt=prompt.strip(),
        repo_url=repo_url,
        base_branch=base_branch,
        protect_paths=protect_paths,
        test_command=test_command,
        bench_command=bench_command,
        model=model,
        timeout=timeout,
        workers=[],
    )

    count = len(resolved_workers)

    def launch(index: int, worker: WorkerNode) -> WorkerRunState:
        return start_worker_job(
            worker,
            run_id=run_id,
            # Own ID per slot, so one worker can run several strategies of the same run.
            remote_run_id=f"{run_id}-{index + 1}",
            repo_url=repo_url,
            prompt=prompt.strip(),
            strategy=resolved_strategies[index],
            strategy_hint=resolved_hints[index],
            strategy_index=index + 1,
            strategy_count=count,
            test_command=test_command,
            bench_command=bench_command,
            model=resolved_models[index],
            base_branch=base_branch,
            protect_paths=protect_paths,
            timeout=timeout,
        )

    results: list[Optional[WorkerRunState]] = [None] * count
    with ThreadPoolExecutor(max_workers=max(1, count)) as pool:
        futures = {
            pool.submit(launch, index, worker): index
            for index, worker in enumerate(resolved_workers)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001
                worker = resolved_workers[index]
                results[index] = WorkerRunState(
                    worker_id=worker.id,
                    host=worker.host,
                    port=worker.port,
                    agent_engine=worker.agent_engine,
                    strategy=resolved_strategies[index],
                    strategy_hint=resolved_hints[index],
                    model=resolved_models[index],
                    branch="",
                    status="failed",
                    error=str(exc),
                )

    run.workers = [item for item in results if item is not None]
    run.save()
    return run
