import click
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from hivemind.discovery.scanner import NetworkScanner, DiscoveredDevice
from hivemind.discovery.prober import SystemProber
from hivemind.config.cluster import ClusterConfig, WorkerNode, get_default_cluster_path
from hivemind.runner.dispatch import (
    DispatchError,
    assign_strategies,
    dispatch_run,
    resolve_repo_url,
    select_workers,
)
from hivemind.runner.state import RunState
from hivemind.runner.status import (
    local_base_branch,
    local_git,
    local_repo_origin,
    normalize_git_url,
    refresh_run,
)
from hivemind.ssh import SSHError, ssh_run

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
        "running": "[bold yellow]running[/bold yellow]",
        "starting": "[yellow]starting[/yellow]",
        "unknown": "[dim]unknown[/dim]",
    }
    return mapping.get(status, status)


def _print_run_table(run: RunState, title: Optional[str] = None) -> None:
    table = Table(
        title=title or f"Run {run.run_id}",
        border_style="cyan",
    )
    table.add_column("Worker", style="bold white")
    table.add_column("Strategy", style="magenta")
    table.add_column("Branch", style="green")
    table.add_column("Status", justify="center")
    table.add_column("Tests", justify="center")
    table.add_column("Details", style="dim")

    for worker in run.workers:
        if worker.test_exit_code is None:
            tests = "[dim]—[/dim]"
        elif worker.test_exit_code == 0:
            tests = "[bold green]pass[/bold green]"
        else:
            tests = f"[bold red]fail ({worker.test_exit_code})[/bold red]"
        details = worker.error or worker.log_tail or ""
        if len(details) > 120:
            details = details[-120:]
        table.add_row(
            worker.worker_id,
            worker.strategy,
            worker.branch or "—",
            _status_style(worker.status),
            tests,
            details.replace("\n", " "),
        )
    console.print()
    console.print(table)
    console.print()


@click.group()
@click.version_option(package_name="hivemind", prog_name="hive")
def cli():
    """HiveMind: Distributed AI Coding Agent Fleet Orchestrator."""
    pass


@cli.command(name="discover")
@click.option("--output", "-o", default=None, help="Path to save cluster config (default: ~/.hivemind/cluster.json).")
def discover_cmd(output: Optional[str]):
    """Scan local network for devices and interactively configure your HiveMind cluster."""
    target_path = output if output else str(get_default_cluster_path())
    
    console.print(Panel.fit(
        "[bold cyan]HiveMind Network Discovery[/bold cyan]\n"
        "[dim]Scanning local Wi-Fi / Ethernet subnets to find candidate worker machines...[/dim]",
        border_style="cyan"
    ))

    interfaces = NetworkScanner.get_local_interfaces()
    if not interfaces:
        console.print("[bold red]Error:[/bold red] No active local network interfaces found.")
        return

    # If multiple interfaces, let user choose or default to first
    chosen_iface = interfaces[0]
    if len(interfaces) > 1:
        console.print("\n[bold yellow]Multiple network interfaces detected:[/bold yellow]")
        for i, iface in enumerate(interfaces, 1):
            console.print(f"  [{i}] {iface['name']} - IP: {iface['ip']} (Subnet: {iface['cidr']})")
        choice = Prompt.ask("Select interface to scan", default="1")
        try:
            chosen_iface = interfaces[int(choice) - 1]
        except Exception:
            chosen_iface = interfaces[0]

    console.print(f"\n[bold green]Scanning subnet:[/bold green] {chosen_iface['cidr']} via [cyan]{chosen_iface['name']}[/cyan] ({chosen_iface['ip']})...\n")

    discovered: list[DiscoveredDevice] = []
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

        discovered = NetworkScanner.scan_subnet(
            chosen_iface["network"],
            progress_callback=update_progress
        )

    if not discovered:
        console.print("[yellow]No other active devices discovered on the subnet.[/yellow]")
        return

    # Display results table
    table = Table(title=f"Discovered Devices on {chosen_iface['cidr']}", border_style="blue")
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("IP Address", style="bold white")
    table.add_column("Hostname", style="green")
    table.add_column("SSH (Port 22)", justify="center")
    table.add_column("Type", style="dim")

    for idx, dev in enumerate(discovered, 1):
        ssh_status = "[bold green]OPEN[/bold green]" if dev.ssh_open else "[dim red]CLOSED[/dim red]"
        device_type = "Master Host" if dev.is_local_host else "Remote Device"
        table.add_row(
            str(idx),
            dev.ip,
            dev.hostname,
            ssh_status,
            device_type
        )

    console.print("\n", table, "\n")

    # Interactive device selection
    selection_str = Prompt.ask(
        "[bold yellow]Select device numbers to add to the cluster[/bold yellow] (e.g. '1, 2, 3' or 'all', 'q' to quit)",
        default="all"
    )

    if selection_str.strip().lower() in ["q", "quit", "exit"]:
        console.print("[dim]Discovery aborted.[/dim]")
        return

    selected_devices: list[DiscoveredDevice] = []
    if selection_str.strip().lower() == "all":
        selected_devices = [d for d in discovered if not d.is_local_host]
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

    # Build cluster config
    workers: list[WorkerNode] = []
    for i, dev in enumerate(selected_devices, 1):
        default_id = f"worker-{i}" if dev.hostname == "Unknown" else dev.hostname.split(".")[0].lower()
        console.print(f"[bold cyan]--- Device #{i} ({dev.ip}) ---[/bold cyan]")
        
        worker_id = Prompt.ask("  Worker ID", default=default_id)
        ssh_user = Prompt.ask("  SSH Username [dim](actual user on target device)[/dim]", default="developer")
        ssh_port = int(Prompt.ask("  SSH Port", default="22"))

        # Automatically probe the remote machine for real hardware & tools
        console.print("  [dim]Probing hardware & installed tools over SSH...[/dim]")
        detected_tags = SystemProber.probe_remote(dev.ip, user=ssh_user, port=ssh_port, timeout=3.0)
        default_caps_str = ", ".join(detected_tags)
        console.print(f"  [bold green]✓ Detected Profile:[/bold green] [cyan]{default_caps_str}[/cyan]")

        workspace_dir = Prompt.ask("  Workspace Directory", default="~/workspace/project")
        
        # Auto-suggest default engine if detected
        default_engine = "claude-code"
        if "engine:agy" in detected_tags:
            default_engine = "agy"
        elif "engine:aider" in detected_tags:
            default_engine = "aider"

        agent_engine = Prompt.ask(
            "  AI Coding CLI Engine",
            choices=["claude-code", "agy", "aider", "custom"],
            default=default_engine
        )
        
        capabilities_raw = Prompt.ask("  Capabilities tags (comma-separated)", default=default_caps_str)
        caps = [c.strip() for c in capabilities_raw.split(",") if c.strip()]

        workers.append(WorkerNode(
            id=worker_id,
            host=dev.ip,
            port=ssh_port,
            user=ssh_user,
            workspace_dir=workspace_dir,
            agent_engine=agent_engine,
            capabilities=caps
        ))
        console.print()

    cluster_config = ClusterConfig(
        cluster_name="hive-local-fleet",
        workers=workers
    )

    saved_path = cluster_config.save(output)
    console.print(Panel.fit(
        f"[bold green]✓ Global cluster configuration saved successfully to [white]{saved_path}[/white][/bold green]\n"
        f"[dim]Run [bold white]hive check[/bold white] to verify connectivity and agent readiness.[/dim]",
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
    table.add_column("Host:Port", style="green")
    table.add_column("User", style="yellow")
    table.add_column("Engine", style="magenta")
    table.add_column("Capabilities", style="blue")
    table.add_column("Workspace Dir", style="dim")

    for w in config.workers:
        table.add_row(
            w.id,
            f"{w.host}:{w.port}",
            w.user,
            w.agent_engine,
            ", ".join(w.capabilities) if w.capabilities else "none",
            w.workspace_dir
        )

    console.print("\n", table)
    if config.repo_url:
        console.print(f"[dim]Shared origin:[/dim] [white]{config.repo_url}[/white]\n")
    else:
        console.print("[dim]Shared origin: not set (pass --repo on hive dispatch or add repo_url to cluster.json)[/dim]\n")


@cli.command(name="check")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file (default: ~/.hivemind/cluster.json).")
def check_cmd(cluster: Optional[str]):
    """Test SSH connectivity, Git readiness, and AI CLI tools on all worker nodes."""
    try:
        config = ClusterConfig.load(cluster)
    except FileNotFoundError:
        default_path = cluster if cluster else get_default_cluster_path()
        console.print(f"[bold red]Error:[/bold red] Cluster file '{default_path}' not found. Run [bold cyan]hive discover[/bold cyan] first.")
        return

    console.print(f"\n[bold cyan]Checking connectivity for cluster: [white]{config.cluster_name}[/white]...[/bold cyan]\n")

    table = Table(title="Node Health & Readiness Check", border_style="green")
    table.add_column("Worker ID", style="bold white")
    table.add_column("Host", style="cyan")
    table.add_column("SSH Reachable", justify="center")
    table.add_column("Git Ready", justify="center")
    table.add_column("Agent Engine", style="magenta")
    table.add_column("Status Details", style="dim")

    for w in config.workers:
        ssh_ok = NetworkScanner.check_ssh_port(w.host, port=w.port, timeout=2.5)
        ssh_status = "[bold green]YES[/bold green]" if ssh_ok else "[bold red]NO[/bold red]"
        
        git_status = "[yellow]UNKNOWN[/yellow]"
        details = "Port 22 Open" if ssh_ok else "Connection Refused / Timeout"

        # If SSH is open, attempt a quick SSH command and probe hardware
        if ssh_ok:
            try:
                res = ssh_run(w, "git --version", timeout=3, check=False)
                if res.returncode == 0:
                    git_status = "[bold green]YES[/bold green]"
                    live_tags = SystemProber.probe_remote(w.host, user=w.user, port=w.port, timeout=3.0)
                    details = f"{res.stdout.strip()} | Profile: {', '.join(live_tags)}"
                else:
                    git_status = "[bold yellow]AUTH REQUIRED[/bold yellow]"
                    details = (res.stderr or res.stdout or "SSH key not configured on worker").strip()
            except SSHError as e:
                details = str(e)
            except Exception as e:
                details = str(e)

        table.add_row(
            w.id,
            f"{w.host}:{w.port}",
            ssh_status,
            git_status,
            w.agent_engine,
            details
        )

    console.print(table, "\n")


@cli.command(name="dispatch")
@click.argument("prompt_arg", metavar="PROMPT", required=False)
@click.option("--prompt", "prompt_opt", default=None, help="Task prompt (alternative to the positional argument).")
@click.option("--repo", "repo_url", default=None, help="Shared git origin. Overrides cluster.json repo_url.")
@click.option("--workers", "worker_ids", default=None, help="Comma-separated worker ids (default: all).")
@click.option("--strategy", "strategies", multiple=True, help="Named strategy for a worker. Repeatable.")
@click.option("--test", "test_command", default=None, help="Remote command to run after the agent exits.")
@click.option("--bench", "bench_command", default=None, help="Optional remote benchmark command.")
@click.option("--model", default=None, help="Model id for aider (default: openrouter/openrouter/free).")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def dispatch_cmd(
    prompt_arg: Optional[str],
    prompt_opt: Optional[str],
    repo_url: Optional[str],
    worker_ids: Optional[str],
    strategies: tuple[str, ...],
    test_command: Optional[str],
    bench_command: Optional[str],
    model: Optional[str],
    cluster: Optional[str],
):
    """Start a parallel coding job on worker machines. Returns immediately; use hive status to follow it."""
    prompt = (prompt_arg or prompt_opt or "").strip()
    if not prompt:
        console.print("[bold red]Error:[/bold red] A prompt is required. Example: hive dispatch \"Fix add() so check.py passes.\"")
        return

    config = _load_config(cluster)
    if config is None:
        return

    try:
        resolved_repo = resolve_repo_url(config, repo_url)
        selected = select_workers(
            config,
            [part.strip() for part in worker_ids.split(",")] if worker_ids else None,
        )
        assigned = assign_strategies(selected, strategies)
        extra = len(strategies) - len(selected)
        if extra > 0:
            console.print(f"[yellow]Ignoring {extra} extra --strategy value(s); one strategy is used per worker.[/yellow]")
        run = dispatch_run(
            config,
            prompt,
            repo_url=resolved_repo,
            workers=selected,
            strategies=assigned,
            test_command=test_command,
            bench_command=bench_command,
            model=model,
        )
    except DispatchError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return

    console.print(Panel.fit(
        f"[bold green]Dispatched run[/bold green] [white]{run.run_id}[/white]\n"
        f"[dim]Origin:[/dim] {run.repo_url}\n"
        f"[dim]Follow progress with[/dim] [bold white]hive status[/bold white]",
        border_style="green",
    ))
    _print_run_table(run)


@cli.command(name="status")
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def status_cmd(run_id: Optional[str], cluster: Optional[str]):
    """Show live status of a dispatched run by reading result.json on each worker."""
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
    console.print(
        f"[bold cyan]Run[/bold cyan] [white]{run.run_id}[/white]  "
        f"[dim]origin {run.repo_url}[/dim]"
    )
    _print_run_table(run)
    still_running = any(w.status in {"starting", "running", "unknown"} for w in run.workers)
    if still_running:
        console.print("[dim]Jobs are still in progress. Re-run hive status to refresh.[/dim]\n")
    else:
        console.print("[dim]All workers finished. Review branches, then hive merge.[/dim]\n")


@cli.command(name="merge")
@click.option("--run", "run_id", default=None, help="Run id (default: latest).")
@click.option("--cluster", "-c", default=None, help="Path to cluster configuration file.")
def merge_cmd(run_id: Optional[str], cluster: Optional[str]):
    """Fetch a winning exp/* branch from origin and merge it into local main."""
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

    finished = [w for w in run.workers if w.branch]
    if not finished:
        console.print("[bold red]Error:[/bold red] No worker branches are available to merge yet.")
        return

    default_id = next((w.worker_id for w in finished if w.status == "succeeded"), finished[0].worker_id)
    choice = Prompt.ask("Worker id to merge", default=default_id)
    winner = next((w for w in finished if w.worker_id == choice), None)
    if winner is None:
        console.print(f"[bold red]Error:[/bold red] Unknown worker '{choice}'.")
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

    try:
        local_git(
            "fetch",
            "origin",
            f"+refs/heads/{winner.branch}:refs/remotes/origin/{winner.branch}",
        )
        base = local_base_branch()
        log = local_git("log", "--oneline", f"origin/{base}..origin/{winner.branch}", check=False)
        diff = local_git("diff", "--stat", f"origin/{base}...origin/{winner.branch}", check=False)
    except RuntimeError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        return

    console.print(Panel(
        (log.stdout or "[dim]no commits on branch[/dim]") + "\n" + (diff.stdout or ""),
        title=f"origin/{winner.branch} vs origin/{base}",
        border_style="blue",
    ))

    if not Confirm.ask(f"Merge [bold]{winner.branch}[/bold] into local [bold]{base}[/bold]?", default=True):
        console.print("[dim]Merge cancelled.[/dim]")
        return

    try:
        local_git("checkout", base)
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
