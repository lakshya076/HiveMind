# Changes

## Security hardening, round 1 (branch `security`)

Master-side companion to `hivemind-worker` branch `worker-security`, which fixes the two critical issues: workers ran unauthenticated when no key was set, and any web page could drive a worker through a browser. See that repo's `CHANGES.md` for the worker details.

### `hive` refuses to contact workers without a fleet key
- `hive check`, `dispatch`, `status` and `merge` now exit with an error, before any network request, when neither `~/.hivemind/fleet.key` nor `HIVEMIND_FLEET_KEY` is set.
- Workers now always require a key. Previously a keyless master paired with a keyless worker "worked" with no authentication at all.

### Documentation
- Worker setup uses `hivemind-worker key set` (hidden prompt) instead of `start --key <KEY>`, keeping the key out of shell history and process lists. This applies to the README Quickstart, the systemd and Task Scheduler sections, and NETWORK_SETUP §4.3.
- **NSSM instructions fixed**. They installed the worker service as LocalSystem, which didn't find the user's config, so the worker started keyless with SYSTEM privileges. The service now runs as the user (`ObjectName`) with an explicit `--config`.
- The Security Model table now covers mandatory keys, the private-network allowlist, and browser/DNS-rebinding protection.
- The troubleshooting table covers the new worker responses: `Source address not allowed` (403) and `Unrecognised Host header` (400).

### Testing
- Local tests (kept out of git via `.gitignore`): 7 passed. They cover:
  - each of `check`/`dispatch`/`status`/`merge` refusing without a key, with HTTP client creation trapped to prove no request is sent;
  - `HIVEMIND_FLEET_KEY` satisfying the requirement;
  - a cross-repo integration test running the real `hive check` against a real hardened `hivemind-worker` process with a matching key (ONLINE) and a wrong key (Auth Failed).
- Mutation-checked: disabling the key guard makes 4 tests fail.
