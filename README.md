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
  * [Workflow Overview](#-workflow-overview)
* [5. What Happens on a Worker Machine](#5-what-happens-on-a-worker-machine)
* [6. SSH Setup & Troubleshooting Guide](#6-ssh-setup--troubleshooting-guide)
  * [Is it necessary to open Port 22?](#is-it-necessary-to-open-port-22)
  * [How to Enable SSH & Open Port 22 on Worker Machines](#how-to-enable-ssh--open-port-22-on-worker-machines)
  * [Configuring Passwordless SSH Authentication](#configuring-passwordless-ssh-authentication)
  * [Using `~/.ssh/config` Host Aliases or Cloudflare Tunnels](#using-sshconfig-host-aliases-or-cloudflare-tunnels)
* [7. Project Documentation](#7-project-documentation)
* [8. Contributing & Community](#8-contributing--community)
* [9. License](#9-license)

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
* **Git remotes**: Master and workers must already be able to clone and push the shared origin (`repo_url`). HiveMind does not store git tokens. Do not install `hive` on workers.

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
hive --help
hivemind --help   # identical
```

---

### 🔁 Workflow Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          MASTER MACHINE (you)                               │
│                                                                             │
│  1. hive discover ──► scans LAN, probes SSH, saves ~/.hivemind/cluster.json │
│  2. hive check    ──► verifies SSH + Git + agent CLI on every worker        │
│  3. hive dispatch ──► uploads worker.sh + prompt, starts nohup job via SSH  │
│  4. hive status   ──► SSHes in, reads result.json, prints live table        │
│  5. hive merge    ──► git fetch origin <exp/branch>, diff preview, merge    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │ SSH (BatchMode, no password)
                    ┌──────────────▼───────────────┐
                    │       WORKER MACHINE(S)      │
                    │                              │
                    │  worker.sh (uploaded by      │
                    │  master each dispatch):      │
                    │                              │
                    │  a) clone / fetch origin     │
                    │  b) checkout exp/<w>-<s>     │
                    │  c) run aider / claude / agy │
                    │  d) run --test command       │
                    │  e) git commit + push branch │
                    │  f) write result.json        │
                    └──────────────────────────────┘
                           │ git push
                           ▼
                    ┌─────────────┐
                    │  Git Origin │   (GitHub / GitLab / self-hosted)
                    │  exp/<w>-<s>│ ◄── master fetches winner here
                    └─────────────┘
```

> **Current test setup (Stage 1A — [validated ✅](ROADMAP.md#phase-1-mvp-core-runner--hive-cli-the-foundation)):**
> - **Master:** Windows machine, `hive` CLI installed via `uv tool install -e .`
> - **Worker (`ari`):** Debian Linux machine on same LAN, passwordless SSH
> - **Agent:** `aider` with model **`openrouter/openrouter/free`**
>   - Requires `OPENROUTER_API_KEY` on the worker — see [Step 4](#step-4-point-the-cluster-at-a-shared-git-origin) for placement guidance
> - **Origin:** GitHub repo (SSH clone URL)

---

### Step 1: Discover Devices on Your Local Network

Scan your local Wi-Fi or Ethernet subnet to find active machines with open SSH ports and interactively configure your global cluster (`~/.hivemind/cluster.json`):

```bash
hive discover
```

Probes active IPs, checks SSH port 22, auto-detects remote CPU/RAM/Docker capabilities, and saves selected machines to your global cluster config.

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

### Step 4: Point the Cluster at a Shared Git Origin

Workers clone this repo, push `exp/<worker>-<strategy>` branches to it, and `hive merge` fetches the winner from origin. Set `repo_url` in `~/.hivemind/cluster.json` - or pass `--repo` on every `hive dispatch` call.

```json
{
  "cluster_name": "hive-local-fleet",
  "repo_url": "git@github.com:you/your-project.git",
  "workers": [
    {
      "id": "ari",
      "host": "192.168.1.20",
      "user": "developer",
      "workspace_dir": "~/workspace/your-project",
      "agent_engine": "aider"
    }
  ]
}
```

The coding CLI (`aider`, `claude`, or `agy`) must be installed on each **worker**. `hive` itself stays on the master only — workers do not install HiveMind.

> **Aider + OpenRouter (CURRENT TEST SETUP):**
>
> The default aider model is `openrouter/openrouter/free`. Workers need `OPENROUTER_API_KEY` in their environment.
> Export it **unconditionally** in `~/.bashrc` — place the line **before** any interactive-shell guard (`case $-`) that would skip it in non-login SSH sessions:
>
> ```bash
> # ~/.bashrc, before any 'case $-' guard:
> export OPENROUTER_API_KEY="sk-or-v1-..."
>
> # Or place it in ~/.env — the hive worker wrapper loads this file automatically
> ```
>
> If the key isn't found, aider tries to open a browser OAuth flow and hangs indefinitely.

### Step 5: Dispatch a Parallel Swarm Task

```bash
# Minimal — positional prompt, repo from cluster.json
hive dispatch "Fix the off-by-one error in parser.py" --test "python3 -m pytest -q"

# Override repo for this run
hive dispatch "Add retries to the HTTP client" \
  --repo git@github.com:you/sample.git \
  --workers <worker-name>

# Two workers, two explicitly named strategies
hive dispatch "Optimize the query engine for >100k req/s" \
  --strategy "simd" \
  --strategy "actor-queues"

# With benchmark
hive dispatch "Speed up sort()" \
  --test "python3 -m pytest -q" \
  --bench "python3 -m pytest --benchmark-json=bench.json"
```

| Flag | Description |
|------|-------------|
| `"<prompt>"` | *(required)* Task description — positional first arg |
| `--repo <url>` | Git SSH/HTTPS URL — overrides `cluster.json` `repo_url` |
| `--workers id1,id2` | Subset of cluster workers (default: all) |
| `--strategy NAME` | Repeatable — one strategy name per worker. Auto-names `strategy-1..N` if omitted |
| `--test <cmd>` | Shell command run after agent exits; exit code stored in `result.json` |
| `--bench <cmd>` | Benchmark command; JSON output stored alongside result |
| `--model <id>` | Override the agent model (e.g. `gemini/gemini-2.5-pro`) |

Dispatch is **async** — it starts `nohup` jobs over SSH and returns immediately. The coding agent does not own `git commit` or `git push`; the uploaded `worker.sh` wrapper does.

### Step 6: Watch Progress

```bash
hive status                                   # latest run
hive status --run 20261002-123000-ab12cd34    # specific run
```

Polls each worker's `result.json` over SSH and renders a live Rich table:

```
                     Run 20261002-081203-03189faa
┏━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┓
┃ Worker ┃ Strategy   ┃ Branch             ┃ Status    ┃ Tests ┃ Details ┃
┡━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━┩
│ ari    │ strategy-1 │ exp/ari-strategy-1 │ succeeded │ pass  │         │
└────────┴────────────┴────────────────────┴───────────┴───────┴─────────┘
```

### Step 7: Merge a Winner

Run this from a **local clone** of the same origin (the directory where you normally develop):

```bash
cd ~/projects/your-project   # must be a clone of the same repo_url
hive merge
```

HiveMind:
1. Shows the results table — pick the winning worker ID
2. Force-fetches the winning `exp/…` branch from origin
3. Shows a `git diff --stat` preview
4. Merges into your local `main`/`master` (no-ff commit)
5. Leaves `git push` to you

---



## 5. What Happens on a Worker Machine

This section explains exactly what `hive dispatch` triggers on the worker and how the result flows back to you. Nothing here requires HiveMind to be installed on the worker.

---

### Step 1 — Master uploads three files over SSH

Every dispatch creates a run directory on the worker:

```
~/hive-testing/.hive/runs/<run-id>/
├── worker.sh    ← the full execution script (POSIX sh, no hive required)
├── job.env      ← shell exports: RUN_ID, REPO_URL, BRANCH, AGENT_COMMAND, TEST_COMMAND, ...
└── prompt.txt   ← the task text (written to a file so shell quoting can't corrupt it)
```

Then the master fires it off and returns immediately:

```bash
nohup bash worker.sh > wrapper.log 2>&1 &
# master exits SSH; job keeps running in background under nohup
```

---

### Step 2 — worker.sh runs autonomously

`worker.sh` sources `job.env` and executes the full pipeline without any further input from the master:

```
worker.sh
│
├─ Load env vars
│     sources ~/.profile, ~/.bash_profile
│     extracts export lines from ~/.bashrc with grep
│     (bypasses the interactive-shell guard at top of ~/.bashrc)
│     also loads ~/.env if present
│
├─ Clone or fetch origin
│     if workspace is empty    → git clone <REPO_URL> <workspace>
│     if workspace exists      → verify origin matches REPO_URL, then git fetch
│     if origins differ        → abort (writes result.json: failed)
│
├─ Dirty tree check
│     only tracked modifications (modified/staged/deleted) block dispatch
│     untracked files (e.g. leftover .gitignore) are ignored
│
├─ Checkout base + experiment branch
│     git checkout -B main origin/main     (fast-forward base)
│     git checkout -B exp/ari-strategy-1   (fresh experiment branch)
│
├─ Write .gitignore entries
│     adds .hive/ and .aider* so hive run files are never committed
│
├─ Run the coding agent
│     e.g.: aider --model openrouter/openrouter/free \
│                  --message "$(cat prompt.txt)" \
│                  --yes-always --no-auto-commits --no-dirty-commits
│     stdout → agent.log
│     agent edits files only. It does NOT commit or push.
│
├─ Run the test command (if --test was given)
│     e.g.: python3 check.py  →  test.log
│     exit code saved as test_exit_code
│
├─ Commit
│     git add -A
│     git commit -m "hive: strategy-1 on ari (<run-id>)"
│     (skipped if agent made no file changes)
│
├─ Push branch to origin
│     git push --force origin exp/ari-strategy-1
│     → branch now lives on GitHub (or your git host)
│     failure here marks the worker as failed
│
└─ Write result.json  ← this is the completion signal
      {
        "status": "succeeded",
        "branch": "exp/ari-strategy-1",
        "agent_exit_code": 0,
        "test_exit_code": 0,
        "benchmark_path": null,
        "error": null
      }
```

> **Key design rule:** The coding agent (aider/claude/agy) **only edits files**. It never touches git.
> All git operations — checkout, commit, push — are done by `worker.sh`. This means:
> - The agent cannot push to the wrong branch
> - The agent cannot skip the test run
> - The agent cannot corrupt the branch name or run metadata

---

### Step 3 — hive status reads result.json over SSH

`result.json` is the **completion signal**. It does not exist while the job is still running.

```
hive status
  └─ SSH into worker
       ├─ cat .hive/runs/<run-id>/result.json
       │     found  → parse status, branch, test exit code → show table
       │     missing → job still running → show "running / unknown"
       └─ tail -20 agent.log  (shown in Details column on failure)
```

> **Note:** `hive check` is a separate command — it tests SSH connectivity, git version, and agent CLI presence. It has nothing to do with run results. Only `hive status` reads `result.json`.

---

### Step 4 — Code travels through origin, never directly between machines

```
WORKER                      GITHUB ORIGIN               MASTER (your local clone)
──────────────      ──────────────      ────────────────────────
aider edits files
git commit
git push ─────────› branch-name
writes result.json                                          hive status (SSH reads result.json)
                                                            hive merge
                                           exp/branch-name ‐‐‐‐‐‐‐‐‐— git fetch origin
                                                            git merge (into local main)
                                                            ↓ you review diff
                                                            git push  (you do this)
```

The master **never reads the worker's code files directly** over SSH. It only reads `result.json` and log tails. The code itself always travels through the shared git origin.

---

## 6. SSH Setup & Troubleshooting Guide

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
# Install OpenSSH Server
sudo apt update && sudo apt install openssh-server -y

# Enable and start the SSH service
sudo systemctl enable --now ssh

# Allow Port 22 in the firewall (UFW)
sudo ufw allow 22
```

#### On Windows (PowerShell as Administrator):
```powershell
# Install OpenSSH Server
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# Start the service and set to Automatic startup
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'

# Allow inbound Port 22 in Windows Firewall
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
  "repo_url": "git@github.com:you/tiny-add.git",
  "workers": [
    {
      "id": "ari",
      "host": "homelab",
      "user": "developer",
      "workspace_dir": "~/workspace/project",
      "agent_engine": "aider"
    }
  ]
}
```

HiveMind will route the connection using your existing SSH config rules, proxy commands, and identity files automatically.

---

## 7. Project Documentation

* **[Architecture & Deep Analysis](DISTRIBUTED_AI_CODING_SYSTEM.md)**: Deep dive into distributed agent mechanics, problem analysis, market landscape, and technical solutions for all 5 industry gaps.
* **[Development Roadmap](ROADMAP.md)**: Phased engineering milestones, task checklists, and technical deliverables.

---

## 8. Contributing & Community

Contributions, issues, and feature requests are welcome! Feel free to check the [Roadmap](ROADMAP.md) to see what is currently in progress.

---

## 9. License

This project is licensed under the MIT License - see the [LICENSE](license.txt) file for details.
