"""``hive mcp``: the hive tools for an MCP-capable agent, over stdio.

The agent can use the fleet (check, dispatch, status, logs, cancel, merge into a
local clone) but cannot change who is in it: keygen is not exposed, and adding
workers needs the human's answer through MCP elicitation, which the agent never
sees or supplies. Nothing here pushes to origin.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, create_model

from mcp.server.mcpserver import Context, Elicit, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from hivemind.config.cluster import (
    SUPPORTED_ENGINES,
    ClusterConfig,
    WorkerNode,
    get_fleet_key,
    merge_workers,
    validate_worker_host,
)
from hivemind.discovery.prober import SystemProber
from hivemind.discovery.scanner import DiscoveredDevice, NetworkScanner
from hivemind.discovery.tailscale import TailscaleScanner
from hivemind.runner.dispatch import (
    DispatchError,
    assign_strategies,
    dispatch_run,
    resolve_repo_url,
    select_workers,
)
from hivemind.runner.merge import (
    MergeError,
    default_winner,
    mergeable_workers,
    pick_worker,
    prepare_merge,
    run_merge,
    worker_label,
)
from hivemind.runner.remote import LOG_FILES, cancel_worker_job, fetch_worker_log
from hivemind.runner.state import RunState
from hivemind.runner.status import TERMINAL_STATUSES, refresh_run, worker_node_for

INSTRUCTIONS = """\
HiveMind runs one coding task on several worker machines at once, each with a different
strategy on its own exp/* branch, then merges the best branch into a local clone.

Typical flow: hive_check -> hive_dispatch -> hive_status (repeat until every strategy has
finished) -> hive_logs for failures -> hive_merge (preview) -> hive_merge confirm=true.
Strategies are short names that tell the workers how to differ (e.g. "iterative",
"memoized"); give a test command so success is measured, not guessed.
Nothing here pushes: the human reviews the merge and runs git push. Never edit the
test command's files to make a run pass.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

app = MCPServer("hive", instructions=INSTRUCTIONS, log_level="WARNING")


def _config() -> ClusterConfig:
    try:
        return ClusterConfig.load()
    except FileNotFoundError:
        raise ToolError("No cluster file (~/.hivemind/cluster.json). Add workers with hive_discover first.")
    except (ValueError, OSError) as exc:
        raise ToolError(f"Could not read ~/.hivemind/cluster.json: {exc}") from exc


def _require_key() -> None:
    if not get_fleet_key():
        raise ToolError(
            "No fleet key on this machine. The human must run `hive keygen` in a terminal, "
            "then `hivemind-worker key set` on each worker."
        )


def _run_context(run_id: Optional[str]) -> tuple[RunState, dict[str, WorkerNode]]:
    _require_key()
    try:
        run = RunState.load(run_id) if run_id else RunState.load_latest()
    except (FileNotFoundError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    try:
        workers = ClusterConfig.load().workers
    except FileNotFoundError:
        workers = []
    return refresh_run(run, {w.id: w for w in workers}), {w.id: w for w in workers}


def _run_summary(run: RunState) -> dict[str, Any]:
    """The run as JSON, without log tails (hive_logs has those)."""
    data = run.model_dump(exclude={"workers": {"__all__": {"log_tail"}}})
    for item, worker in zip(data["workers"], run.workers):
        item["label"] = worker_label(run, worker)
    data["finished"] = all(w.status in TERMINAL_STATUSES for w in run.workers)
    return data


def _in_parallel(fn, items: list) -> list:
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(32, len(items))) as pool:
        return list(pool.map(fn, items))


@app.tool(annotations=READ_ONLY)
def hive_list() -> dict[str, Any]:
    """List the workers in the cluster file and the default repo_url. Does not contact workers."""
    config = _config()
    return {"cluster_name": config.cluster_name, "repo_url": config.repo_url,
            "workers": [w.model_dump() for w in config.workers]}


@app.tool(annotations=READ_ONLY)
def hive_check() -> dict[str, Any]:
    """Check every worker: reachable, ready or busy, git, installed engines, version."""
    _require_key()
    config = _config()
    probes = _in_parallel(lambda w: SystemProber.probe_daemon(w.host, w.port, 3.5), config.workers)
    workers = []
    for worker, (health, _tags, err) in zip(config.workers, probes):
        engines = (health or {}).get("engines", [])
        wanted = "claude-code" if worker.agent_engine == "claude" else worker.agent_engine
        workers.append({
            "id": worker.id,
            "online": health is not None,
            "status": (health or {}).get("status"),
            "version": (health or {}).get("version"),
            "git_ready": (health or {}).get("git_ready"),
            "engine": worker.agent_engine,
            "engine_installed": wanted in engines if health else None,
            "engines": engines,
            "error": err,
        })
    return {"workers": workers, "all_ready": all(w["online"] for w in workers)}


@app.tool(annotations=READ_ONLY)
def hive_runs(limit: int = 20) -> dict[str, Any]:
    """Saved runs, newest first, with status counts as of the last poll. Does not contact workers."""
    out = []
    for run in RunState.load_all()[: max(1, limit)]:
        counts: dict[str, int] = {}
        for w in run.workers:
            counts[w.status] = counts.get(w.status, 0) + 1
        out.append({"run_id": run.run_id, "created_at": run.created_at, "prompt": run.prompt[:200],
                    "strategies": len(run.workers), "statuses": counts})
    return {"runs": out}


@app.tool()
def hive_dispatch(
    prompt: str,
    strategies: Optional[list[str]] = None,
    workers: Optional[list[str]] = None,
    repo: Optional[str] = None,
    test: Optional[str] = None,
    bench: Optional[str] = None,
    model: Optional[str] = None,
    engine: Optional[str] = None,
    base: Optional[str] = None,
) -> dict[str, Any]:
    """Start a run: one strategy per entry in `strategies` (default one per worker), spread over `workers`
    (default all). `test` is run after the agent (e.g. "pytest -q"); `bench` must print JSON metrics.
    `base` is a branch on origin to start from. Returns the run, including its run_id."""
    _require_key()
    config = _config()
    if engine and engine not in SUPPORTED_ENGINES:
        raise ToolError(f"Unknown engine {engine!r}; use one of {', '.join(SUPPORTED_ENGINES)}")
    try:
        slot_workers, names = assign_strategies(select_workers(config, workers), strategies or [])
        repo_url = resolve_repo_url(config, repo)
        if engine:
            slot_workers = [w.model_copy(update={"agent_engine": engine}) for w in slot_workers]
        run = dispatch_run(config, prompt, repo_url=repo_url, workers=slot_workers, strategies=names,
                           test_command=test, bench_command=bench, model=model, base_branch=base)
    except DispatchError as exc:
        raise ToolError(str(exc)) from exc
    return _run_summary(run)


@app.tool(annotations=READ_ONLY)
def hive_status(run_id: Optional[str] = None) -> dict[str, Any]:
    """Poll the workers once and return the run (default: latest). `finished` is true when every
    strategy ended (succeeded, failed, no_changes or cancelled). Call again to keep waiting."""
    run, _ = _run_context(run_id)
    return _run_summary(run)


@app.tool(annotations=READ_ONLY)
def hive_logs(
    run_id: Optional[str] = None,
    worker: Optional[str] = None,
    full: bool = False,
    file: Literal["agent", "test", "bench", "wrapper"] = "agent",
) -> dict[str, Any]:
    """Logs per strategy (all, or `worker` as id or id:strategy). By default the last lines each worker
    reports; `full` fetches the end of `file` (up to 256 KiB)."""
    run, workers_by_id = _run_context(run_id)
    try:
        targets = [pick_worker(run, worker, run.workers)] if worker else run.workers
    except MergeError as exc:
        raise ToolError(str(exc)) from exc
    fetched = _in_parallel(
        lambda w: fetch_worker_log(worker_node_for(w, workers_by_id), w.remote_run_dir or run.run_id, file, 256 * 1024),
        targets,
    ) if full else [None] * len(targets)
    out = []
    for w, got in zip(targets, fetched):
        item = {"label": worker_label(run, w), "status": w.status, "error": w.error, "log": w.log_tail or ""}
        if got is not None:
            payload, err = got
            if payload is None:
                item["log_error"] = err
            else:
                item.update(log=payload["content"], truncated=bool(payload.get("truncated")), size=payload.get("size"))
        out.append(item)
    return {"logs": out}


@app.tool()
def hive_cancel(run_id: Optional[str] = None, worker: Optional[str] = None) -> dict[str, Any]:
    """Cancel the strategies of a run (default: latest) that are still queued or running."""
    run, workers_by_id = _run_context(run_id)
    try:
        targets = [pick_worker(run, worker, run.workers)] if worker else run.workers
    except MergeError as exc:
        raise ToolError(str(exc)) from exc
    targets = [w for w in targets if w.status not in TERMINAL_STATUSES]
    results = _in_parallel(
        lambda w: cancel_worker_job(worker_node_for(w, workers_by_id), w.remote_run_dir or run.run_id), targets
    )
    return {"cancelled": [{"label": worker_label(run, w), "ok": ok, "message": msg}
                          for w, (ok, msg) in zip(targets, results)]}


@app.tool()
def hive_merge(
    repo_path: str,
    run_id: Optional[str] = None,
    worker: Optional[str] = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Merge a winning strategy into the local clone at `repo_path` (whose origin must be the run's repo).
    Without `confirm` it only previews the commits and diff stat. With `confirm=true` it checks out the
    run's base branch and merges with --no-ff. Never pushes. `worker` (id or id:strategy) defaults to the
    first succeeded strategy; a failed one is only merged when named explicitly."""
    path = Path(repo_path).expanduser()
    if not path.is_dir():
        raise ToolError(f"repo_path {repo_path!r} is not a directory")
    run, _ = _run_context(run_id)
    candidates = mergeable_workers(run)
    if not candidates:
        raise ToolError("No worker branches are available to merge yet.")
    try:
        if worker:
            winner = pick_worker(run, worker, candidates)
        else:
            winner = default_winner(candidates)
            if winner.status != "succeeded":
                raise MergeError("No strategy succeeded. Name a worker to merge one anyway.")
        plan = prepare_merge(run, winner, cwd=str(path))
        result = {"winner": worker_label(run, winner), "branch": winner.branch, "into": plan.base,
                  "commits": plan.commits, "diff_stat": plan.diff_stat}
        if not confirm:
            return {**result, "merged": False, "next": "Call again with confirm=true to merge."}
        run_merge(run, plan, cwd=str(path))
    except MergeError as exc:
        raise ToolError(str(exc)) from exc
    return {**result, "merged": True, "next": f"Merged locally into {plan.base}. The human reviews and pushes."}


# ---------------------------------------------------------------- discovery


def _scan(mode: str, port: int) -> list[DiscoveredDevice]:
    found: list[DiscoveredDevice] = []
    if mode in ("tailscale", "both"):
        found.extend(TailscaleScanner.discover_peers())
    if mode in ("local", "both"):
        interfaces = NetworkScanner.get_local_interfaces()
        if interfaces:
            iface = interfaces[0]  # shortcut: first interface only; `hive discover --local` lets you pick
            seen = {d.ip for d in found}
            found.extend(d for d in NetworkScanner.scan_subnet(iface["network"], daemon_port=port,
                                                               interface_ip=iface["ip"]) if d.ip not in seen)
    return [d for d in found if not d.is_local_host]


def _valid_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ToolError(f"Invalid port {port!r}; must be 1..65535.")
    return port


@app.tool(annotations=READ_ONLY)
def hive_discover(mode: Literal["auto", "tailscale", "local", "both"] = "auto", port: int = 7422) -> dict[str, Any]:
    """Find machines that could become workers, over Tailscale and/or the local subnet ("auto": Tailscale
    when connected, else local). Read-only: no fleet key is sent and nothing is saved. Add machines with
    hive_add_workers, which asks the human."""
    _valid_port(port)
    if mode == "auto":
        mode = "tailscale" if TailscaleScanner.backend_state() == "Running" else "local"
    elif mode in ("tailscale", "both") and TailscaleScanner.backend_state() != "Running":
        raise ToolError("Tailscale is not connected on this machine; use mode='local'.")
    devices = _scan(mode, port)
    presence = _in_parallel(lambda d: SystemProber.probe_presence(d.ip, port), devices)
    try:
        configured = {w.host for w in ClusterConfig.load().workers}
    except (FileNotFoundError, ValueError):
        configured = set()
    return {"mode": mode, "devices": [
        {"ip": d.ip, "hostname": d.hostname, "os": d.os_type, "in_cluster": d.ip in configured,
         "worker_detected": info is not None, "worker_version": (info or {}).get("version"), "note": err}
        for d, (info, err) in zip(devices, presence)
    ]}


def _valid_hosts(ips: list[str]) -> list[str]:
    if not ips:
        raise ToolError("Pass at least one IP from hive_discover.")
    try:
        return list(dict.fromkeys(validate_worker_host(ip) for ip in ips))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _ask_human(ips: list[str], port: int, ctx: Context):
    """Resolver for hive_add_workers: the human ticks machines in the client's own form."""
    hosts = _valid_hosts(ips)  # before asking: never show the human a host that would be rejected
    _valid_port(port)
    caps = ctx.client_capabilities
    if not (caps and caps.elicitation):
        return None
    _require_key()
    form = create_model(
        "AddWorkers",
        **{f"add_{i}": (bool, Field(default=False, title=f"Add {host}")) for i, host in enumerate(hosts)},
    )
    return Elicit(
        message=(
            "An AI agent asks to add these machines to your HiveMind fleet. Each machine you tick is "
            "sent the fleet key, which controls every worker. Tick only machines you own."
        ),
        schema=form,
    )


def _worker_id(health: dict, ip: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", str(health.get("hostname") or "").split(".")[0].lower()).strip("-")
    base = base or "worker-" + ip.replace(".", "-").replace(":", "-")
    worker_id, n = base, 2
    while worker_id in taken:
        worker_id, n = f"{base}-{n}", n + 1
    return worker_id


@app.tool()
def hive_add_workers(
    ips: list[str],
    approval: Annotated[Optional[BaseModel], Resolve(_ask_human)],
    port: int = 7422,
) -> dict[str, Any]:
    """Ask the human to add machines (IPs from hive_discover) as workers. The human ticks each one in a
    form shown by your MCP client; only ticked machines get the fleet key and are saved to cluster.json.
    If the client cannot show forms, nothing is added and the human must run `hive discover` instead."""
    hosts = _valid_hosts(ips)
    _valid_port(port)
    if approval is None:
        return {"added": [], "needs_human": True,
                "message": "This MCP client cannot ask the human directly. Ask them to run "
                           f"`hive discover` in a terminal and select: {', '.join(hosts)}."}
    chosen = [h for i, h in enumerate(hosts) if getattr(approval, f"add_{i}", False) is True]
    if not chosen:
        return {"added": [], "message": "The human did not tick any machine."}

    try:
        existing = ClusterConfig.load()
    except FileNotFoundError:
        existing = ClusterConfig()
    taken = {w.id for w in existing.workers if (w.host, w.port) not in {(h, port) for h in chosen}}
    added, failed = [], []
    for host, (health, tags, err) in zip(chosen, _in_parallel(lambda h: SystemProber.probe_daemon(h, port), chosen)):
        if health is None:
            failed.append({"ip": host, "error": err})
            continue
        engine = "aider" if "engine:aider" in tags else ("agy" if "engine:agy" in tags else "claude-code")
        node = WorkerNode(id=_worker_id(health, host, taken), host=host, port=port,
                          agent_engine=engine, capabilities=tags)
        taken.add(node.id)
        added.append(node)
    if added:
        existing.workers = merge_workers(existing.workers, added)
        existing.save()
    return {"added": [w.model_dump() for w in added], "failed": failed}


def main() -> None:
    app.run()
