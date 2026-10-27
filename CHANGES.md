# Changes: `HiveMind` master (branch `security`)

Every change on this branch, one point each. Worker-side changes are in the `hivemind-worker` repo's `CHANGES.md` (branch `worker-security`).

---

## Security round 1: critical issues (master side)

Companion to the worker fixes for critical #1 (workers ran unauthenticated without a key) and #2 (browsers could drive workers).

### Fleet key required
1. New helper `_require_fleet_key()` in `cli.py`. It checks `~/.hivemind/fleet.key`, then `HIVEMIND_FLEET_KEY`.
2. `hive check` exits 1 with setup instructions when there is no key, before any network request.
3. `hive dispatch` does the same.
4. `hive status` does the same.
5. `hive merge` does the same.
6. A keyless master with a keyless worker can no longer "work" with no authentication at all.

### Documentation
7. `README.md` worker setup step 2 uses `hivemind-worker key set` (hidden prompt) instead of `start --key <KEY> --save`.
8. `README.md` systemd step 1 uses `key set`.
9. `README.md` Task Scheduler step 1 uses `key set`.
10. **`README.md` NSSM section fixed.** It installed the service as LocalSystem, so the worker started keyless with SYSTEM privileges. It now sets `ObjectName` and passes `--config`, with a warning box explaining why.
11. `README.md` Security Model table lists mandatory keys, the private-network allowlist, and browser/DNS-rebinding protection.
12. `NETWORK_SETUP.md` §4.3 uses `key set`, then `start`.
13. `NETWORK_SETUP.md` troubleshooting: the "Auth Failed" fix now uses `key set`.
14. `NETWORK_SETUP.md` troubleshooting: new row for `Source address not allowed` (HTTP 403).
15. `NETWORK_SETUP.md` troubleshooting: new row for `Unrecognised Host header` (HTTP 400).

### Repository
16. `.gitignore`: added `tests/` (tests are kept local).
17. Removed the stray `p.txt`.

---

## Security round 2: high-severity issues

### #3: `hive discover` no longer sends the fleet key to every device (option D)
18. New `SystemProber.probe_presence()` (`discovery/prober.py`). It sends an **unauthenticated** `GET /` and recognises a worker only if the response is JSON with `name == "HiveMind Worker Daemon"`.
19. `probe_presence` reports clear errors: not reachable, timeout, HTTP 403 (worker's `allowed_networks` refused us), non-JSON, or not a worker.
20. `discover` uses `probe_presence` for every found device. **No credentials are sent before you choose devices.**
21. Presence probes run in parallel (up to 32 at a time) instead of one after another.
22. The authenticated `/health` probe, which sends the key, runs **only for devices you select**, to read engines/CPU/RAM/git tags.
23. With no fleet key, `discover` still lists devices, warns that capabilities can't be read, and sends no `Authorization` header to anyone.
24. Choosing `q` (quit) means no device ever receives the key.
25. `all` selects only devices where a worker was detected.
26. The results table shows `DETECTED (v<version>)` / `NOT INSTALLED / OFFLINE` and the device OS. Engines are no longer shown before selection, because reading them needs the key.
27. A note under the table says the key is only sent to selected devices.
28. New option `discover --port/-p` (default `7422`) sets the worker port to probe. It is also the default in the per-device port prompt.
29. The per-device "Daemon Port" prompt uses `IntPrompt`, so non-numeric input re-prompts instead of crashing.
30. Device hostnames, OS names, versions, capability strings and probe errors in `discover` output are passed through `rich.markup.escape`, so a hostile device name can't inject fake coloured text or links (a partial fix for #11).

### Testing (local, not committed)
31. Round 1: 7 tests. `check`/`dispatch`/`status`/`merge` refuse without a key, with HTTP client creation trapped to prove nothing is sent. `HIVEMIND_FLEET_KEY` is honoured. The real `hive check` runs against a real hardened worker process (ONLINE with the right key, Auth Failed with the wrong one).
32. Round 2: 7 tests. Fake devices are real HTTP servers on `127.0.0.1/.2/.3` that record every request's `Authorization` header. They show:
    - all devices are probed without credentials;
    - only the selected device receives the key;
    - `all` skips non-workers;
    - quitting sends the key to nobody;
    - no key means no auth header at all;
    - hostile markup in hostnames is shown literally.
33. Round 2: `discover` against a real hardened `hivemind-worker` process detects it through the worker's new network guards and reads its capabilities after selection.
34. Total: 14 passed.
35. Mutation-checked: disabling the key guard makes 4 round-1 tests fail.
36. Mutation-checked: making the presence probe send the key again (the old behaviour) makes 3 round-2 tests fail.

---

## Round 3: high-impact accuracy and performance upgrades

### Accuracy and correctness (A4, A10, A11, A12, A15)
37. `branch_name()` (`runner/remote.py`) scopes experiment branches by run ID (`exp/<run_id>/<worker>-<strategy>`), so later dispatches no longer overwrite earlier runs' branches on `origin`.
38. `WorkerRunState` (`runner/state.py`) records `pushed`, `commit`, `bench_exit_code`, and `benchmark_data` alongside `benchmark_path`.
39. `refresh_run()` (`runner/status.py`) reads `benchmark_data`, `bench_exit_code`, `pushed`, and `commit` from the worker's `/status/<run_id>` response.
40. `_print_run_table()` (`cli.py`) renders a `Benchmark` column when `--bench` is used or when any worker reports benchmark results.
41. `_status_style()` and `TERMINAL_STATUSES` handle `"no_changes"` and `"cancelled"` so `hive status` recognises finished runs and styles both states clearly.
42. `hive merge` filters out workers whose status is `"no_changes"` or `"cancelled"` or where `pushed` is `False`, so it does not offer branches that were never pushed to `origin`.
43. `hive discover` loads the existing `cluster.json` if present, preserves `cluster_name` and `repo_url`, and merges newly configured workers into the existing worker list by ID or `(host, port)` instead of wiping unselected workers.
44. `.gitignore`: added `*.jsonl` and removed the stray conversation `.jsonl` file from the repository root.

### Performance (P1)
45. `hive check` (`cli.py`) probes all configured workers concurrently via `ThreadPoolExecutor` instead of sequentially one by one.
46. `refresh_run()` (`runner/status.py`) polls all workers in a run concurrently via `ThreadPoolExecutor`, speeding up both `hive status` and `hive merge`.

### Testing (local, not committed)
47. Added 5 tests in `tests/test_accuracy_perf.py` covering run-scoped branch names, benchmark result rendering and terminal status handling in `hive status`, `hive discover` config merging (`repo_url` and existing workers preserved), parallel worker polling in `hive check` and `refresh_run()`, and `hive merge` filtering of unpushed/`no_changes` workers.
48. Total master suite: 19 passed.

---

## Round 4: remaining high-severity security fixes

### S2: Git option and remote-helper injection via `repo_url` and `branch`
49. `resolve_repo_url()` (`runner/dispatch.py`) rejects `repo_url` values starting with `-`, using git remote helper protocols (`ext::`, `fd::`), or containing whitespace/control characters before any worker is contacted.
50. New `validate_branch_ref()` (`runner/dispatch.py`) validates branch names before `hive merge` (`cli.py`) passes them to `git fetch` or `git merge`, preventing a rogue worker response from injecting git options on the master.
51. Added S2 tests in `tests/test_accuracy_perf.py` (total master suite: 20 passed).

---

## Round 5: medium-severity accuracy, performance, and security upgrades

### A13: `hive merge` working-tree safety and default branch detection
52. New `local_working_tree_dirty()` (`runner/status.py`) checks `git status --porcelain --untracked-files=no`, and `hive merge` (`cli.py`) refuses to run when tracked files have uncommitted edits.
53. `local_base_branch()` (`runner/status.py`) checks `refs/remotes/origin/HEAD` first, then `main` and `master`, and then the currently checked-out non-`exp/` branch so repositories using `develop` or `trunk` merge cleanly.
54. `hive merge` previews commits and diff statistics against the local target branch (`<base>..origin/<branch>`) when `<base>` exists locally, matching the branch `git merge` actually merges into.
55. Added A13 end-to-end test in `tests/test_accuracy_perf.py`.

### A16 + P8: Cross-platform LAN discovery, worker port probing, and large-subnet cap
56. New `parse_arp_output()` (`discovery/scanner.py`) parses Windows, Linux, and macOS `arp -a` output formats and invokes `["arp", "-a"]` without `shell=True`.
57. Broadcast (`255.255.255.255`, `*.255`, `ff:ff:ff:ff:ff:ff`), multicast (`224.0.0.0/4`), loopback, link-local, and incomplete ARP entries are filtered out so they are no longer listed as candidate worker machines.
58. `NetworkScanner.resolve_hostname()` no longer calls `socket.setdefaulttimeout()`, avoiding thread-unsafe process-wide socket timeout mutation during concurrent scans.
59. `NetworkScanner.probe_host()` and `scan_subnet()` probe the configured worker `daemon_port` (default `7422`) instead of SSH port `22`, so workers without an SSH server are found on LAN scans.
60. `NetworkScanner._candidate_hosts()` caps oversized subnets (>1,024 addresses, such as `/16` VPN or Docker networks) to the `/24` slice around the interface IP plus known ARP entries in the subnet.
61. Added A16 + P8 unit and integration tests in `tests/test_accuracy_perf.py` (total master suite: 22 passed).

---

## Round 6: deep security audit fixes (High severity)

### V1: Reject UNC network share `repo_url` values on master
62. `resolve_repo_url()` (`runner/dispatch.py`) rejects UNC network share paths (`\\server\share`, `//server/share`, `file:////...`) before dispatching to workers.
63. Added `test_v1_master_rejects_unc_repo_urls` in `tests/test_accuracy_perf.py`.
