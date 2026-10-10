# HiveMind Network Setup & Machine Preparation Guide

This guide provides step-by-step instructions for preparing your computers and network before running `hive discover` and orchestrating your distributed AI agent fleet.

---

## 📑 Contents

* [1. Architecture Overview](#1-architecture-overview)
* [2. Approach A: Tailscale Mesh Network (Recommended)](#2-approach-a-tailscale-mesh-network-recommended)
  * [Why Tailscale?](#why-tailscale)
  * [Step 1: Install Tailscale on All Machines](#step-1-install-tailscale-on-all-machines)
  * [Step 2: Authenticate to the Same Tailnet](#step-2-authenticate-to-the-same-tailnet)
  * [Step 3: Verify Peer Reachability](#step-3-verify-peer-reachability)
* [3. Approach B: Local Area Network (Same Wi-Fi / Ethernet Subnet)](#3-approach-b-local-area-network-same-wi-fi--ethernet-subnet)
  * [Step 1: Verify Subnet Connectivity](#step-1-verify-subnet-connectivity)
  * [Step 2: Disable Wi-Fi AP / Client Isolation](#step-2-disable-wi-fi-ap--client-isolation)
  * [Step 3: Open Firewall Port 7422 on Worker Machines](#step-3-open-firewall-port-7422-on-worker-machines)
* [4. Worker Machine Pre-Flight Checklist](#4-worker-machine-pre-flight-checklist)
  * [1. Install Git & Configure Origin Push Auth](#1-install-git--configure-origin-push-auth)
  * [2. Install AI Coding Agents & Set API Keys](#2-install-ai-coding-agents--set-api-keys)
  * [3. Install & Start `hivemind-worker`](#3-install--start-hivemind-worker)
* [5. Verifying Fleet Connectivity](#5-verifying-fleet-connectivity)
* [6. Troubleshooting & Diagnostics](#6-troubleshooting--diagnostics)

---

## 1. Architecture Overview

HiveMind uses a **Zero-SSH HTTP REST Daemon architecture**:
* The **Master machine** runs the `hive` CLI.
* Every **Worker machine** runs the `hivemind-worker` daemon listening on port `7422`.
* The Master communicates with workers via authenticated HTTP calls (`POST /dispatch`, `GET /status`, `GET /health`) secured by your shared **Fleet API Key**.

To enable discovery and dispatch, all computers must be reachable over an IP network—either via a **Tailscale virtual mesh network** or a **Local Area Network (LAN)**.

```
       ┌────────────────────────────────────────────────────────┐
       │                   Master Machine (you)                 │
       │           Tailscale: 100.77.177.65 | LAN: 192.168.1.10 │
       └───────────────────────────┬────────────────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              │                                         │
              ▼                                         ▼
┌───────────────────────────┐             ┌───────────────────────────┐
│     Worker 1 (Linux)      │             │    Worker 2 (Windows)     │
│ Tailscale: 100.101.58.76  │             │ Tailscale: 100.115.119.6  │
│ LAN: 192.168.1.20:7422    │             │ LAN: 192.168.1.35:7422    │
└───────────────────────────┘             └───────────────────────────┘
```

---

## 2. Approach A: Tailscale Mesh Network (Recommended)

### Why Tailscale?

Tailscale is the **recommended** networking approach for HiveMind because:
1. **Zero Firewall & Router Configuration:** Automatically traverses NATs, CGNATs, firewalls, and home routers using WireGuard.
2. **Multi-Location Support:** Connects local workstations, remote cloud VMs, home servers, and laptops across different Wi-Fi networks seamlessly.
3. **Instant Peer Discovery:** HiveMind queries the local Tailscale daemon (`tailscale status --json`) to discover all fleet nodes in $<0.1\text{s}$ without scanning subnet ranges.
4. **Static Virtual IPs:** Each node gets a persistent `100.x.y.z` IP that never changes between network reboots or location switches.
5. **End-to-End Encryption:** All traffic between nodes is encrypted peer-to-peer with WireGuard keys.

---

### Step 1: Install Tailscale on All Machines

Install Tailscale on the Master machine and every Worker machine:

* **🐧 Ubuntu / Debian Linux:**
  ```bash
  curl -fsSL https://tailscale.com/install.sh | sh
  ```

* **🪟 Windows:**
  Download and run the installer from [tailscale.com/download/windows](https://tailscale.com/download/windows), or use `winget`:
  ```powershell
  winget install Tailscale.Tailscale
  ```

* **🍎 macOS:**
  Install from the Mac App Store or download from [tailscale.com/download/mac](https://tailscale.com/download/mac).

---

### Step 2: Authenticate to the Same Tailnet

Log in with your account on every computer so they join the same private Tailnet:

* **On Linux:**
  ```bash
  sudo tailscale up
  ```
  *(Click the authorization URL printed in terminal to log in).*

* **On Windows / macOS:**
  Click the Tailscale system tray menu icon $\to$ **Log In**.

---

### Step 3: Verify Peer Reachability

On your Master machine, check that all worker devices appear in your Tailscale status:

```bash
tailscale status
```

Output should show your active peers:
```text
100.77.177.65   Artemis              you@  windows  -
100.101.58.76   ari                  you@  linux    active; direct 192.168.1.20:41641
100.115.119.6   Hermes               you@  windows  active; direct 192.168.1.35:41641
```

Test a quick ping to a worker's `100.x.y.z` IP:
```bash
tailscale ping 100.101.58.76
```

Once Tailscale is connected, run:
```bash
hive discover --tailscale
```

---

## 3. Approach B: Local Area Network (Same Wi-Fi / Ethernet Subnet)

If all machines are located in the same physical space and you prefer not to use Tailscale, HiveMind can scan and probe over your local IPv4 subnet (e.g. `192.168.1.0/24`).

---

### Step 1: Verify Subnet Connectivity

Ensure all machines are connected to the same local router (either via Ethernet or the same Wi-Fi SSID).

* **Check Master IP:**
  * Windows: `ipconfig` (Look for IPv4 Address, e.g. `192.168.1.10`)
  * Linux: `ip addr` or `hostname -I` (e.g. `192.168.1.20`)

* **Test Ping from Master to Worker:**
  ```bash
  ping 192.168.1.20
  ```

---

### Step 2: Disable Wi-Fi AP / Client Isolation

> [!WARNING]
> Many consumer and office Wi-Fi routers have a feature called **"AP Isolation"**, **"Client Isolation"**, or **"Guest Mode"** enabled by default. This blocks wireless devices from communicating directly with each other on the LAN.

1. Open your router's admin portal (usually `http://192.168.1.1` or `http://192.168.0.1`).
2. Navigate to **Wireless Settings** / **Advanced Wi-Fi Settings**.
3. Ensure **AP Isolation / Client Isolation** is set to **Disabled / Off**.
4. Save and restart the router if prompted.

---

### Step 3: Open Firewall Port 7422 on Worker Machines

Workers must allow inbound TCP connections on port `7422` from the local network.

#### 🐧 On Ubuntu / Debian Linux:
```bash
# If using UFW (Uncomplicated Firewall):
sudo ufw allow 7422/tcp

# If using iptables:
sudo iptables -A INPUT -p tcp --dport 7422 -j ACCEPT
```

#### 🪟 On Windows (Run PowerShell as Administrator):
```powershell
New-NetFirewallRule -Name 'HiveMind-Worker-Port-7422' `
                    -DisplayName 'HiveMind Worker Daemon (Port 7422)' `
                    -Enabled True `
                    -Direction Inbound `
                    -Protocol TCP `
                    -Action Allow `
                    -LocalPort 7422
```

#### 🍎 On macOS:
Open **System Settings** $\to$ **Network** $\to$ **Firewall** and ensure incoming connections for Python / `hivemind-worker` are permitted.

Once configured, discover local nodes from Master with:
```bash
hive discover --local
```

---

## 4. Worker Machine Pre-Flight Checklist

Before adding a worker machine to your cluster, perform these 3 setup steps on each worker node:

### 1. Install Git & Configure Origin Push Auth

Worker daemons push experimental branches (`exp/<worker>-<strategy>`) directly to your Git origin. The worker must have permission to push to the target repository.

* **Recommended:** Configure SSH Key authentication for GitHub / GitLab on the worker:
  ```bash
  # Check if SSH key exists on worker
  ls -la ~/.ssh/id_ed25519.pub

  # If not, generate one:
  ssh-keygen -t ed25519 -C "worker-name@github"
  cat ~/.ssh/id_ed25519.pub
  ```
  *(Add this public key to your GitHub/GitLab account under **SSH Keys** with write access).*

* **Auto-rewrite HTTPS URLs to SSH (Recommended on Workers):**
  ```bash
  git config --global url."git@github.com:".insteadOf "https://github.com/"
  git config --global user.name "HiveMind Worker"
  git config --global user.email "worker@hivemind.local"
  ```

---

### 2. Install AI Coding Agents & Set API Keys

Install at least one supported AI coding CLI on the worker:

#### Engine Options:
* **Aider (`aider`)**:
  ```bash
  pip install aider-chat   # or: uv tool install aider-chat
  ```
* **Antigravity CLI (`agy`)**:
  Install `agy` from your Antigravity distribution.
* **Claude Code (`claude-code`)**:
  ```bash
  npm install -g @anthropic-ai/claude-code
  ```

#### Set Provider API Keys:
Save your API key(s) in `~/.bashrc` or `~/.env` on the worker machine:

```bash
# Example ~/.env or ~/.bashrc on worker:
export OPENROUTER_API_KEY="sk-or-v1-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export GOOGLE_API_KEY="AIzaSy..."
```
*(The `hivemind-worker` daemon automatically reads API keys from `~/.env` and system environment variables).*

---

### 3. Install & Start `hivemind-worker`

1. On the **Master machine**, generate a fleet key if you haven't already:
   ```bash
   hive keygen
   # Example output: hive-key-5b0eb20d8420ff95174ed299d9b813acc712cbc8
   ```

2. On the **Worker machine**, install and initialize the worker daemon:
   ```bash
   cd hivemind-worker
   uv tool install -e .   # or: pip install -e .

   # Store the fleet key (hidden prompt), then start
   hivemind-worker key set
   hivemind-worker start
   ```

3. Enable persistent auto-start on boot:
   * **Linux:** Setup systemd service (see [README.md Service Guide](README.md#-linux-systemd-service))
   * **Windows:** Setup Task Scheduler / NSSM (see [README.md Windows Guide](README.md#-windows-background--task-scheduler--nssm))

---

## 5. Verifying Fleet Connectivity

After completing the network and worker setup:

### 1. Manual Health Check (from Master machine)

```bash
# Test health endpoint using curl
curl -s -H "Authorization: Bearer <FLEET_KEY>" http://<WORKER_IP>:7422/health
```

Expected JSON response:
```json
{
  "hostname": "ari",
  "os": "linux",
  "arch": "x86_64",
  "cpu_cores": 4,
  "ram_gb": 7.6,
  "engines": ["agy", "aider"],
  "git_ready": true,
  "version": "0.1.0",
  "status": "ready",
  "active_runs": []
}
```

### 2. Run Interactive Fleet Discovery

```bash
hive discover --tailscale    # if using Tailscale
# or:
hive discover --local        # if using Local LAN
```

Select the discovered nodes and save to `~/.hivemind/cluster.json`.

### 3. Run Cluster Audit

```bash
hive check
```

Output should confirm all nodes are **`ONLINE (Ready)`** with **`Git Ready: YES`** and detected engines displayed.

---

## 6. Troubleshooting & Diagnostics

| Symptom | Cause | Solution |
|---|---|---|
| **`Daemon not reachable on port 7422`** | Worker daemon is offline or blocked by firewall | 1. Check `systemctl status hivemind-worker` on worker.<br>2. Verify port rule: `sudo ufw allow 7422/tcp` or Windows Firewall rule.<br>3. Check if Tailscale is running on both machines. |
| **`Auth Failed: Worker rejected Fleet API Key`** | Fleet key on worker does not match master's key | Run `hive keygen` on master, then on the worker run `hivemind-worker key set`, paste the key, and restart the worker. |
| **`Source address not allowed` (HTTP 403)** | Master connects from an address outside the worker's `allowed_networks` (e.g. a public IP) | Connect over Tailscale or the LAN, or add the master's range to `allowed_networks` in the worker's `worker.json`. |
| **`Unrecognised Host header` (HTTP 400)** | Master reaches the worker via a DNS name the worker doesn't know | Use the worker's IP, hostname or MagicDNS name, or add the name to `allowed_hosts` in the worker's `worker.json`. |
| **`No active devices discovered` (Local scan)** | Wi-Fi AP Isolation enabled or wrong network interface | 1. Disable AP Isolation in router settings.<br>2. Run `hive discover --local` and explicitly select your active network interface. |
| **`Git push failed: could not read Username`** | Worker cannot authenticate with Git remote | 1. Add worker's `~/.ssh/id_ed25519.pub` to GitHub/GitLab SSH keys.<br>2. Run `git config --global url."git@github.com:".insteadOf "https://github.com/"` on worker. |
| **`No engines detected`** | Agent CLI binary not in PATH | Ensure `aider`, `agy`, or `claude` is installed at `~/.local/bin/` or in system `PATH`. Restart `hivemind-worker` service. |
