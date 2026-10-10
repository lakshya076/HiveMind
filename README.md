# HiveMind: Distributed AI Coding Agent Fleet

[![Status](https://img.shields.io/badge/Status-Active_Development-orange.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](license.txt)
[![Target-Engines](https://img.shields.io/badge/Engines-Claude_Code_|_Antigravity_|_Aider-purple.svg)](#)

> **Transform multiple machines into a coordinated, parallel AI software engineering team.**  
> Orchestrate multiple computers to explore different coding strategies concurrently, share discoveries in real time, prune failing attempts, and synthesize the winning code into production.

---

## 📑 Contents

* [1. Overview](#1-overview)
* [2. Key Features](#2-key-features)
* [3. Architecture & Workflow](#3-architecture--workflow)
* [4. Installation & Setup](#4-installation--setup)
  * [Master Machine Setup (`hive`)](#master-machine-setup-hive)
  * [Worker Machine Setup (`hivemind-worker`)](#worker-machine-setup-hivemind-worker)
* [5. CLI Command Reference](#5-cli-command-reference)
  * [Step 1: Generate Fleet API Key (`hive keygen`)](#step-1-generate-fleet-api-key-hive-keygen)
  * [Step 2: Discover Devices (`hive discover`)](#step-2-discover-devices-hive-discover)
  * [Step 3: Inspect Cluster (`hive list`)](#step-3-inspect-cluster-hive-list)
  * [Step 4: Audit Cluster Readiness (`hive check`)](#step-4-audit-cluster-readiness-hive-check)
  * [Step 5: Dispatch a Parallel Swarm Job (`hive dispatch`)](#step-5-dispatch-a-parallel-swarm-job-hive-dispatch)
  * [Step 6: Track Live Execution (`hive status`)](#step-6-track-live-execution-hive-status)
  * [Step 7: Inspect & Merge Winner (`hive merge`)](#step-7-inspect--merge-winner-hive-merge)
* [6. Running `hivemind-worker` as a Persistent Service](#6-running-hivemind-worker-as-a-persistent-service)
  * [🐧 Linux (systemd Service)](#-linux-systemd-service)
  * [🪟 Windows (Background / Task Scheduler / NSSM)](#-windows-background--task-scheduler--nssm)
* [7. Security Model](#7-security-model)
* [8. Project Documentation](#8-project-documentation)
  * [Network & Machine Setup Guide](NETWORK_SETUP.md)
* [9. License](#9-license)

---

## 1. Overview

**HiveMind** (CLI: `hive`, Package: `hivemind`) is an open-source distributed orchestrator for autonomous AI coding agents.

Instead of relying on a single AI agent working sequentially on one computer, HiveMind coordinates your existing fleet of machines (GPU workstations, cloud servers, and developer laptops) into an autonomous software engineering team:

* **Master Node (Lead Architect):** Runs the `hive` CLI. Discovers fleet nodes over Tailscale or local subnets, decomposes coding goals into parallel implementation strategies, and dispatches them across workers.
* **Worker Fleet (`hivemind-worker` Daemons):** Lightweight HTTP daemons running on port `7422` of each worker machine. Each daemon manages headless AI coding sessions (Claude Code, Antigravity CLI `agy`, Aider) in isolated Git branches, running builds, tests, and benchmark suites on local hardware.
* **Git Origin Synchronization:** All code changes travel through isolated experimental branches (`exp/<worker>-<strategy>`) on your shared Git remote (GitHub, GitLab, or self-hosted). The master machine reviews and merges the winning solution into your main branch.

---

## 2. Key Features

* **Zero-SSH HTTP Daemon Transport**: Master communicates with workers via authenticated REST API calls over port `7422`. No SSH server configuration, passwordless key juggling, or raw shell exposure required on worker machines.
* **Cross-Platform Compatibility**: Full native support for **Linux** and **Windows** across both master and worker nodes.
* **Automatic Fleet Discovery**: One-command instant discovery across Tailscale mesh networks (`hive discover --tailscale`) and local subnets (`hive discover --local`).
* **Competitive Strategy Exploration**: Run $N$ parallel implementations of complex features (e.g., SIMD vs. Actor Queues vs. Memory-Mapped files) concurrently.
* **Hardware-Aware Tool Detection**: Automatically detects installed AI engines (`claude-code`, `agy`, `aider`), CPU core count, available RAM, and Git readiness at probe time.
* **Isolated Branch Workflows**: Autonomous agents only edit code in designated workspace branches. The worker daemon safely manages git branching, commit creation, and branch pushes.

---

## 3. Architecture & Workflow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          MASTER MACHINE (you)                               │
│                                                                             │
│  1. hive keygen   ──► Generates fleet API key (~/.hivemind/fleet.key)       │
│  2. hive discover ──► Probes port 7422 across Tailscale / LAN, saves cluster│
│  3. hive check    ──► GET /health on every worker to verify daemon readiness│
│  4. hive dispatch ──► POST /dispatch with task prompt, repo URL, test cmd   │
│  5. hive status   ──► GET /status/{run_id} polling, renders live table      │
│  6. hive merge    ──► git fetch origin exp/* branch, diff preview, merge    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │ HTTP REST (Port 7422 + Bearer Token)
                    ┌──────────────▼───────────────┐
                    │    WORKER DAEMON (Port 7422) │
                    │       (hivemind-worker)      │
                    │                              │
                    │  a) POST /dispatch received  │
                    │  b) git clone / fetch repo   │
                    │  c) checkout exp/<w>-<s>     │
                    │  d) run aider / agy / claude │
                    │  e) run --test command       │
                    │  f) git commit + git push    │
                    │  g) update status & logs     │
                    └──────────────┬───────────────┘
                                   │ git push origin exp/<w>-<s>
                                   ▼
                    ┌──────────────────────────────┐
                    │      Shared Git Origin       │
                    │   (GitHub / GitLab / etc.)   │
                    │        exp/<w>-<s>           │ ◄── master fetches winner
                    └──────────────────────────────┘
```

---

## 4. Installation & Setup

### Master Machine Setup (`hive`)

The master machine runs the `hive` orchestration CLI.

#### 1. Clone the repository:
```bash
git clone https://github.com/your-username/HiveMind.git
cd HiveMind
```

#### 2. Install using `uv` (Recommended) or `pip`:
```bash
# Option A: Fast Global Setup with uv
uv tool install -e .

# Option B: Using standard pip in virtualenv
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
pip install -e .
```

#### 3. Generate your Fleet API Key:
```bash
hive keygen
```
This generates a secure key and saves it to `~/.hivemind/fleet.key`.

---

### Worker Machine Setup (`hivemind-worker`)

Worker machines run the standalone `hivemind-worker` daemon. They do **not** need the master `hive` CLI. Clone the [hivemind-worker](https://github.com/lakshya076/hivemind-worker) repo first.

#### 1. Install `hivemind-worker`:
```bash
cd hivemind-worker
uv tool install -e .    # or: pip install -e .
```

#### 2. Authorize with your Fleet API Key:
```bash
# Paste the key from `hive keygen` at the hidden prompt (stored in ~/.hivemind/worker.json)
hivemind-worker key set
```
> FLEET_KEY was generated during `hive keygen` command. If you start a worker without a key, it generates one and prints instructions. Workers never run unauthenticated on the network.

#### 3. Ensure AI Coding Agents & Git are Available:
Ensure at least one supported AI coding CLI is installed and accessible on the worker:
* **Aider**: `pip install aider-chat` (Export `OPENROUTER_API_KEY` or model API key in `~/.bashrc` / `~/.env`)
* **Antigravity CLI (`agy`)**: Install `agy` (Export `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY`)
* **Claude Code**: Install `claude-code` (Export `ANTHROPIC_API_KEY`)
* **Git**: Configured with push access to your shared repositories.

---

## 5. CLI Command Reference

Both `hive` and `hivemind` can be used interchangeably.

### Step 1: Generate Fleet API Key (`hive keygen`)

```bash
hive keygen
```
Creates a new fleet authorization token and saves it to `~/.hivemind/fleet.key`.

### Step 2: Discover Devices (`hive discover`)

HiveMind provides **two complementary detection strategies** to locate and audit worker machines across your fleet:

1. **Approach A: Tailscale Mesh Network (`--tailscale`) [Recommended]**  
   Queries the local Tailscale daemon (`tailscale status --json`) for instant ($<0.1\text{s}$) zero-config discovery of all active peer nodes across your private Tailnet. Works seamlessly across different networks, cloud VMs, and home routers with full end-to-end WireGuard encryption.
2. **Approach B: Local Subnet Scan (`--local`)**  
   Performs a fast concurrent ARP and TCP port scan across your local Wi-Fi or Ethernet IPv4 subnet (e.g. `192.168.1.0/24`) to find active computers on the same physical network.

```bash
# Discover Tailscale peers (Instant mesh network discovery)
hive discover --tailscale

# Scan local subnet (Wi-Fi / Ethernet)
hive discover --local

# Interactive mode (prompts to choose between Tailscale or Local scan)
hive discover
```

Probes port `7422` on candidate IPs, queries `/health` for live OS, CPU, RAM, and AI engine status, and saves selected nodes to `~/.hivemind/cluster.json`.

> 📘 **Network Configuration & Pre-Flight Setup:**  
> Before running discovery, ensure your routers, firewalls, and worker machines are prepared (e.g. disabling Wi-Fi AP Isolation, opening port 7422, or joining your Tailnet).  
> 👉 **See the detailed [Network & Machine Preparation Guide (NETWORK_SETUP.md)](NETWORK_SETUP.md)** for step-by-step setup on Linux, Windows, and macOS.

### Step 3: Inspect Cluster (`hive list`)

View your configured cluster nodes and their hardware capabilities:

```bash
hive list
```

### Step 4: Audit Cluster Readiness (`hive check`)

Verify real-time HTTP reachability, Git readiness, and AI agent status across all nodes:

```bash
hive check
```

### Step 5: Dispatch a Parallel Swarm Job (`hive dispatch`)

Dispatch a task to one or more worker machines:

```bash
# Minimal: Positional prompt, pass repo_url in cluster.json
hive dispatch "Fix the bug in add.py so that add(a, b) returns a + b" --test "python3 check.py"

# Explicit repo URL and target workers
hive dispatch "Refactor database query engine" \
  --repo https://github.com/you/repo.git \
  --workers ari,hermes \
  --test "pytest tests/ -q"

# Multiple named strategies
hive dispatch "Optimize sorting algorithm" \
  --strategy "quicksort-simd" \
  --strategy "timsort-parallel" \
  --test "pytest -q" \
  --bench "python benchmark.py"
```

| Option | Description |
|---|---|
| `PROMPT` | *(Required)* The task instruction for the AI agents |
| `--repo <url>` | Git origin URL (overrides `repo_url` in `cluster.json`) |
| `--workers <id1,id2>` | Comma-separated worker IDs (default: all workers) |
| `--strategy <name>` | Named strategy (repeatable, e.g. `--strategy simd --strategy async`) |
| `--test <cmd>` | Test command executed after agent completes |
| `--bench <cmd>` | Benchmark command producing JSON output |
| `--model <id>` | Model override for the worker engine |

### Step 6: Track Live Execution (`hive status`)

Poll the status of active and recent swarm runs:

```bash
hive status                                   # Check latest run
hive status --run 20261006-174456-add56543    # Check specific run
```

Displays a live table with strategy names, branch names, execution status (`starting`, `running`, `succeeded`, `failed`), test exit codes, and output snippets.

### Step 7: Inspect & Merge Winner (`hive merge`)

Run from your local development repository clone:

```bash
cd ~/workspace/my-project
hive merge
```

1. Displays completed worker runs with test outcomes.
2. Prompts you to pick the winning worker strategy.
3. Fetches the winning branch (`exp/<worker>-<strategy>`) from the Git remote.
4. Previews the `git diff --stat`.
5. Merges the branch cleanly into your current local branch.

---

## 6. Running `hivemind-worker` as a Persistent Service

To ensure worker nodes are always online and auto-start on system boot:

### Linux (systemd Service)

1. Save configuration on the worker (as the same user the service runs as):
   ```bash
   hivemind-worker key set
   ```

2. Create `/etc/systemd/system/hivemind-worker.service`:
   ```ini
   [Unit]
   Description=HiveMind Worker Daemon
   After=network.target tailscaled.service

   [Service]
   Type=simple
   User=lakshya
   Environment="PATH=/home/lakshya/.local/bin:/usr/local/bin:/usr/bin:/bin"
   ExecStart=/home/lakshya/.local/bin/hivemind-worker start
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   *(Update `lakshya` and executable path to match `which hivemind-worker`).*

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now hivemind-worker
   ```

4. Check logs:
   ```bash
   journalctl -u hivemind-worker -f
   ```

---

### Windows (Background / Task Scheduler / NSSM)

#### Method 1: Run in Background for Current Session (No Auto-Startup)

* **Hidden Background Process (PowerShell):**
  ```powershell
  Start-Process -WindowStyle Hidden -FilePath "hivemind-worker" -ArgumentList "start"
  ```
  *To stop:* `Stop-Process -Name "hivemind-worker" -Force`

* **PowerShell Background Job:**
  ```powershell
  Start-Job -Name "HiveMindWorker" -ScriptBlock { hivemind-worker start }
  ```
  *To view logs:* `Receive-Job -Name "HiveMindWorker" -Keep`  
  *To stop:* `Stop-Job -Name "HiveMindWorker"`

#### Method 2: Auto-Start via Windows Task Scheduler (Recommended)

Run in PowerShell as Administrator:

```powershell
# 1. Save the fleet key once
hivemind-worker key set

# 2. Register scheduled task to start silently on login
$Action = New-ScheduledTaskAction -Execute "hivemind-worker.exe" -Argument "start"
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "HiveMindWorker" -Action $Action -Trigger $Trigger -Settings $Settings -Description "HiveMind Worker AI Fleet Daemon"
```

#### Method 3: Windows Service via NSSM (Runs even when logged out)

> [!WARNING]
> Without `ObjectName`, NSSM runs the service as **LocalSystem**, which uses a different home folder, so it won't find your worker config, API keys or git credentials, and jobs would run with SYSTEM privileges. Run it as your own (or a dedicated) user and pass `--config`.

```powershell
# Install NSSM (via scoop or choco)
scoop install nssm

# Store the fleet key as the account the service will use
hivemind-worker key set

# Configure service
$WorkerPath = (Get-Command hivemind-worker).Source
$Config = "$env:USERPROFILE\.hivemind\worker.json"
nssm install HiveMindWorker $WorkerPath "--config `"$Config`" start"
nssm set HiveMindWorker ObjectName ".\$env:USERNAME" "<your-windows-password>"
nssm set HiveMindWorker AppStdout "$env:USERPROFILE\.hivemind\worker-service.log"
nssm set HiveMindWorker AppStderr "$env:USERPROFILE\.hivemind\worker-service.log"
nssm start HiveMindWorker
```
*(For a Microsoft account, use `MicrosoftAccount\you@example.com` as the user name.)*

---

## 7. Security Model

| Mechanism | Detail |
|---|---|
| **Restricted REST Endpoints** | Only 4 typed endpoints exposed (`/health`, `/dispatch`, `/status`, `/cancel`). No arbitrary shell execution endpoints. |
| **Fleet Key Bearer Token** | All requests require `Authorization: Bearer <FLEET_KEY>` authentication. Unauthorized requests are rejected immediately with HTTP 401. Workers refuse to run without a key, and `hive` refuses to contact workers without one. |
| **Private-Network Allowlist** | Workers only accept connections from loopback, private LAN ranges and Tailscale (`100.64.0.0/10`); public internet addresses get HTTP 403 (configurable via `allowed_networks`). |
| **Browser & DNS-Rebinding Protection** | Workers serve no CORS, reject browser-originated requests (`Origin` / cross-site `Sec-Fetch-Site`), and only accept `Host` values that are IPs, the machine's own names, or its Tailscale MagicDNS name. |
| **Workspace Isolation** | Task execution is strictly isolated inside designated workspace directories (e.g. `~/hive-workspace/<run_id>`). |
| **Stripped Subprocess Environment** | Agent subprocesses run with a sanitized environment containing only essential system variables and AI provider keys. Personal credentials and sensitive session tokens are stripped. |
| **Tailscale Mesh Encryption** | When communicating over Tailscale (`100.x.x.x`), all peer-to-peer traffic is end-to-end encrypted with WireGuard. |

---

## 8. Project Documentation

* **[Network & Machine Preparation Guide](NETWORK_SETUP.md)**: Detailed instructions for configuring Tailscale, local LAN firewalls, router settings, Git keys, and worker daemons.
* **[Architecture & Deep Analysis](DISTRIBUTED_AI_CODING_SYSTEM.md)**: Comprehensive analysis of distributed AI agent architectures, coordination patterns, and industry gap resolutions.
* **[Development Roadmap](ROADMAP.md)**: Multi-phase engineering roadmap, milestone deliverables, and validation history.

---

## 9. License

This project is licensed under the MIT License - see the [license.txt](license.txt) file for details.
