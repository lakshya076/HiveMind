import json

import httpx
import pytest
from click.testing import CliRunner

from hivemind.cli import cli
from hivemind.config.cluster import ClusterConfig, WorkerNode
from hivemind.runner.dispatch import DispatchError, dispatch_run, validate_base_branch
from hivemind.runner.state import RunState, WorkerRunState
from hivemind.runner.status import refresh_run

ARI = WorkerNode(id="ari", host="10.0.0.2")
HERMES = WorkerNode(id="hermes", host="10.0.0.3")
CLUSTER = ClusterConfig(repo_url="r", workers=[ARI, HERMES])
RUN_ID = "20261107-120000-abcd1234"


def health(features, version="0.3.0"):
    return {"status": "ready", "engines": ["claude-code"], "git_ready": True, "version": version, "features": features}


def fleet(per_host):
    """/health answers per host; /dispatch always accepts."""
    def handler(request):
        if request.url.path == "/health":
            answer = per_host[request.url.host]
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(200, json=answer)
        return httpx.Response(200, json={"status": "starting"})
    return handler


def posts(fake_http):
    return [json.loads(r.content) for r in fake_http.requests if r.url.path == "/dispatch"]


# ---------------------------------------------------------------- --base


@pytest.mark.parametrize("base", ["main", "develop", "feature/login", "release/1.2"])
def test_valid_base_branches(base):
    assert validate_base_branch(f" {base} ") == base


@pytest.mark.parametrize("base", ["--upload-pack=x", "a..b", "a b", "a@{1}", "x.lock"])
def test_invalid_base_branches(base):
    with pytest.raises(DispatchError):
        validate_base_branch(base)


def test_base_branch_is_sent_and_saved(fleet_key, fake_http):
    fake_http.handler = fleet({ARI.host: health(["base_branch", "logs"]), HERMES.host: health(["base_branch"])})

    run = dispatch_run(CLUSTER, "p", repo_url="r", workers=[ARI, HERMES], strategies=["a", "b"], base_branch="develop")

    assert [b["base_branch"] for b in posts(fake_http)] == ["develop", "develop"]
    assert RunState.load(run.run_id).base_branch == "develop"


def test_base_branch_refused_if_any_worker_would_ignore_it(fleet_key, fake_http):
    # An older worker drops unknown fields and would quietly start from main.
    fake_http.handler = fleet({ARI.host: health(["base_branch"]), HERMES.host: health([], version="0.2.0")})

    with pytest.raises(DispatchError, match="hermes: hivemind-worker 0.2.0 does not support it"):
        dispatch_run(CLUSTER, "p", repo_url="r", workers=[ARI, HERMES], strategies=["a", "b"], base_branch="develop")
    assert posts(fake_http) == []


def test_base_branch_refused_if_a_worker_cannot_be_checked(fleet_key, fake_http):
    fake_http.handler = fleet({
        ARI.host: health(["base_branch"]),
        HERMES.host: httpx.ConnectError("refused", request=httpx.Request("GET", "http://10.0.0.3")),
    })
    with pytest.raises(DispatchError, match="hermes: could not check"):
        dispatch_run(CLUSTER, "p", repo_url="r", workers=[ARI, HERMES], strategies=["a", "b"], base_branch="develop")
    assert posts(fake_http) == []


def test_without_base_no_health_check_and_no_field(fleet_key, fake_http):
    fake_http.handler = fleet({})
    dispatch_run(CLUSTER, "p", repo_url="r", workers=[ARI], strategies=["a"])
    assert [r.url.path for r in fake_http.requests] == ["/dispatch"]
    assert "base_branch" not in posts(fake_http)[0]


def test_one_feature_check_per_worker_even_with_many_strategies(fleet_key, fake_http):
    fake_http.handler = fleet({ARI.host: health(["base_branch"])})
    dispatch_run(CLUSTER, "p", repo_url="r", workers=[ARI, ARI, ARI], strategies=["a", "b", "c"], base_branch="dev")
    assert sum(r.url.path == "/health" for r in fake_http.requests) == 1


def test_cli_base_flag(fleet_key, fake_http):
    ClusterConfig(repo_url="r", workers=[ARI]).save()
    fake_http.handler = fleet({ARI.host: health(["base_branch"])})

    result = CliRunner().invoke(cli, ["dispatch", "fix it", "--base", "feature/login"])

    assert result.exit_code == 0, result.output
    assert "Base: feature/login" in result.output
    assert posts(fake_http)[0]["base_branch"] == "feature/login"


def test_cli_base_refusal_exits_1(fleet_key, fake_http):
    ClusterConfig(repo_url="r", workers=[ARI]).save()
    fake_http.handler = fleet({ARI.host: health([], version="0.1.0")})
    result = CliRunner().invoke(cli, ["dispatch", "fix it", "--base", "develop"])
    assert result.exit_code == 1
    assert "does not support it" in result.output


def test_refresh_records_reported_base(fleet_key, fake_http):
    run = RunState(run_id=RUN_ID, prompt="p", repo_url="r", workers=[WorkerRunState(
        worker_id="ari", host=ARI.host, port=ARI.port, agent_engine="aider", strategy="s",
        branch=f"exp/{RUN_ID}/ari-s", remote_run_dir=f"{RUN_ID}-1")])
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "running", "base_branch": "main"})
    assert refresh_run(run, {"ari": ARI}).workers[0].base_branch == "main"


def test_merge_goes_into_the_runs_base_branch(fleet_key, fake_http, origin_repo, tmp_path, git, monkeypatch):
    origin, local = origin_repo
    # A develop branch on origin, created after the local clone was made.
    work = tmp_path / "dev-work"
    git(tmp_path, "clone", str(origin), str(work))
    git(work, "checkout", "-b", "develop")
    (work / "dev.txt").write_text("develop only\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", "develop work")
    git(work, "push", "origin", "develop")
    # The worker's branch starts from develop.
    branch = f"exp/{RUN_ID}/ari-s"
    git(work, "checkout", "-b", branch)
    (work / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-m", "hive: s")
    git(work, "push", "origin", branch)

    RunState(run_id=RUN_ID, prompt="p", repo_url=str(origin), base_branch="develop", workers=[WorkerRunState(
        worker_id="ari", host=ARI.host, port=ARI.port, agent_engine="aider", strategy="s", branch=branch,
        remote_run_dir=f"{RUN_ID}-1", status="succeeded", pushed=True)]).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "succeeded", "branch": branch, "pushed": True})
    monkeypatch.chdir(local)

    result = CliRunner().invoke(cli, ["merge", "--worker", "ari", "--yes"])

    assert result.exit_code == 0, result.output
    assert "into local develop" in result.output
    assert git(local, "rev-parse", "--abbrev-ref", "HEAD") == "develop"
    assert (local / "feature.py").exists() and (local / "dev.txt").exists()
    assert "feature.py" not in git(local, "ls-tree", "--name-only", "main")  # main untouched


# ---------------------------------------------------------------- logs --full


def save_slot_run():
    RunState(run_id=RUN_ID, prompt="p", repo_url="r", workers=[WorkerRunState(
        worker_id="ari", host=ARI.host, port=ARI.port, agent_engine="aider", strategy="s",
        branch=f"exp/{RUN_ID}/ari-s", remote_run_dir=f"{RUN_ID}-1", status="failed")]).save()


def test_logs_full_fetches_requested_file(fleet_key, fake_http):
    save_slot_run()
    seen = {}

    def handler(request):
        if request.url.path.startswith("/logs/"):
            seen.update(path=request.url.path, params=dict(request.url.params))
            return httpx.Response(200, json={
                "run_id": f"{RUN_ID}-1", "file": "test", "size": 5000, "truncated": True,
                "content": "FAILED tasks/add/check.py [red]x[/red]\n"})
        return httpx.Response(200, json={"status": "failed", "log_tail": "short tail"})

    fake_http.handler = handler
    result = CliRunner().invoke(cli, ["logs", "--full", "--file", "test"])

    assert result.exit_code == 0, result.output
    assert seen["path"] == f"/logs/{RUN_ID}-1"
    assert seen["params"]["file"] == "test"
    assert "FAILED tasks/add/check.py [red]x[/red]" in result.output  # literal, not markup
    assert "of 5000 bytes" in result.output
    assert "short tail" not in result.output


def test_logs_full_on_old_worker_falls_back_to_tail(fleet_key, fake_http):
    save_slot_run()

    def handler(request):
        if request.url.path.startswith("/logs/"):
            return httpx.Response(404, json={"detail": "Not Found"})  # route unknown to 0.1/0.2 workers
        return httpx.Response(200, json={"status": "failed", "log_tail": "short tail"})

    fake_http.handler = handler
    result = CliRunner().invoke(cli, ["logs", "--full"])

    assert result.exit_code == 1
    assert "update hivemind-worker to 0.3.0" in result.output
    assert "short tail" in result.output


def test_logs_full_reports_missing_log(fleet_key, fake_http):
    save_slot_run()

    def handler(request):
        if request.url.path.startswith("/logs/"):
            return httpx.Response(404, json={"detail": f"Run '{RUN_ID}-1' has no bench log."})
        return httpx.Response(200, json={"status": "failed"})

    fake_http.handler = handler
    result = CliRunner().invoke(cli, ["logs", "--full", "--file", "bench"])
    assert result.exit_code == 1
    assert "has no bench log" in result.output


def test_logs_full_rejects_unknown_file(fleet_key, fake_http):
    save_slot_run()
    result = CliRunner().invoke(cli, ["logs", "--full", "--file", "../../worker"])
    assert result.exit_code == 2
    assert fake_http.requests == []
