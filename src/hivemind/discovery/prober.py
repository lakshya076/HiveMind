import subprocess
from typing import List, Tuple, Optional


class SystemProber:
    """Probes local or remote machines over SSH to detect accurate hardware, OS, and tool tags."""

    # Cross-platform single-line Python probe command that works in bash, sh, cmd, and PowerShell
    PROBE_CMD = (
        'python3 -c "'
        'import os, sys, platform, shutil, subprocess; '
        'tags = [f\'os-{platform.system().lower()}\', f\'arch-{platform.machine().lower()}\']; '
        'cores = os.cpu_count() or 1; '
        'tags.append(f\'{cores}-cores\'); '
        'tags.append(\'high-cpu\' if cores >= 16 else (\'standard-cpu\' if cores <= 4 else \'multi-core\')); '
        'tools = [(\'docker\', \'docker\'), (\'claude\', \'engine:claude-code\'), (\'agy\', \'engine:agy\'), (\'aider\', \'engine:aider\'), (\'uv\', \'uv\')]; '
        '[tags.append(tag) for cmd, tag in tools if shutil.which(cmd)]; '
        'print(\'__HIVE_PROBE__:\' + \',\'.join(tags))" '
        '2>/dev/null || '
        'python -c "'
        'import os, sys, platform, shutil; '
        'tags = [f\'os-{platform.system().lower()}\', f\'arch-{platform.machine().lower()}\']; '
        'cores = os.cpu_count() or 1; '
        'tags.append(f\'{cores}-cores\'); '
        'tags.append(\'high-cpu\' if cores >= 16 else (\'standard-cpu\' if cores <= 4 else \'multi-core\')); '
        'tools = [(\'docker\', \'docker\'), (\'claude\', \'engine:claude-code\'), (\'agy\', \'engine:agy\'), (\'aider\', \'engine:aider\'), (\'uv\', \'uv\')]; '
        '[tags.append(tag) for cmd, tag in tools if shutil.which(cmd)]; '
        'print(\'__HIVE_PROBE__:\' + \',\'.join(tags))"'
    )

    @classmethod
    def probe_remote_detailed(
        cls, host: str, user: str, port: int = 22, timeout: float = 4.0
    ) -> Tuple[List[str], Optional[str]]:
        """Runs a fast non-interactive SSH probe command and returns (tags, error_detail)."""
        try:
            cmd = [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", f"ConnectTimeout={int(timeout)}",
                "-p", str(port),
                f"{user}@{host}",
                cls.PROBE_CMD,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if res.returncode == 0 and "__HIVE_PROBE__:" in res.stdout:
                line = [l for l in res.stdout.splitlines() if "__HIVE_PROBE__:" in l][-1]
                raw_tags = line.split("__HIVE_PROBE__:", 1)[1]
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
                return tags, None

            # Diagnose failure from stderr or exit code
            stderr = res.stderr.lower() if res.stderr else ""
            if "permission denied" in stderr:
                return [], "SSH Authentication Required: Public key not authorized on target device"
            if "connection timed out" in stderr or "timed out" in stderr:
                return [], "Connection timed out: Target machine did not respond in time"
            if "connection refused" in stderr:
                return [], "Connection refused: SSH server is not accepting connections"
            if res.returncode != 0:
                return [], f"SSH command failed (exit code {res.returncode})"

        except subprocess.TimeoutExpired:
            return [], "SSH connection timed out"
        except Exception as exc:
            return [], f"Probe error: {exc}"

        return [], "No hardware profile tags returned by target"

    @classmethod
    def probe_remote(cls, host: str, user: str, port: int = 22, timeout: float = 4.0) -> List[str]:
        """Backward-compatible helper returning just tags list."""
        tags, _ = cls.probe_remote_detailed(host, user, port, timeout)
        return tags
