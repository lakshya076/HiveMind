import subprocess
from typing import Optional

from hivemind.config.cluster import WorkerNode


class SSHError(Exception):
    """Raised when a remote SSH command fails or times out."""

    def __init__(
        self,
        message: str,
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _decode(data: Optional[bytes]) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def ssh_args(worker: WorkerNode, connect_timeout: int) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-p", str(worker.port),
        f"{worker.user}@{worker.host}",
    ]


def ssh_run(
    worker: WorkerNode,
    remote_command: str,
    *,
    timeout: float = 15,
    stdin: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command on a worker over passwordless SSH."""
    connect_timeout = max(1, int(timeout))
    cmd = ssh_args(worker, connect_timeout) + [remote_command]
    payload = stdin.encode("utf-8") if stdin is not None else None
    try:
        result = subprocess.run(
            cmd,
            input=payload,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHError(
            f"SSH timed out talking to {worker.user}@{worker.host}:{worker.port}",
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
        ) from exc
    except FileNotFoundError as exc:
        raise SSHError("ssh is not installed or not on PATH") from exc

    result.stdout = _decode(result.stdout)
    result.stderr = _decode(result.stderr)

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise SSHError(
            f"SSH command failed on {worker.id} ({worker.host}): {detail}",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def ssh_write(worker: WorkerNode, remote_path: str, content: str, *, timeout: float = 20) -> None:
    """Write a UTF-8 file on the worker, creating parent directories."""
    ssh_run(worker, f"mkdir -p $(dirname {shlex_quote(remote_path)})", timeout=timeout)
    ssh_run(
        worker,
        f"cat > {shlex_quote(remote_path)}",
        stdin=content.replace("\r\n", "\n"),
        timeout=timeout,
    )


def expand_remote_path(worker: WorkerNode, path: str, *, timeout: float = 10) -> str:
    """Expand a leading ~ using the worker's HOME. Other paths are used as-is."""
    trimmed = path.strip()
    if trimmed == "~" or trimmed.startswith("~/"):
        home = ssh_run(
            worker,
            r"""bash -lc 'printf %s "$HOME"'""",
            timeout=timeout,
        ).stdout.strip()
        if not home:
            raise SSHError(f"Could not resolve HOME on {worker.id}")
        if trimmed == "~":
            return home
        return home.rstrip("/") + "/" + trimmed[2:]
    return trimmed


def shlex_quote(value: str) -> str:
    """POSIX-quote a string for a remote Linux/macOS shell."""
    return "'" + value.replace("'", "'\"'\"'") + "'"
