import json
from pathlib import Path
import secrets
import sys
import click
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt

from hivemind.discovery.scanner import NetworkScanner, DiscoveredDevice
from hivemind.discovery.prober import SystemProber
from hivemind.discovery.tailscale import TailscaleScanner
from hivemind.config.cluster import ClusterConfig, WorkerNode, get_default_cluster_path, get_hivemind_home, get_fleet_key
from hivemind.runner.dispatch import (
    DispatchError,
    assign_strategies,
    dispatch_run,
    resolve_repo_url,
    select_workers,
    validate_branch_ref,
)
from hivemind.runner.state import RunState, WorkerRunState
from hivemind.runner.status import (
    TERMINAL_STATUSES,
    local_base_branch,
    local_git,
    local_repo_origin,
    local_working_tree_dirty,
    normalize_git_url,
    refresh_run,
)

console = Console()


def _load_config(cluster: Optional[str]) -> Optional[ClusterConfig]:
    try:
        return ClusterConfig.load(cluster)
    except FileNotFoundError:
        default_path = cluster if cluster else get_default_cluster_path()
        console.print(
            f"[bold red]Error:[/bold red] Cluster file '{default_path}' not found. "
            "Run [bold cyan]hive discover[/bold cyan] first."
        )
        return None


def _require_fleet_key() -> bool:
    """Workers always require a fleet key; fail fast instead of sending keyless requests."""
    if get_fleet_key():
        return True
    console.print(
        "[bold red]Error:[/bold red] No fleet key found in ~/.hivemind/fleet.key or HIVEMIND_FLEET_KEY.\n"
        "Run [bold cyan]hive keygen[/bold cyan], then [bold white]hivemind-worker key set[/bold white] on each worker."
    )
    return False


def _load_run(run_id: Optional[str]) -> Optional[RunState]:
    try:
        return RunState.load(run_id) if run_id else RunState.load_latest()
    except FileNotFoundError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return None


def _status_style(status: str) -> str:
    mapping = {
        "succeeded": "[bold green]succeeded[/bold green]",
        "failed": "[bold red]failed[/bold red]",
        "no_changes": "[yellow]no_changes[/yellow]",
        "cancelled": "[dim yellow]cancelled[/dim yellow]",
        "running": "[bold yellow]running[/bold yellow]",
        "starting": "[yellow]starting[/yellow]",
        "unknown": "[dim]unknown[/dim]",
    }
    return mapping.get(status, escape(status))


def _format_benchmark(worker: WorkerRunState) -> str:
    data = worker.benchmark_data
    if data is not None:
        if isinstance(data, dict):
            if set(data.keys()) == {"raw_output"}:
                text = str(data.get("raw_output") or "").strip()
            else:
                text = ", ".join(f"{k}={v}" for k, v in data.items())
        else:
            text = json.dumps(data)
        text = text.encode("ascii", errors="ignore").decode("ascii").replace("\n", " ").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        return escape(text) if text else "[dim]-[/dim]"
    if worker.benchmark_path:
        return escape(str(worker.benchmark_path))
    if worker.bench_exit_code is not None:
        if worker.bench_exit_code == 0:
            return "[green]ok[/green]"
        return f"[bold red]fail ({worker.bench_exit_code})[/bold red]"
    return "[dim]-[/dim]"


def _merge_workers(existing: list[WorkerNode], updated: list[WorkerNode]) -> list[WorkerNode]:
    """Merge newly configured workers into an existing worker list without losing unselected ones."""
    merged = list(existing)
    for new_w in updated:
        replaced = False
        for idx, old_w in enumerate(merged):
            if old_w.id == new_w.id or (old_w.host == new_w.host and old_w.port == new_w.port):
                merged[idx] = new_w
                replaced = True
                break
        if not replaced:
            merged.append(new_w)
    return merged


def _print_run_table(run: RunState, title: Optional[str] = None) -> None:
    show_bench = bool(run.bench_command) or any(
        w.benchmark_data is not None or w.bench_exit_code is not None or w.benchmark_path
        for w in run.workers
    )
    table = Table(
        title=title or f"Run {run.run_id}",
        border_style="cyan",
    )
    table.add_column("Worker", style="bold white")
    table.add_column("Strategy", style="magenta")
    table.add_column("Branch", style="green")
    table.add_column("Status", justify="center")
    table.add_column("Tests", justify="center")
    if show_bench:
        table.add_column("Benchmark", justify="center", style="cyan")
    table.add_column("Details", style="dim")

    for worker in run.workers:
        if worker.test_exit_code is None:
            tests = "[dim]-[/dim]"
        elif worker.test_exit_code == 0:
            tests = "[bold green]pass[/bold green]"
        else:
            tests = f"[bold red]fail ({worker.test_exit_code})[/bold red]"
        details = worker.error or worker.log_tail or ""
        if len(details) > 120:
            details = details[-120:]
        # Strip non-ASCII characters (e.g. aider token count symbols) to avoid
        # UnicodeEncodeError on Windows cp1252 consoles
        details = details.encode("ascii", errors="ignore").decode("ascii")
        row = [
            escape(worker.worker_id),
            escape(worker.strategy),
            escape(worker.branch or "-"),
            _status_style(worker.status),
            tests,
        ]
        if show_bench:
            row.append(_format_benchmark(worker))
        row.append(escape(details.replace("\n", " ")))
        table.add_row(*row)
    console.print()
    console.print(table)
    console.print()


@click.group()
@click.version_option(package_name="hivemind", prog_name="hive")
def cli():
    """HiveMind: Distributed AI Coding Agent Fleet Orchestrator."""
    pass


@cli.command(name="keygen")
@click.option("--force", "-f", is_flag=True, default=False, help="Overwrite existing fleet key if present.")
def keygen_cmd(force: bool):
    """Generate a shared Fleet API key for securing worker nodes."""
    key_file = get_hivemind_home() / "fleet.key"
    if key_file.exists() and not force:
        existing_key = key_file.read_text(encoding="utf-8").strip()
        console.print(Panel.fit(
            f"[bold yellow]Existing Fleet API Key Found:[/bold yellow]\n\n"
            f"[bold green]{existing_key}[/bold green]\n\n"
            f"[dim]Stored at: {key_file}[/dim]\n\n"
            f"[dim]Use [bold white]hive keygen --force[/bold white] to generate a new key.[/dim]",
            title="HiveMind Fleet Key",
            border_style="yellow"
        ))
        return

    new_key = f"hive-key-{secrets.token_hex(20)}"
    get_hivemind_home().mkdir(parents=True, exist_ok=True)
    key_file.write_text(new_key, encoding="utf-8")

    console.print(Panel.fit(
        f"[bold green][OK] New Fleet API Key Generated Successfully![/bold green]\n\n"
        f"[bold white]Key:[/bold white] [bold cyan]{new_key}[/bold cyan]\n\n"
        f"[dim]Saved to:[/dim] [white]{key_file}[/white]\n\n"
        f"[bold yellow]Next Step on Worker Nodes:[/bold yellow]\n"
        f"Run on each worker machine to authorize it:\n"
        f"  [bold white]hivemind-worker start --key {new_key} --save[/bold white]",
        title="HiveMind Fleet Key",
        border_style="green"
    ))


@cli.command(name="discover")
@click.option("--output", "-o", default=None, help="Path to save cluster config (default: ~/.hivemind/cluster.json).")
@click.option("--local", "-l", "use_local", is_flag=True, default=False, help="Discover devices over local Wi-Fi / Ethernet subnet.")
@click.option("--tailscale", "-t", "use_tailscale", is_flag=True, default=False, help="Discover devices over Tailscale mesh network.")
@click.option("--port", "-p", "daemon_port", default=7422, type=int, show_default=True, help="Worker daemon port to probe.")
def discover_cmd(output: Optional[str], use_local: bool, use_tailscale: bool, daemon_port: int):
    """Scan Tailscale mesh network or local subnet for candidate worker machines.

    Devices are probed without credentials; the fleet key is only sent to the
    devices you select.
    """
    target_path = Path(output) if output else get_default_cluster_path()
    existing_config: Optional[ClusterConfig] = None
    if target_path.exists():
        try:
            existing_config = ClusterConfig.load(target_path)
        except Exception:
            existing_config = None

    ts_available = TailscaleScanner.is_available()

    # Determine discovery mode
    mode = "tailscale" if use_tailscale else ("local" if use_local else None)
    if not mode:
        if ts_available:
            console.print(Panel.fit(
                "[bold cyan]HiveMind Fleet Discovery[/bold cyan]\n"
                "[dim]Tailscale mesh network and local network detected.[/dim]",
                border_style="cyan"
            ))
            console.print("[bold yellow]Choose discovery method:[/bold yellow]")
            console.print("  [1] [bold green]Tailscale Mesh Fleet[/bold green] [dim](instant peer lookup across all locations)[/dim]")
            console.print("  [2] [bold cyan]Local Network Subnet Scan[/bold cyan] [dim](Wi-Fi / Ethernet subnet sweep)[/dim]")
            console.print("  [3] [bold magenta]Both[/bold magenta] [dim](combine Tailscale peers and local subnet devices)[/dim]")
            choice = Prompt.ask("Select discovery method", choices=["1", "2", "3"], default="1")
            if choice == "1":
                mode = "tailscale"
            elif choice == "2":
                mode = "local"
            else:
                mode = "both"
        else:
            mode = "local"

    discovered: list[DiscoveredDevice] = []
    source_label = "Local Network"

    # 1. Tailscale Discovery
    if mode in ("tailscale", "both"):
        with console.status("[bold green]Querying Tailscale daemon for active peers...[/bold green]"):
            ts_peers = TailscaleScanner.discover_peers(probe_ssh=False)
            discovered.extend(ts_peers)
        source_label = "Tailscale Mesh" if mode == "tailscale" else "Tailscale + Local"

    # 2. Local Subnet Discovery
    if mode in ("local", "both"):
        interfaces = NetworkScanner.get_local_interfaces()
        if not interfaces:
            if not discovered:
                console.print("[bold red]Error:[/bold red] No active local network interfaces found.")
                return
        else:
            chosen_iface = interfaces[0]
            if len(interfaces) > 1 and mode == "local":
                console.print("\n[bold yellow]Multiple local network interfaces detected:[/bold yellow]")
                for i, iface in enumerate(interfaces, 1):
                    console.print(f"  [{i}] {iface['name']} - IP: {iface['ip']} (Subnet: {iface['cidr']})")
                choice = Prompt.ask("Select interface to scan", default="1")
                try:
                    chosen_iface = interfaces[int(choice) - 1]
                except Exception:
                    chosen_iface = interfaces[0]

            console.print(f"\n[bold green]Scanning subnet:[/bold green] {chosen_iface['cidr']} via [cyan]{chosen_iface['name']}[/cyan] ({chosen_iface['ip']})...\n")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console
            ) as progress:
                scan_task = progress.add_task("Probing local devices...", total=100)

                def update_progress(current, total):
                    progress.update(scan_task, completed=int((current / total) * 100))

                local_devs = NetworkScanner.scan_subnet(
                    chosen_iface["network"],
                    progress_callback=update_progress
                )
                # Deduplicate by IP
                existing_ips = {d.ip for d in discovered}
                for ld in local_devs:
                    if ld.ip not in existing_ips:
                        discovered.append(ld)
            source_label = f"Local Subnet ({chosen_iface['cidr']})" if mode == "local" else source_label

    if not discovered:
        console.print("[yellow]No active devices discovered.[/yellow]")
        return

    # Detect worker daemons WITHOUT credentials (in parallel). The fleet key is only
    # sent later, to devices the user explicitly selects.
    presence_map: dict[str, tuple] = {}
    remote_devices = [d for d in discovered if not d.is_local_host]
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Looking for HiveMind worker daemons (port {daemon_port})...", total=len(remote_devices))
        with ThreadPoolExecutor(max_workers=min(32, max(1, len(remote_devices)))) as pool:
            futures = {
                pool.submit(SystemProber.probe_presence, dev.ip, daemon_port): dev.ip
                for dev in remote_devices
            }
            for future in as_completed(futures):
                presence_map[futures[future]] = future.result()
                progress.advance(task)

    # Display results table
    table = Table(title=f"Discovered Devices ({source_label})", border_style="blue")
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("IP Address", style="bold white")
    table.add_column("Hostname", style="green")
    table.add_column(f"Worker Daemon (Port {daemon_port})", justify="center")
    table.add_column("OS", style="cyan")
    table.add_column("Type", style="dim")

    for idx, dev in enumerate(discovered, 1):
        if dev.is_local_host:
            daemon_status = "[dim]-- (Master Host)[/dim]"
            profile_str = "Master Node"
            device_type = "Master Host"
        else:
            info, err = presence_map.get(dev.ip, (None, None))
            if info:
                daemon_status = f"[bold green]DETECTED (v{escape(str(info.get('version', '?')))})[/bold green]"
            else:
                daemon_status = "[dim red]NOT INSTALLED / OFFLINE[/dim red]"
            profile_str = f"[dim]{escape(dev.os_type or 'unknown')}[/dim]"
            device_type = "Tailscale Peer" if dev.ip.startswith("100.") else "Local Machine"

        table.add_row(
            str(idx),
            dev.ip,
            escape(dev.hostname),
            daemon_status,
            profile_str,
            device_type
        )

    console.print("\n", table, "\n")
    console.print("[dim]Devices were probed without credentials. The fleet key is only sent to the devices you select.[/dim]\n")

    # Interactive device selection
    selection_str = Prompt.ask(
        "[bold yellow]Select device numbers to add to the cluster[/bold yellow] (e.g. '1, 2' or 'all', 'q' to quit)",
        default="all"
    )

    if selection_str.strip().lower() in ["q", "quit", "exit"]:
        console.print("[dim]Discovery aborted.[/dim]")
        return

    selected_devices: list[DiscoveredDevice] = []
    if selection_str.strip().lower() == "all":
        # Default to selecting remote devices with active worker daemons
        ready = [d for d in discovered if not d.is_local_host and presence_map.get(d.ip, (None, None))[0] is not None]
        selected_devices = ready if ready else [d for d in discovered if not d.is_local_host]
    else:
        for part in selection_str.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(discovered):
                    selected_devices.append(discovered[idx])

    if not selected_devices:
        console.print("[yellow]No devices selected. Exiting.[/yellow]")
        return

    console.print(f"\n[bold green]Configuring {len(selected_devices)} worker node(s):[/bold green]\n")

    has_key = bool(get_fleet_key())
    if not has_key:
        console.print(
            "[yellow]No fleet key found, so worker capabilities can't be read. "
            "Run [bold cyan]hive keygen[/bold cyan] first for auto-detected engines and hardware.[/yellow]\n"
        )

    # Build cluster config
    workers: list[WorkerNode] = []
    for i, dev in enumerate(selected_devices, 1):
        default_id = f"worker-{i}" if dev.hostname in ("Unknown", "Master") else dev.hostname.split(".")[0].lower()
        console.print(f"[bold cyan]--- Device #{i} ({escape(dev.hostname)} @ {dev.ip}) ---[/bold cyan]")

        worker_id = Prompt.ask("  Worker ID", default=default_id)
        worker_port = IntPrompt.ask("  Daemon Port", default=daemon_port)

        # Authenticated probe: only devices the user selected ever receive the fleet key.
        if has_key:
            h_data, detected_tags, probe_err = SystemProber.probe_daemon(dev.ip, port=worker_port)
        else:
            h_data, detected_tags, probe_err = None, [], "No fleet key configured on this machine"

        if h_data:
            default_caps_str = ", ".join(detected_tags)
            console.print(f"  [bold green][OK] Worker Daemon Active:[/bold green] [cyan]{escape(default_caps_str)}[/cyan]")
        else:
            default_caps_str = ""
            console.print(f"  [yellow][!] Daemon Probe Warning:[/yellow] [dim]{escape(probe_err or 'No response on port ' + str(worker_port))}[/dim]")

        # Suggest workspace directory based on OS
        is_windows_peer = dev.os_type == "windows" or "os-windows" in detected_tags
        if is_windows_peer:
            default_ws = "C:\\hive-workspace"
        else:
            default_ws = "~/hive-workspace"

        workspace_dir = Prompt.ask("  Workspace Directory", default=default_ws)

        # Auto-suggest default engine if detected
        default_engine = "aider" if "engine:aider" in detected_tags else ("agy" if "engine:agy" in detected_tags else "claude-code")
        agent_engine = Prompt.ask(
            "  AI Coding CLI Engine",
            choices=["claude-code", "agy", "aider", "custom"],
            default=default_engine
        )

        capabilities_raw = Prompt.ask(
            "  Capabilities tags (comma-separated)",
            default=default_caps_str if default_caps_str else "standard-cpu"
        )
        caps = [c.strip() for c in capabilities_raw.split(",") if c.strip()]

        workers.append(WorkerNode(
            id=worker_id,
            host=dev.ip,
            port=worker_port,
            workspace_dir=workspace_dir,
            agent_engine=agent_engine,
            capabilities=caps
        ))
        console.print()

    cluster_config = ClusterConfig(
        cluster_name=existing_config.cluster_name if existing_config else "hive-fleet",
        repo_url=existing_config.repo_url if existing_config else None,
        workers=_merge_workers(existing_config.workers, workers) if existing_config else workers,
    )

    saved_path = cluster_config.save(output)
    console.print(Panel.fit(
        f"[bold green][OK] Global cluster configuration saved successfully to [white]{saved_path}[/white][/bold green]\n"
        f"[dim]Run [bold white]hive check[/bold white] to verify daemon readiness across the fleet.[/dim]",
        border_style="green"
    ))


@cli.command(name="list")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file (default: ~/.hivemind/cluster.json).")
def list_cmd(cluster: Optional[str]):
    """List all configured worker nodes in the cluster."""
    try:
        config = ClusterConfig.load(cluster)
    except FileNotFoundError:
        default_path = cluster if cluster else get_default_cluster_path()
        console.print(f"[bold red]Error:[/bold red] Cluster file '{default_path}' not found. Run [bold cyan]hive discover[/bold cyan] first.")
        return

    table = Table(title=f"HiveMind Cluster: {config.cluster_name} ({len(config.workers)} Workers)", border_style="cyan")
    table.add_column("Worker ID", style="bold white")
    table.add_column("Daemon Address", style="green")
    table.add_column("Workspace Root", style="yellow")
    table.add_column("Engine", style="magenta")
    table.add_column("Capabilities", style="blue")

    for worker in config.workers:
        caps_str = ", ".join(worker.capabilities) if worker.capabilities else "none"
        table.add_row(
            worker.id,
            f"{worker.host}:{worker.port}",
            worker.workspace_dir,
            worker.agent_engine,
            caps_str
        )

    console.print()
    console.print(table)
    if config.repo_url:
        console.print(f"[dim]Shared origin (repo_url):[/dim] {config.repo_url}")
    else:
        console.print("[dim]No repo_url set in cluster config. Pass --repo on dispatch.[/dim]")
    console.print()


@cli.command(name="check")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file (default: ~/.hivemind/cluster.json).")
def check_cmd(cluster: Optional[str]):
    """Test HTTP Daemon connectivity, Git readiness, and AI CLI tools on all configured workers."""
    if not _require_fleet_key():
        sys.exit(1)
    config = _load_config(cluster)
    if config is None:
        return

    console.print(Panel.fit(
        f"[bold cyan]Checking HiveMind Cluster:[/bold cyan] {config.cluster_name} ({len(config.workers)} nodes)",
        border_style="cyan"
    ))

    table = Table(title="Cluster Readiness Status", border_style="green")
    table.add_column("Worker ID", style="bold white")
    table.add_column("Daemon Reachable", justify="center")
    table.add_column("Git Ready", justify="center")
    table.add_column("Agent Engine", justify="center")
    table.add_column("Live Hardware / Tools", style="cyan")

    probe_results: list[Optional[tuple]] = [None] * len(config.workers)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task("Auditing worker nodes...", total=len(config.workers))
        if config.workers:
            with ThreadPoolExecutor(max_workers=min(32, max(1, len(config.workers)))) as pool:
                futures = {
                    pool.submit(SystemProber.probe_daemon, worker.host, worker.port, 3.5): idx
                    for idx, worker in enumerate(config.workers)
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        probe_results[idx] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        probe_results[idx] = (None, [], str(exc))
                    progress.advance(task)

    for idx, worker in enumerate(config.workers):
        h_data, tags, err = probe_results[idx] or (None, [], "refused")

        if h_data:
            daemon_status = "[bold green]ONLINE (Ready)[/bold green]" if h_data.get("status") == "ready" else "[bold yellow]BUSY[/bold yellow]"
            git_status = "[bold green]YES[/bold green]" if h_data.get("git_ready") else "[bold red]NO (missing)[/bold red]"

            engines = h_data.get("engines", [])
            if worker.agent_engine in engines:
                engine_status = f"[bold green]{escape(worker.agent_engine)}[/bold green]"
            elif engines:
                engine_status = f"[yellow]{escape(worker.agent_engine)} (avail: {escape(', '.join(engines))})[/yellow]"
            else:
                engine_status = f"[bold red]MISSING ({escape(worker.agent_engine)})[/bold red]"

            live_caps_str = escape(", ".join(tags)) if tags else "[dim]none[/dim]"
        else:
            daemon_status = f"[bold red]OFFLINE ({escape(err or 'refused')})[/bold red]"
            git_status = "[dim red]UNKNOWN[/dim red]"
            engine_status = "[dim red]UNKNOWN[/dim red]"
            live_caps_str = "[dim red]unreachable[/dim red]"

        table.add_row(
            escape(worker.id),
            daemon_status,
            git_status,
            engine_status,
            live_caps_str
        )

    console.print("\n", table, "\n")


@cli.command(name="dispatch")
@click.argument("prompt", required=True)
@click.option("--repo", "repo_url", default=None, help="Git origin URL (default: repo_url from cluster.json).")
@click.option("--workers", "worker_ids_csv", default=None, help="Comma-separated worker ids to use (default: all).")
@click.option("--strategy", "strategies", multiple=True, help="Named strategy (repeatable). Default: strategy-1..N.")
@click.option("--test", "test_command", default=None, help="Command to run after agent finishes (e.g. 'pytest -q').")
@click.option("--bench", "bench_command", default=None, help="Benchmark command producing a JSON output.")
@click.option("--model", default=None, help="Model override for the worker engine.")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def dispatch_cmd(
    prompt: str,
    repo_url: Optional[str],
    worker_ids_csv: Optional[str],
    strategies: tuple[str, ...],
    test_command: Optional[str],
    bench_command: Optional[str],
    model: Optional[str],
    cluster: Optional[str],
):
    """Start a parallel coding job on worker machines."""
    if not _require_fleet_key():
        sys.exit(1)
    config = _load_config(cluster)
    if config is None:
        return

    worker_ids = [wid.strip() for wid in worker_ids_csv.split(",")] if worker_ids_csv else None
    try:
        selected = select_workers(config, worker_ids)
        assigned_strategies = assign_strategies(selected, strategies)
        resolved_repo = resolve_repo_url(config, repo_url)
    except DispatchError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return

    console.print(Panel.fit(
        f"[bold cyan]Dispatching Swarm Job[/bold cyan]\n"
        f"[white]{escape(prompt)}[/white]\n"
        f"[dim]Origin:[/dim] {escape(resolved_repo)}\n"
        f"[dim]Workers ({len(selected)}):[/dim] {escape(', '.join(w.id for w in selected))}",
        border_style="cyan",
    ))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Dispatching tasks to worker daemons...", total=len(selected))
        try:
            run = dispatch_run(
                config,
                prompt,
                repo_url=resolved_repo,
                workers=selected,
                strategies=assigned_strategies,
                test_command=test_command,
                bench_command=bench_command,
                model=model,
            )
        except DispatchError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            return
        progress.advance(task, advance=len(selected))

    console.print(Panel.fit(
        f"[bold green]Dispatched run {run.run_id}[/bold green]\n"
        f"[dim]Origin:[/dim] {escape(run.repo_url)}\n"
        "[dim]Follow progress with[/dim] [bold white]hive status[/bold white]",
        border_style="green",
    ))
    _print_run_table(run)


@cli.command(name="status")
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def status_cmd(run_id: Optional[str], cluster: Optional[str]):
    """Show live status of a dispatched run by reading result.json from workers."""
    if not _require_fleet_key():
        sys.exit(1)
    run = _load_run(run_id)
    if run is None:
        return

    config = None
    try:
        config = ClusterConfig.load(cluster)
    except FileNotFoundError:
        pass
    workers_by_id = {w.id: w for w in config.workers} if config else {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"Polling workers for run {run.run_id}...", total=len(run.workers))
        run = refresh_run(run, workers_by_id)
        progress.advance(task, advance=len(run.workers))

    console.print(f"[bold cyan]Run {run.run_id}[/bold cyan]  [dim]origin {escape(run.repo_url)}[/dim]")
    _print_run_table(run)

    all_done = all(w.status in TERMINAL_STATUSES for w in run.workers)
    if all_done and run.workers:
        console.print("[dim]All workers finished. Review branches, then[/dim] [bold white]hive merge[/bold white].\n")


@cli.command(name="merge")
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def merge_cmd(run_id: Optional[str], cluster: Optional[str]):
    """Fetch a winning exp/* branch from origin and merge it into local main."""
    if not _require_fleet_key():
        sys.exit(1)
    run = _load_run(run_id)
    if run is None:
        return

    config = None
    try:
        config = ClusterConfig.load(cluster)
    except FileNotFoundError:
        pass
    workers_by_id = {w.id: w for w in config.workers} if config else {}
    run = refresh_run(run, workers_by_id)
    _print_run_table(run, title=f"Pick a winner for {run.run_id}")

    finished = [
        w for w in run.workers
        if w.branch
        and w.pushed is not False
        and w.status not in ("no_changes", "cancelled", "starting", "running")
        and not (w.status == "failed" and w.pushed is not True and w.agent_exit_code is None)
    ]
    if not finished:
        console.print("[bold red]Error:[/bold red] No worker branches are available to merge yet.")
        return

    default_id = next((w.worker_id for w in finished if w.status == "succeeded"), finished[0].worker_id)
    choice = Prompt.ask("Worker id to merge", default=default_id)
    winner = next((w for w in finished if w.worker_id == choice), None)
    if winner is None:
        console.print(f"[bold red]Error:[/bold red] Unknown worker '{choice}'.")
        return

    try:
        validate_branch_ref(winner.branch)
    except DispatchError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return

    origin = local_repo_origin()
    if origin is None:
        console.print(
            "[bold red]Error:[/bold red] Run hive merge from a clone of the shared origin.\n"
            f"[dim]Expected repo:[/dim] {run.repo_url}"
        )
        return
    if normalize_git_url(origin) != normalize_git_url(run.repo_url):
        console.print(
            "[bold red]Error:[/bold red] This directory's origin does not match the run.\n"
            f"  local: {origin}\n"
            f"  run:   {run.repo_url}"
        )
        return

    if local_working_tree_dirty():
        console.print(
            "[bold red]Error:[/bold red] Local working tree has uncommitted changes. "
            "Commit or stash them before running [bold white]hive merge[/bold white]."
        )
        return

    try:
        local_git(
            "fetch",
            "origin",
            f"+refs/heads/{winner.branch}:refs/remotes/origin/{winner.branch}",
        )
        base = local_base_branch()
        has_local_base = (
            local_git("show-ref", "--verify", "--quiet", f"refs/heads/{base}", check=False).returncode == 0
        )
        compare_base = base if has_local_base else f"origin/{base}"
        log = local_git("log", "--oneline", f"{compare_base}..origin/{winner.branch}", check=False)
        diff = local_git("diff", "--stat", f"{compare_base}...origin/{winner.branch}", check=False)
    except RuntimeError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return

    console.print(Panel(
        (log.stdout or "[dim]no commits on branch[/dim]") + "\n" + (diff.stdout or ""),
        title=f"origin/{winner.branch} vs {compare_base}",
        border_style="blue",
    ))

    if not Confirm.ask(f"Merge [bold]{winner.branch}[/bold] into local [bold]{base}[/bold]?", default=True):
        console.print("[dim]Merge cancelled.[/dim]")
        return

    try:
        if has_local_base:
            local_git("checkout", base)
        else:
            local_git("checkout", "-B", base, f"origin/{base}")
        local_git("merge", "--no-ff", f"origin/{winner.branch}", "-m", f"hive: merge {winner.branch} from {run.run_id}")
    except RuntimeError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return

    console.print(Panel.fit(
        f"[bold green]Merged[/bold green] [white]{winner.branch}[/white] into local [white]{base}[/white].\n"
        "[dim]Push to origin when you are ready.[/dim]",
        border_style="green",
    ))


if __name__ == "__main__":
    cli()
