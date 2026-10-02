import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Optional

from hivemind.config.cluster import WorkerNode
from hivemind.runner.agents import build_agent_command
from hivemind.runner.state import WorkerRunState
from hivemind.ssh import SSHError, expand_remote_path, shlex_quote, ssh_run, ssh_write


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "strategy"


def branch_name(worker_id: str, strategy: str) -> str:
    return f"exp/{slugify(worker_id)}-{slugify(strategy)}"


def _job_export(name: str, value: str) -> str:
    return f"export {name}={shlex_quote(value)}\n"


def _worker_script() -> str:
    local = Path(__file__).with_name("worker.sh")
    if local.exists():
        return local.read_text(encoding="utf-8")
    return files("hivemind.runner").joinpath("worker.sh").read_text(encoding="utf-8")


def start_worker_job(
    worker: WorkerNode,
    *,
    run_id: str,
    repo_url: str,
    prompt: str,
    strategy: str,
    strategy_index: int,
    strategy_count: int,
    test_command: Optional[str],
    bench_command: Optional[str],
    model: Optional[str],
) -> WorkerRunState:
    """Upload the wrapper and start it with nohup. Returns immediately."""
    branch = branch_name(worker.id, strategy)
    state = WorkerRunState(
        worker_id=worker.id,
        host=worker.host,
        port=worker.port,
        user=worker.user,
        workspace_dir=worker.workspace_dir,
        agent_engine=worker.agent_engine,
        strategy=strategy,
        branch=branch,
        status="starting",
    )

    full_prompt = (
        f"{prompt.rstrip()}\n\n"
        f"You are pursuing strategy {strategy_index} of {strategy_count}: {strategy}. "
        "Take a distinct approach.\n"
        "Change code only. Do not commit, push, or change git remotes.\n"
    )

    try:
        agent_command = build_agent_command(worker.agent_engine, model=model)
        workspace = expand_remote_path(worker, worker.workspace_dir)
        remote_run_dir = f"{workspace.rstrip('/')}/.hive/runs/{run_id}"
        state.workspace_dir = workspace
        state.remote_run_dir = remote_run_dir

        job_env = "".join([
            _job_export("RUN_ID", run_id),
            _job_export("REPO_URL", repo_url),
            _job_export("WORKSPACE_DIR", workspace),
            _job_export("WORKER_ID", worker.id),
            _job_export("STRATEGY", strategy),
            _job_export("BRANCH", branch),
            _job_export("AGENT_ENGINE", worker.agent_engine),
            _job_export("AGENT_COMMAND", agent_command),
            _job_export("TEST_COMMAND", test_command or ""),
            _job_export("BENCH_COMMAND", bench_command or ""),
            _job_export("PROMPT_FILE", f"{remote_run_dir}/prompt.txt"),
            _job_export("RESULT_FILE", f"{remote_run_dir}/result.json"),
        ])

        ssh_run(worker, f"mkdir -p {shlex_quote(remote_run_dir)}", timeout=15)
        ssh_write(worker, f"{remote_run_dir}/worker.sh", _worker_script())
        ssh_write(worker, f"{remote_run_dir}/job.env", job_env)
        ssh_write(worker, f"{remote_run_dir}/prompt.txt", full_prompt)
        ssh_run(worker, f"chmod +x {shlex_quote(remote_run_dir + '/worker.sh')}", timeout=10)

        start = (
            f"nohup bash {shlex_quote(remote_run_dir + '/worker.sh')} "
            f">{shlex_quote(remote_run_dir + '/wrapper.log')} 2>&1 < /dev/null & echo $!"
        )
        launched = ssh_run(worker, start, timeout=20)
        pid = launched.stdout.strip().splitlines()[-1] if launched.stdout.strip() else ""
        ssh_write(worker, f"{remote_run_dir}/worker.pid", pid + "\n")
        state.status = "running"
    except (SSHError, ValueError) as exc:
        state.status = "failed"
        state.error = str(exc)
    return state


def read_worker_result(worker: WorkerNode, remote_run_dir: str) -> tuple[str, Optional[dict], str]:
    """Return (status, result_json_or_none, log_tail)."""
    result_path = f"{remote_run_dir}/result.json"
    log_path = f"{remote_run_dir}/wrapper.log"
    agent_log = f"{remote_run_dir}/agent.log"

    listed = ssh_run(
        worker,
        f"if [ -f {shlex_quote(result_path)} ]; then echo RESULT; cat {shlex_quote(result_path)}; "
        f"echo '---LOG---'; "
        f"if [ -f {shlex_quote(log_path)} ]; then tail -n 20 {shlex_quote(log_path)}; "
        f"elif [ -f {shlex_quote(agent_log)} ]; then tail -n 20 {shlex_quote(agent_log)}; fi; "
        f"else echo RUNNING; "
        f"if [ -f {shlex_quote(log_path)} ]; then echo '---LOG---'; tail -n 20 {shlex_quote(log_path)}; "
        f"elif [ -f {shlex_quote(agent_log)} ]; then echo '---LOG---'; tail -n 20 {shlex_quote(agent_log)}; fi; fi",
        timeout=15,
        check=False,
    )
    if listed.returncode != 0:
        raise SSHError(listed.stderr or listed.stdout or "status poll failed")

    output = listed.stdout
    log_tail = ""
    if "---LOG---" in output:
        body, log_tail = output.split("---LOG---", 1)
    else:
        body = output

    body = body.strip()
    if body.startswith("RESULT"):
        raw = body[len("RESULT"):].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "unknown", None, log_tail.strip()
        return str(data.get("status") or "unknown"), data, log_tail.strip()
    return "running", None, log_tail.strip()
