import click
import json
import subprocess
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from hivemind.discovery.scanner import NetworkScanner, DiscoveredDevice
from hivemind.discovery.prober import SystemProber
from hivemind.config.cluster import ClusterConfig, WorkerNode, get_default_cluster_path

console = Console()


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

    console.print("\n", table, "\n")


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
                res = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", "-p", str(w.port), f"{w.user}@{w.host}", "git --version"],
                    capture_output=True, text=True, timeout=3
                )
                if res.returncode == 0:
                    git_status = "[bold green]YES[/bold green]"
                    # Probe live tags
                    live_tags = SystemProber.probe_remote(w.host, user=w.user, port=w.port, timeout=3.0)
                    details = f"{res.stdout.strip()} | Profile: {', '.join(live_tags)}"
                else:
                    git_status = "[bold yellow]AUTH REQUIRED[/bold yellow]"
                    details = "SSH key not configured on worker"
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


if __name__ == "__main__":
    cli()
