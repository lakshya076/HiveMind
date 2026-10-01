# HiveMind: Distributed AI Coding Agent Fleet

[![Status](https://img.shields.io/badge/Status-Early_Development-orange.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Target-Engines](https://img.shields.io/badge/Engines-Claude_Code_|_Antigravity_|_Aider-purple.svg)](#)

> **Transform multiple machines into a coordinated, parallel AI software engineering team.**  
> Orchestrate multiple computers to explore different coding strategies concurrently, share discoveries in real time, prune failing attempts, and synthesize the winning code into production.

---

## 📑 Contents

* [1. Overview](#1-overview)
* [2. Key Features](#2-key-features)
* [3. Installation & Setup](#3-installation--setup)
* [4. How to Run the `hive` CLI](#4-how-to-run-the-hive-cli)
* [5. SSH Setup & Troubleshooting Guide](#5-ssh-setup--troubleshooting-guide)
  * [Is it necessary to open Port 22?](#is-it-necessary-to-open-port-22)
  * [How to Enable SSH & Open Port 22 on Worker Machines](#how-to-enable-ssh--open-port-22-on-worker-machines)
  * [Configuring Passwordless SSH Authentication](#configuring-passwordless-ssh-authentication)
  * [Using `~/.ssh/config` Host Aliases or Cloudflare Tunnels](#using-sshconfig-host-aliases-or-cloudflare-tunnels)
* [6. Project Documentation](#6-project-documentation)
* [7. Contributing & Community](#7-contributing--community)
* [8. License](#8-license)

---

## 1. Overview

**HiveMind** (CLI: `hive`, Package: `hivemind`) is an open-source distributed orchestrator for autonomous AI coding agents.

Instead of relying on a single AI agent working sequentially on one computer, HiveMind coordinates your existing fleet of machines (GPU workstations, cloud instances, and laptops) into an autonomous development team:

* **Master Node (Lead Architect):** Decomposes your high-level engineering goal into distinct implementation strategies and delegates them across available worker machines.
* **Worker Fleet (Developer Fleet):** Run independent, headless AI coding sessions (Claude Code, Antigravity CLI `agy`, Aider) in isolated Git branches. Each machine writes code, builds, and executes test suites on its local hardware.
* **Synthesizer Engine:** Evaluates candidate solutions, prunes dead-end attempts early, shares real-time discoveries between workers, and merges the winning code—or combines the best parts of multiple branches—into your main codebase.

---

## 2. Key Features

* **Ad-Hoc Multi-Machine Dispatch**: Connect 2–5 mixed machines with zero complex cloud infrastructure—currently operates over direct SSH across your local Wi-Fi / Ethernet network (with cloud/mesh network support coming in future phases).
* **Competitive Strategy Exploration**: Run $N$ parallel implementations of a difficult problem (e.g., SIMD vs. Actor Queues vs. Memory-Mapped files) and benchmark them automatically.
* **Progress-Aware Early Pruning**: Watchdog automatically halts stuck or regressing agent runs, preventing runaway token costs.
* **Live Cross-Worker Knowledge Bus**: Insights or bug discoveries found on one node are broadcast to all other running workers in real time.
* **Multi-Branch Hybrid Synthesis**: Combines the best parts from different workers (e.g., algorithm from Worker 1 + test suite from Worker 2) into one unified Pull Request.
* **Hardware-Aware Routing**: Directs GPU-intensive or heavy build tasks to machines with matching hardware profiles.

---

## 3. Installation & Setup

Follow these steps to clone the repository and set up HiveMind:

### Prerequisites
* **Python**: 3.10 or higher
* **Git**: Installed and configured on your system
* **SSH**: Installed and accessible from your command line
* **Coding Agents (on worker machines)**: At least one supported coding CLI installed on target machines (`claude-code`, `agy`, or `aider`)

### Clone and Install

```bash
git clone https://github.com/your-username/HiveMind.git
cd HiveMind
```

#### Option A: Fast Global Setup with `uv` (Recommended)
```bash
uv tool install -e .
```

#### Option B: Standard Setup with `pip`
```bash
python -m venv .venv
# Activate virtual environment:
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# Linux / macOS:        source .venv/bin/activate
pip install -e .
```

> **Tip with `uv`:** If you prefer running without global install, you can also run directly inside the repo using `uv run hive <command>` (e.g. `uv run hive discover`).

After installation, both the **`hive`** and **`hivemind`** commands will be available globally in your terminal.

---

## 4. How to Run the CLI (`hive` / `hivemind`)

HiveMind can be run using either the **`hive`** or **`hivemind`** command interchangeably:

```bash
# Both commands are identical:
hive --help
hivemind --help
```

### Step 1: Discover Devices on Your Local Network
Scan your local Wi-Fi or Ethernet subnet to find active machines with open SSH ports and interactively configure your global cluster (`~/.hivemind/cluster.json`):

```bash
hive discover
```
* Probes active IPs, checks SSH port 22, auto-detects remote CPU/RAM/Docker capabilities, and saves selected machines to your global cluster configuration.

### Step 2: Inspect Your Configured Cluster
View all configured worker nodes, their SSH targets, assigned AI coding engines, and hardware capabilities:

```bash
hive list
```

### Step 3: Run Cluster Health & Readiness Checks
Test SSH reachability, Git installation, and AI agent CLI readiness across all configured machines:

```bash
hive check
```

### Step 4: Dispatch a Parallel Swarm Task *(Phase 1 In Progress)*
Dispatch multi-strategy parallel exploration across your fleet:

```bash
hive dispatch \
  --prompt "Optimize the query execution engine in src/engine.rs to achieve >100k req/s" \
  --strategies 3 \
  --test-command "cargo test" \
  --benchmark-command "cargo bench -- --json"
```

---

## 5. SSH Setup & Troubleshooting Guide

> ℹ️ **Local Network Focus (Current Phase):** HiveMind currently operates over **direct SSH for local network machines connected on the same Wi-Fi / Ethernet subnet** to trigger isolated Git branching and headless coding agent sessions on your worker nodes. Support for remote/cloud machines across different networks will be expanded in subsequent phases.

---

### Is it necessary to open Port 22?

* **On Local Networks (Same Wi-Fi / Ethernet):**  
  **Yes.** The worker machine must have an SSH server running and listening on Port 22 (or a designated custom port), and the worker's local OS firewall must allow inbound TCP connections on that port.
* **Across Remote Networks / Internet (Future Phases):**  
  When using mesh networks (such as NetBird or Tailscale) or SSH tunnels in future phases, public router port-forwarding is not required because the tunnel handles secure peer-to-peer routing.

---

### How to Enable SSH & Open Port 22 on Worker Machines

#### On Ubuntu / Debian Linux:
```bash
# 1. Install OpenSSH Server
sudo apt update && sudo apt install openssh-server -y

# 2. Enable and start the SSH service
sudo systemctl enable --now ssh

# 3. Allow Port 22 in the firewall (UFW)
sudo ufw allow 22
```

#### On Windows (PowerShell as Administrator):
```powershell
# 1. Install OpenSSH Server
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# 2. Start the service and set to Automatic startup
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'

# 3. Allow inbound Port 22 in Windows Firewall
New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

#### On macOS:
1. Open **System Settings** $\to$ **General** $\to$ **Sharing**.
2. Toggle **Remote Login** to **ON**.

---

### Configuring Passwordless SSH Authentication

HiveMind executes tasks automatically and requires **passwordless SSH key authentication** between your Master machine and Worker nodes.

If `hive check` shows `AUTH REQUIRED` or `Permission denied (publickey)`:

#### Step 1: Generate an SSH Key on your Master Machine (if you don't have one):
```bash
ssh-keygen -t ed25519
```
*(Press Enter on all prompts to accept defaults).*

#### Step 2: Copy your Public Key to the Worker Machine:

* **From Linux / macOS Master:**
  ```bash
  ssh-copy-id <worker-username>@<worker-ip>
  ```
* **From Windows PowerShell Master:**
  ```powershell
  type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh <worker-username>@<worker-ip> "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
  ```
  *(Enter the worker machine's password once to authorize the key).*

#### Step 3: Verify Passwordless Login:
```bash
ssh <worker-username>@<worker-ip> "echo OK"
```
If this prints `OK` without asking for a password, run `hive check` to confirm that all nodes show `SSH Reachable: YES` and `Git Ready: YES`.

---

### Using `~/.ssh/config` Host Aliases or Cloudflare Tunnels

If you have pre-configured SSH host aliases or Cloudflare Tunnels in your `~/.ssh/config` (for example: `Host homelab`):

You can use the alias name directly as the `host` in `~/.hivemind/cluster.json`:

```json
{
  "cluster_name": "hive-fleet",
  "workers": [
    {
      "id": "ari",
      "host": "homelab",
      "user": "developer",
      "workspace_dir": "~/workspace/project",
      "agent_engine": "claude-code"
    }
  ]
}
```

HiveMind will route the connection using your existing SSH config rules, proxy commands, and identity files automatically.

---

## 6. Project Documentation

* **[Architecture & Deep Analysis](DISTRIBUTED_AI_CODING_SYSTEM.md)**: Deep dive into distributed agent mechanics, problem analysis, market landscape, and technical solutions for all 5 industry gaps.
* **[Development Roadmap](ROADMAP.md)**: Phased engineering milestones, task checklists, and technical deliverables.

---

## 7. Contributing & Community

Contributions, issues, and feature requests are welcome! Feel free to check the [Roadmap](ROADMAP.md) to see what is currently in progress.

---

## 8. License

This project is licensed under the MIT License - see the [LICENSE](license.txt) file for details.
