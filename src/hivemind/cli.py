import json
import os
from pathlib import Path
import secrets
import sys
import time
import click
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NoReturn, Optional
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt

from hivemind.discovery.scanner import NetworkScanner, DiscoveredDevice
from hivemind.discovery.prober import SystemProber
from hivemind.discovery.tailscale import TailscaleScanner
from hivemind.config.cluster import (
    SUPPORTED_ENGINES,
    ClusterConfig,
    WorkerNode,
    get_default_cluster_path,
    get_fleet_key,
    get_hivemind_home,
)
from hivemind.runner.dispatch import (
    DispatchError,
    assign_strategies,
    dispatch_run,
    resolve_repo_url,
    select_workers,
    validate_base_branch,
    validate_branch_ref,
)
from hivemind.runner.remote import LOG_FILES, cancel_worker_job, fetch_worker_log
from hivemind.runner.state import RunState, WorkerRunState, get_runs_dir
from hivemind.runner.status import (
    TERMINAL_STATUSES,
    local_base_branch,
    local_git,
    local_repo_origin,
    local_working_tree_dirty,
    normalize_git_url,
    refresh_run,
    worker_node_for,
)

console = Console()


def _fail(message: str) -> NoReturn:
    """Print an error and exit 1, so scripts can tell failure from success."""
    console.print(f"[bold red]Error:[/bold red] {message}")
    sys.exit(1)


def _load_config(cluster: Optional[str], *, required: bool = True) -> Optional[ClusterConfig]:
    """Load the cluster file. A missing file is fatal only when ``required``; an unreadable one always is."""
    path = cluster if cluster else get_default_cluster_path()
    try:
        return ClusterConfig.load(cluster)
    except FileNotFoundError:
        if not required:
            return None
        _fail(f"Cluster file '{escape(str(path))}' not found. Run [bold cyan]hive discover[/bold cyan] first.")
    except (ValueError, OSError) as exc:  # bad JSON or a field that fails validation
        _fail(f"Could not read cluster file '{escape(str(path))}': {escape(str(exc))}")


def _require_fleet_key() -> bool:
    """Workers always require a fleet key; fail fast instead of sending keyless requests."""
    if get_fleet_key():
        return True
    console.print(
        "[bold red]Error:[/bold red] No fleet key found in ~/.hivemind/fleet.key or HIVEMIND_FLEET_KEY.\n"
        "Run [bold cyan]hive keygen[/bold cyan], then [bold white]hivemind-worker key set[/bold white] on each worker."
    )
    return False


def _load_run(run_id: Optional[str]) -> RunState:
    try:
        return RunState.load(run_id) if run_id else RunState.load_latest()
    except (FileNotFoundError, ValueError) as exc:
        _fail(escape(str(exc)))


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


def _worker_label(run: RunState, worker: WorkerRunState) -> str:
    """Worker id, or id:strategy when that worker ran several strategies in this run."""
    if sum(w.worker_id == worker.worker_id for w in run.workers) > 1:
        return f"{worker.worker_id}:{worker.strategy}"
    return worker.worker_id


def _pick_worker(run: RunState, choice: str, candidates: list[WorkerRunState]) -> WorkerRunState:
    """Resolve a user's 'id' or 'id:strategy' to exactly one of ``candidates``, else exit 1."""
    matches = [w for w in candidates if choice in (_worker_label(run, w), f"{w.worker_id}:{w.strategy}")]
    if len(matches) == 1:
        return matches[0]
    if sum(w.worker_id == choice for w in run.workers) > 1:
        _fail(f"Worker '{escape(choice)}' ran several strategies in this run; use '{escape(choice)}:<strategy>'.")
    _fail(f"Unknown worker '{escape(choice)}'.")

def _print_run_table(run: RunState, title: Optional[str] = None) -> None:
    show_bench = bool(run.bench_command) or any(
        w.benchmark_data is not None or w.bench_exit_code is not None
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


def _make_output_streams_safe() -> None:
    """Replace characters the console can't encode instead of crashing.

    Worker logs contain characters such as aider's '►', which a cp1252 Windows
    console (or a redirected stream) cannot encode: printing them raised
    UnicodeEncodeError in the middle of a command.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


@click.group()
@click.version_option(package_name="hivemind", prog_name="hive")
def cli():
    """HiveMind: Distributed AI Coding Agent Fleet Orchestrator."""
    _make_output_streams_safe()


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
    # Owner-only (0600) on POSIX; on Windows the user-profile ACL already restricts it.
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_key)
    try:
        os.chmod(key_file, 0o600)  # also tighten a key file that already existed
    except OSError:
        pass

    console.print(Panel.fit(
        f"[bold green][OK] New Fleet API Key Generated Successfully![/bold green]\n\n"
        f"[bold white]Key:[/bold white] [bold cyan]{new_key}[/bold cyan]\n\n"
        f"[dim]Saved to:[/dim] [white]{key_file}[/white]\n\n"
        f"[bold yellow]Next Step on Worker Nodes:[/bold yellow]\n"
        f"Run on each worker machine and paste the key at the hidden prompt:\n"
        f"  [bold white]hivemind-worker key set[/bold white]",
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
        # Never continue on an unreadable file: saving would overwrite every existing worker.
        try:
            existing_config = ClusterConfig.load(target_path)
        except Exception as exc:
            console.print(
                f"[bold red]Error:[/bold red] Could not read {escape(str(target_path))}: {escape(str(exc))}\n"
                "Fix or remove the file, then run [bold cyan]hive discover[/bold cyan] again."
            )
            sys.exit(1)

    ts_state = TailscaleScanner.backend_state()
    ts_available = ts_state == "Running"
    if use_tailscale and not ts_available:
        reason = "is not installed" if ts_state is None else f"is not connected (state: {escape(ts_state)})"
        _fail(
            f"Tailscale {reason} on this machine. Connect it (e.g. [bold white]tailscale up[/bold white]), "
            "or scan the local network with [bold cyan]hive discover --local[/bold cyan]."
        )

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
            ts_peers = TailscaleScanner.discover_peers()
            discovered.extend(ts_peers)
        source_label = "Tailscale Mesh" if mode == "tailscale" else "Tailscale + Local"

    # 2. Local Subnet Discovery
    if mode in ("local", "both"):
        interfaces = NetworkScanner.get_local_interfaces()
        if not interfaces:
            if not discovered:
                _fail("No active local network interfaces found.")
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
                    progress_callback=update_progress,
                    daemon_port=daemon_port,
                    interface_ip=chosen_iface["ip"],
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
        # Only detected workers: "all" must never send the fleet key to unknown devices.
        selected_devices = [
            d for d in discovered
            if not d.is_local_host and presence_map.get(d.ip, (None, None))[0] is not None
        ]
        if not selected_devices:
            console.print(
                "[yellow]No HiveMind workers were detected. "
                "To add a device anyway, enter its number explicitly.[/yellow]"
            )
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
    used_ids: set[str] = set()
    for i, dev in enumerate(selected_devices, 1):
        default_id = f"worker-{i}" if dev.hostname in ("Unknown", "Master") else dev.hostname.split(".")[0].lower()
        if default_id in used_ids:
            default_id = f"{default_id}-{i}"
        console.print(f"[bold cyan]--- Device #{i} ({escape(dev.hostname)} @ {dev.ip}) ---[/bold cyan]")

        # Two devices with one ID would collapse into a single worker when merged.
        worker_id = Prompt.ask("  Worker ID", default=default_id)
        while worker_id in used_ids:
            console.print(f"  [yellow]ID '{escape(worker_id)}' is already used by another device in this session.[/yellow]")
            worker_id = Prompt.ask("  Worker ID", default=f"{worker_id}-{i}")
        used_ids.add(worker_id)
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

        # Auto-suggest default engine if detected
        default_engine = "aider" if "engine:aider" in detected_tags else ("agy" if "engine:agy" in detected_tags else "claude-code")
        agent_engine = Prompt.ask(
            "  AI Coding CLI Engine",
            choices=["claude-code", "agy", "aider"],
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
    config = _load_config(cluster)

    table = Table(title=f"HiveMind Cluster: {escape(config.cluster_name)} ({len(config.workers)} Workers)", border_style="cyan")
    table.add_column("Worker ID", style="bold white")
    table.add_column("Daemon Address", style="green")
    table.add_column("Engine", style="magenta")
    table.add_column("Capabilities", style="blue")

    for worker in config.workers:
        caps_str = ", ".join(worker.capabilities) if worker.capabilities else "none"
        table.add_row(
            escape(worker.id),
            f"{worker.host}:{worker.port}",
            worker.agent_engine,
            escape(caps_str),
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

    console.print(Panel.fit(
        f"[bold cyan]Checking HiveMind Cluster:[/bold cyan] {escape(config.cluster_name)} ({len(config.workers)} nodes)",
        border_style="cyan"
    ))

    table = Table(title="Cluster Readiness Status", border_style="green")
    table.add_column("Worker ID", style="bold white")
    table.add_column("Daemon Reachable", justify="center")
    table.add_column("Git Ready", justify="center")
    table.add_column("Agent Engine", justify="center")
    table.add_column("Version", justify="center")
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

    not_ready = 0
    versions: dict[str, list[str]] = {}
    for idx, worker in enumerate(config.workers):
        h_data, tags, err = probe_results[idx] or (None, [], "refused")
        if not h_data:
            not_ready += 1
        version = str(h_data.get("version") or "?") if h_data else "-"
        if h_data:
            versions.setdefault(version, []).append(worker.id)

        if h_data:
            daemon_status = "[bold green]ONLINE (Ready)[/bold green]" if h_data.get("status") == "ready" else "[bold yellow]BUSY[/bold yellow]"
            git_status = "[bold green]YES[/bold green]" if h_data.get("git_ready") else "[bold red]NO (missing)[/bold red]"

            engines = h_data.get("engines", [])
            # Workers report Claude Code as "claude-code"; "claude" is the cluster.json alias.
            if ("claude-code" if worker.agent_engine == "claude" else worker.agent_engine) in engines:
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
            escape(version),
            live_caps_str
        )

    console.print("\n", table, "\n")
    if len(versions) > 1:
        detail = "; ".join(f"{escape(v)}: {escape(', '.join(ids))}" for v, ids in sorted(versions.items()))
        console.print(
            f"[bold yellow]Warning:[/bold yellow] workers run different hivemind-worker versions ({detail}). "
            "Update them to the same version before dispatching.\n"
        )
    if not_ready:
        sys.exit(1)


@cli.command(name="dispatch")
@click.argument("prompt", required=True)
@click.option("--repo", "repo_url", default=None, help="Git origin URL (default: repo_url from cluster.json).")
@click.option("--workers", "worker_ids_csv", default=None, help="Comma-separated worker ids to use (default: all).")
@click.option("--strategy", "strategies", multiple=True, help="Named strategy (repeatable). Default: strategy-1..N.")
@click.option("--test", "test_command", default=None, help="Command to run after agent finishes (e.g. 'pytest -q').")
@click.option("--bench", "bench_command", default=None, help="Benchmark command producing a JSON output.")
@click.option("--model", default=None, help="Model override for the worker engine.")
@click.option(
    "--engine",
    type=click.Choice(SUPPORTED_ENGINES),
    default=None,
    help="Engine for every strategy, overriding agent_engine from cluster.json.",
)
@click.option(
    "--base",
    "base_branch",
    default=None,
    help="Branch on origin to start from (default: origin's default branch). Needs hivemind-worker 0.3.0+.",
)
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def dispatch_cmd(
    prompt: str,
    repo_url: Optional[str],
    worker_ids_csv: Optional[str],
    strategies: tuple[str, ...],
    test_command: Optional[str],
    bench_command: Optional[str],
    model: Optional[str],
    engine: Optional[str],
    base_branch: Optional[str],
    cluster: Optional[str],
):
    """Start a parallel coding job on worker machines."""
    if not _require_fleet_key():
        sys.exit(1)
    config = _load_config(cluster)

    worker_ids =[wid.strip() for wid in worker_ids_csv.split(",")] if worker_ids_csv else None
    try:
        selected = select_workers(config, worker_ids)
        slot_workers, assigned_strategies = assign_strategies(selected, strategies)
        resolved_repo = resolve_repo_url(config, repo_url)
    except DispatchError as exc:
        _fail(escape(str(exc)))
    if engine:
        slot_workers = [w.model_copy(update={"agent_engine": engine}) for w in slot_workers]

    console.print(Panel.fit(
        f"[bold cyan]Dispatching Swarm Job[/bold cyan]\n"
        f"[white]{escape(prompt)}[/white]\n"
        f"[dim]Origin:[/dim] {escape(resolved_repo)}\n"
        f"[dim]Base:[/dim] {escape(base_branch or 'default branch')}\n"
        f"[dim]Strategies ({len(slot_workers)}):[/dim] "
        f"{escape(', '.join(f'{s} on {w.id}' for w, s in zip(slot_workers, assigned_strategies)))}",
        border_style="cyan",
    ))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Dispatching tasks to worker daemons...", total=len(slot_workers))
        try:
            run = dispatch_run(
                config,
                prompt,
                repo_url=resolved_repo,
                workers=slot_workers,
                strategies=assigned_strategies,
                test_command=test_command,
                bench_command=bench_command,
                model=model,
                base_branch=base_branch,
            )
        except DispatchError as exc:
            _fail(escape(str(exc)))
        progress.advance(task, advance=len(slot_workers))

    console.print(Panel.fit(
        f"[bold green]Dispatched run {run.run_id}[/bold green]\n"
        f"[dim]Origin:[/dim] {escape(run.repo_url)}\n"
        "[dim]Follow progress with[/dim] [bold white]hive status[/bold white]",
        border_style="green",
    ))
    _print_run_table(run)
    rejected = [w for w in run.workers if w.status == "failed"]
    if rejected:
        _fail(f"{len(rejected)} of {len(run.workers)} strategies could not be dispatched (see Details). The run is saved.")


@cli.command(name="status")
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--watch", "-w", is_flag=True, default=False, help="Keep polling until every strategy has finished.")
@click.option("--interval", default=10.0, show_default=True, type=click.FloatRange(min=1.0), help="Seconds between polls with --watch.")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def status_cmd(run_id: Optional[str], watch: bool, interval: float, cluster: Optional[str]):
    """Show live status of a dispatched run by reading result.json from workers."""
    run, workers_by_id = _run_context(run_id, cluster)

    first_poll = True
    while True:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(f"Polling workers for run {run.run_id}...", total=len(run.workers))
            run = refresh_run(run, workers_by_id, skip_terminal=not first_poll)
            first_poll = False
            progress.advance(task, advance=len(run.workers))

        base = f"  base {escape(run.base_branch)}" if run.base_branch else ""
        console.print(f"[bold cyan]Run {run.run_id}[/bold cyan]  [dim]origin {escape(run.repo_url)}{base}[/dim]")
        _print_run_table(run)

        all_done = all(w.status in TERMINAL_STATUSES for w in run.workers)
        if all_done or not watch:
            break
        console.print(f"[dim]Polling again in {interval:g}s (Ctrl+C to stop)...[/dim]")
        time.sleep(interval)

    if all_done and run.workers:
        console.print("[dim]All workers finished. Review branches, then[/dim] [bold white]hive merge[/bold white].\n")


def _run_context(run_id: Optional[str], cluster: Optional[str]) -> tuple[RunState, dict[str, WorkerNode]]:
    """Fleet key check, the run, and the cluster's workers by id (empty if no cluster file)."""
    if not _require_fleet_key():
        sys.exit(1)
    run = _load_run(run_id)
    config = _load_config(cluster, required=False)
    return run, ({w.id: w for w in config.workers} if config else {})


@cli.command(name="merge")
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--worker", "worker_choice", default=None, help="Winner to merge (id, or id:strategy); skips the prompt.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Merge without asking for confirmation.")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def merge_cmd(run_id: Optional[str], worker_choice: Optional[str], yes: bool, cluster: Optional[str]):
    """Fetch a winning exp/* branch from origin and merge it into local main."""
    run, workers_by_id = _run_context(run_id, cluster)
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
        _fail("No worker branches are available to merge yet.")

    if worker_choice is None:
        default_w = next((w for w in finished if w.status == "succeeded"), finished[0])
        worker_choice = Prompt.ask(
            "Worker to merge (id, or id:strategy when a worker ran several)",
            default=_worker_label(run, default_w),
        )
    winner = _pick_worker(run, worker_choice, finished)

    try:
        validate_branch_ref(winner.branch)
    except DispatchError as exc:
        _fail(escape(str(exc)))

    origin = local_repo_origin()
    if origin is None:
        _fail(
            "Run hive merge from a clone of the shared origin.\n"
            f"[dim]Expected repo:[/dim] {escape(run.repo_url)}"
        )
    if normalize_git_url(origin) != normalize_git_url(run.repo_url):
        _fail(
            "This directory's origin does not match the run.\n"
            f"  local: {escape(origin)}\n"
            f"  run:   {escape(run.repo_url)}"
        )

    if local_working_tree_dirty():
        _fail(
            "Local working tree has uncommitted changes. "
            "Commit or stash them before running [bold white]hive merge[/bold white]."
        )

    try:
        run_base = validate_base_branch(run.base_branch)
    except DispatchError as exc:
        _fail(escape(str(exc)))

    try:
        local_git(
            "fetch",
            "origin",
            f"+refs/heads/{winner.branch}:refs/remotes/origin/{winner.branch}",
        )
        if run_base:
            # A run started from --base merges back into that branch, not the default one.
            local_git("fetch", "origin", f"+refs/heads/{run_base}:refs/remotes/origin/{run_base}")
        base = run_base or local_base_branch()
        has_local_base = (
            local_git("show-ref", "--verify", "--quiet", f"refs/heads/{base}", check=False).returncode == 0
        )
        compare_base = base if has_local_base else f"origin/{base}"
        log = local_git("log", "--oneline", f"{compare_base}..origin/{winner.branch}", check=False)
        diff = local_git("diff", "--stat", f"{compare_base}...origin/{winner.branch}", check=False)
    except RuntimeError as exc:
        _fail(escape(str(exc)))

    console.print(Panel(
        # Commit messages come from the worker's agent: never let them render as markup.
        (escape(log.stdout) if log.stdout else "[dim]no commits on branch[/dim]") + "\n" + escape(diff.stdout or ""),
        title=f"origin/{winner.branch} vs {compare_base}",
        border_style="blue",
    ))

    if not yes and not Confirm.ask(f"Merge [bold]{winner.branch}[/bold] into local [bold]{base}[/bold]?", default=True):
        console.print("[dim]Merge cancelled.[/dim]")
        return

    try:
        if has_local_base:
            local_git("checkout", base)
        else:
            local_git("checkout", "-B", base, f"origin/{base}")
        local_git("merge", "--no-ff", f"origin/{winner.branch}", "-m", f"hive: merge {winner.branch} from {run.run_id}")
    except RuntimeError as exc:
        _fail(
            f"{escape(str(exc))}\n"
            "If the merge stopped on conflicts, resolve them and commit, or run [bold white]git merge --abort[/bold white]."
        )

    console.print(Panel.fit(
        f"[bold green]Merged[/bold green] [white]{winner.branch}[/white] into local [white]{base}[/white].\n"
        "[dim]Push to origin when you are ready.[/dim]",
        border_style="green",
    ))


@cli.command(name="cancel")
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--worker", "worker_choice", default=None, help="Cancel only this one (id, or id:strategy).")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def cancel_cmd(run_id: Optional[str], worker_choice: Optional[str], cluster: Optional[str]):
    """Cancel the strategies of a run that are still queued or running."""
    run, workers_by_id = _run_context(run_id, cluster)
    run = refresh_run(run, workers_by_id)

    targets = [_pick_worker(run, worker_choice, run.workers)] if worker_choice else run.workers
    targets = [w for w in targets if w.status not in TERMINAL_STATUSES]
    if not targets:
        console.print(f"[dim]Nothing to cancel: the selected strategies of run {run.run_id} have already finished.[/dim]")
        return

    failures = 0
    for w in targets:
        ok, message = cancel_worker_job(worker_node_for(w, workers_by_id), w.remote_run_dir or run.run_id)
        label = escape(f"{w.worker_id}:{w.strategy}")
        if ok:
            console.print(f"[green]Cancelled[/green] {label}")
        else:
            failures += 1
            console.print(f"[bold red]Could not cancel[/bold red] {label}: {escape(message)}")

    _print_run_table(refresh_run(run, workers_by_id))
    if failures:
        sys.exit(1)


@cli.command(name="runs")
@click.option("--limit", "-n", default=20, show_default=True, type=click.IntRange(min=1), help="How many runs to show.")
def runs_cmd(limit: int):
    """List saved runs, newest first. Statuses are as of the last status/merge; workers are not contacted."""
    runs: list[RunState] = []
    for path in get_runs_dir().glob("*.json"):
        try:
            runs.append(RunState.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # skip unreadable or hand-edited files instead of failing the listing
    if not runs:
        console.print("[dim]No runs yet. Start one with[/dim] [bold white]hive dispatch[/bold white].")
        return
    runs.sort(key=lambda r: (r.created_at, r.run_id), reverse=True)

    table = Table(title=f"HiveMind Runs ({min(limit, len(runs))} of {len(runs)})", border_style="cyan")
    table.add_column("Run ID", style="bold white", no_wrap=True)
    table.add_column("Created (UTC)", style="dim", no_wrap=True)
    table.add_column("Strategies", justify="center")
    table.add_column("Status")
    table.add_column("Prompt")
    for run in runs[:limit]:
        counts: dict[str, int] = {}
        for w in run.workers:
            counts[w.status] = counts.get(w.status, 0) + 1
        summary = ", ".join(f"{n} {_status_style(s)}" for s, n in sorted(counts.items()))
        prompt = run.prompt.replace("\n", " ")
        table.add_row(
            run.run_id,
            run.created_at.replace("T", " ")[:16],
            str(len(run.workers)),
            summary or "[dim]-[/dim]",
            escape(prompt if len(prompt) <= 60 else prompt[:57] + "..."),
        )
    console.print()
    console.print(table)
    console.print()


@cli.command(name="logs")
@click.argument("worker_choice", required=False)
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--full", is_flag=True, default=False, help="Fetch the whole log (up to 1 MiB) instead of the last lines.")
@click.option(
    "--file",
    "log_file",
    type=click.Choice(LOG_FILES),
    default="agent",
    show_default=True,
    help="Which log to fetch with --full.",
)
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def logs_cmd(worker_choice: Optional[str], run_id: Optional[str], full: bool, log_file: str, cluster: Optional[str]):
    """Show the log each worker reports for a run (all strategies, or WORKER / WORKER:STRATEGY)."""
    run, workers_by_id = _run_context(run_id, cluster)
    run = refresh_run(run, workers_by_id)
    targets = [_pick_worker(run, worker_choice, run.workers)] if worker_choice else run.workers

    failures = 0
    for w in targets:
        console.rule(f"{escape(w.worker_id)} · {escape(w.strategy)} · {_status_style(w.status)}")
        if w.error:
            console.print(f"[bold red]Error:[/bold red] {escape(w.error)}")
        text = w.log_tail or "(no log output reported yet)"
        if full:
            payload, err = fetch_worker_log(worker_node_for(w, workers_by_id), w.remote_run_dir or run.run_id, log_file)
            if payload is None:
                failures += 1
                console.print(f"[yellow]Full {escape(log_file)} log unavailable: {escape(err or 'unknown error')}. "
                              "Showing the last lines instead.[/yellow]")
            else:
                content_str = str(payload.get("content") or "")
                size_val = payload.get("size", len(content_str.encode("utf-8")))
                if payload.get("truncated"):
                    console.print(f"[dim]Showing the end of the {escape(log_file)} log: "
                                  f"last {len(content_str.encode('utf-8'))} of {size_val} bytes.[/dim]")
                text = content_str or f"(the {log_file} log is empty)"
        # Log text comes from the agent and tests: print it literally, never as markup.
        console.print(text, markup=False, highlight=False)
    console.print()
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    cli()
