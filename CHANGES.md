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
