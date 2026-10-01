# HiveMind: Distributed AI Coding Agent Fleet

[![Status](https://img.shields.io/badge/Status-Early_Development-orange.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Target-Engines](https://img.shields.io/badge/Engines-Claude_Code_|_Antigravity_|_Aider-purple.svg)](#)

> **Transform multiple machines into a coordinated, parallel AI software engineering team.**  
> Orchestrate multiple computers to explore different coding strategies concurrently, share discoveries in real time, prune failing attempts, and synthesize the winning code into production.

---

## 1. What We Are Doing (In Brief)

**HiveMind** is an open-source distributed orchestrator for autonomous AI coding agents.

Instead of relying on a single AI agent working sequentially on one computer, HiveMind coordinates your existing fleet of machines (GPU workstations, cloud instances, and laptops) into an autonomous development team:

* **The Master Machine (Lead Architect):** Decomposes your high-level engineering goal into distinct implementation strategies and delegates them across available worker machines.
* **The Worker Machines (Developer Fleet):** Run independent, headless AI coding sessions (Claude Code, Antigravity CLI `agy`, Aider) in isolated Git branches. Each machine writes code, builds, and executes test suites on its local hardware.
* **The Synthesizer Engine:** Evaluates candidate solutions, prunes dead-end attempts early, shares real-time discoveries between workers, and merges the winning code—or combines the best parts of multiple branches—into your main codebase.

---

## 2. The Problem: Why Single-Agent Development Hits a Wall

Frontier AI coding agents are extraordinarily capable, but when applied to compute-heavy, performance-critical, or time-sensitive projects, the **single-machine, single-agent model breaks down**:

1. **The Sequential Trial-and-Error Bottleneck:** Complex engineering challenges (e.g., optimizing database throughput, deep refactoring, fixing race conditions) rarely have an obvious single solution. A single agent tries Approach A, gets stuck after 20 minutes, rolls back, and tries Approach B. Exploring three distinct approaches sequentially can easily take over an hour.
2. **Idle Computing Power:** Many developers and teams have access to multiple machines—a high-end workstation with a dedicated GPU, a multi-core cloud VPS, and a development laptop. Under existing tools, all but one machine sit completely idle while you wait on a single thread of execution.
3. **No Safety in Long-Horizon Tasks:** If a single agent wanders down a flawed design path or gets caught in a circular compiler error loop, the entire development session is wasted.

---

## 3. Current Solutions in the Market

The current landscape of AI development tools falls into three primary categories:

* **Single-Machine Coding CLIs (Claude Code, Aider, Cursor, Antigravity CLI):** Excellent at interactive coding inside a single project folder, but strictly single-process with no native mechanism to coordinate across multiple physical machines.
* **Enterprise Cloud Agent Platforms (Devin Enterprise, OpenHands Cloud):** Platforms that spin up sandboxed cloud virtual machines for GitHub issues. They are heavy, costly, cloud-locked, and cannot easily leverage your own physical hardware, local GPU clusters, or private on-premise networks.
* **In-Memory Multi-Agent Frameworks (LangGraph, AutoGen, CrewAI):** Frameworks designed to coordinate multiple AI personas. However, they operate primarily on text messages and chat threads inside a single Python process; they lack native primitives for managing distributed Git repositories, live compiler logs, hardware benchmarks, and multi-machine filesystem state.

---

## 4. Industry Gaps & Our Phased Architecture

HiveMind bridges these gaps through a phased technical roadmap, addressing each major limitation milestone by milestone:

```
                    +------------------------------------------+
                    |    Master Node (Lead Architect CLI)      |
                    |   - Strategy Planner & Synthesizer       |
                    |   - Progress Watchdog & Knowledge Bus    |
                    +--------------------+---------------------+
                                         |
                       [ Tailscale Mesh / Git Remotes ]
                                         |
          +------------------------------+------------------------------+
          |                              |                              |
          v                              v                              v
+--------------------+        +--------------------+        +--------------------+
| Worker 1 (GPU PC)  |        | Worker 2 (Server)  |        | Worker 3 (Laptop)  |
| Strategy A         |        | Strategy B         |        | Strategy C         |
| Headless CLI + Test|        | Headless CLI + Test|        | Headless CLI + Test|
| (Push Branch A)    |        | (Push Branch B)    |        | (Push Branch C)    |
+--------------------+        +--------------------+        +--------------------+
```

---

### Phase 1: The Core Swarm Engine (Targeting Gap 4 — Tooling Void)
* **The Gap:** Developers must currently choose between single-laptop terminal tools or complex enterprise cloud clusters. There is no simple tool to run an ad-hoc swarm across 2 to 5 personal machines.
* **What We Are Doing:**
  * Build a lightweight, frictionless CLI dispatcher that connects machines over **Tailscale** (secure peer-to-peer mesh with no open firewall ports) and **Git** (isolated branches).
  * Automatically sync code, checkout dedicated experiment branches (`exp/<worker-id>-<strategy>`), trigger headless AI coding sessions (Claude Code / `agy`), and report back test/benchmark outputs to the Master terminal.

---

### Phase 2: Cost & Hardware Engine (Targeting Gaps 3 & 5 — Token Burn & Blind Allocation)
* **The Gaps:**
  * *Uncontrolled Token Burn:* Running multiple agents in parallel burns API credits if an agent gets stuck in an error loop.
  * *Blind Hardware Allocation:* Existing runners treat every machine as identical, ignoring whether a node has a high-end GPU or 64 CPU cores.
* **What We Are Doing:**
  * **Progress-Aware Early Pruning Watchdog:** Monitors live compiler exit codes, test pass trajectories, and turn counts. If a worker stalls or loops on errors, HiveMind automatically terminates it early to save token costs and compute budget.
  * **Hardware-Aware Capability Routing:** Automatically matches subtasks to machine profiles declared in `cluster.json` (routing CUDA/model-heavy tasks to GPU workstations and heavy test builds to high-core servers).

---

### Phase 3: The Intelligence Layer (Targeting Gaps 1 & 2 — Siloed Agents & Winner-Take-All Waste)
* **The Gaps:**
  * *Isolated Silos:* Workers cannot share discoveries. If Worker 1 discovers a critical dependency bug or schema quirk, Workers 2 and 3 waste time making the exact same mistake.
  * *Winner-Take-All Waste:* Existing setups discard losing branches even if Worker 2 wrote a perfect test suite and Worker 3 wrote clean API documentation.
* **What We Are Doing:**
  * **Live Knowledge & Discovery Bus:** A real-time event stream that detects critical discoveries in worker logs and broadcasts them directly into the active prompt/scratchpad of all other running agents.
  * **Multi-Branch Hybrid Code Synthesis (AST Splicing):** When multiple workers produce partial solutions, the Master AI reviews the diffs, extracts non-conflicting components (e.g., algorithm from Worker 1 + test suite from Worker 2), and synthesizes a unified, verified Pull Request.

---

### Phase 4: Production Polish & Secure Sandboxing
* **What We Are Doing:**
  * Interactive Terminal User Interface (TUI) showing side-by-side streaming agent thoughts, test counters, and hardware metrics.
  * Containerized microVM sandboxing (using **gVisor `runsc`** or **E2B**) on worker nodes to safely isolate untrusted AI-generated code execution.

---

## 5. Installation & Setup

> 🚧 *HiveMind is currently in early active development. Setup instructions will be updated as Phase 1 stabilizes.*

### Prerequisites *(Coming Soon)*
* Python 3.10+
* Git
* Tailscale (for secure multi-machine networking)
* A supported coding CLI on worker machines (`claude-code`, `agy`, or `aider`)

---

## 6. Usage & Configuration

### Cluster Definition (`cluster.json`) *(Coming Soon)*

```json
{
  "cluster_name": "dev-fleet",
  "workers": [
    {
      "id": "worker-gpu-1",
      "host": "desktop-gpu.ts.net",
      "user": "developer",
      "workspace_dir": "~/workspace/project",
      "agent_engine": "claude-code",
      "capabilities": ["gpu", "cuda12"]
    },
    {
      "id": "worker-cpu-2",
      "host": "cloud-vps.ts.net",
      "user": "developer",
      "workspace_dir": "~/workspace/project",
      "agent_engine": "claude-code",
      "capabilities": ["high-cpu", "32-cores"]
    }
  ]
}
```

### CLI Command *(Under Active Development in Phase 1)*

```bash
# Package: hivemind | CLI command: hive
hive check --cluster cluster.json

# Example dispatch syntax
hive dispatch \
  --cluster cluster.json \
  --prompt "Optimize the database cache to achieve >100k req/s" \
  --strategies 3 \
  --test-command "cargo test"
```

---

## 7. Project Documentation

* **[Architecture & Detailed Analysis](DISTRIBUTED_AI_CODING_SYSTEM.md)**: Deep dive into distributed agent mechanics, theoretical foundations, and communication schemas.
* **[Development Roadmap](ROADMAP.md)**: Phased engineering milestones, task checklists, and technical deliverables.

---

## 8. Contributing & Community

Contributions, issues, and feature requests are welcome! Feel free to check the [Roadmap](ROADMAP.md) to see what is currently in progress.
