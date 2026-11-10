import httpx
import pytest
from click.testing import CliRunner

from hivemind.cli import cli
from hivemind.config.cluster import ClusterConfig, WorkerNode, merge_workers
from hivemind.runner.state import RunState, WorkerRunState

RUN_ID = "20261107-120000-abcd1234"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.parametrize("args", [["check"], ["dispatch", "fix it"], ["status"], ["merge"]])
def test_commands_refuse_to_run_without_fleet_key(runner, fake_http, args):
    result = runner.invoke(cli, args)
    assert result.exit_code == 1
    assert "No fleet key" in result.output
    assert fake_http.requests == []


def test_env_fleet_key_is_accepted(runner, fake_http, monkeypatch):
    monkeypatch.setenv("HIVEMIND_FLEET_KEY", "hive-key-env")
    ClusterConfig(workers=[WorkerNode(id="ari", host="10.0.0.2")]).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "ready", "engines": []})
    result = runner.invoke(cli, ["check"])
    assert result.exit_code == 0, result.output
    assert fake_http.requests[0].headers["authorization"] == "Bearer hive-key-env"


def test_merge_workers_keeps_unselected_and_replaces_matches():
    old_a = WorkerNode(id="a", host="10.0.0.1")
    old_b = WorkerNode(id="b", host="10.0.0.2")
    new_b = WorkerNode(id="b-renamed", host="10.0.0.2", agent_engine="aider")
    new_c = WorkerNode(id="c", host="10.0.0.3")
    assert merge_workers([old_a, old_b], [new_b, new_c]) == [old_a, new_b, new_c]


def test_dispatch_command_saves_run(runner, fleet_key, fake_http):
    ClusterConfig(
        repo_url="git@github.com:me/repo.git",
        workers=[WorkerNode(id="ari", host="10.0.0.2")],
    ).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "starting"})

    result = runner.invoke(cli, ["dispatch", "fix it", "--strategy", "simd", "--test", "pytest -q"])

    assert result.exit_code == 0, result.output
    run = RunState.load_latest()
    assert run.workers[0].branch == f"exp/{run.run_id}/ari-simd"
    assert run.test_command == "pytest -q"


def save_run(repo_url: str, branch: str, status: str = "succeeded", pushed=True) -> RunState:
    run = RunState(
        run_id=RUN_ID,
        prompt="p",
        repo_url=repo_url,
        workers=[
            WorkerRunState(
                worker_id="ari", host="10.0.0.2", port=7422,
                agent_engine="claude-code", strategy="strategy-1", branch=branch,
                remote_run_dir=RUN_ID, status=status, pushed=pushed,
            )
        ],
    )
    run.save()
    return run


def push_worker_branch(tmp_path, origin, git, branch, message="hive: strategy-1"):
    """Simulate a worker: clone origin, commit on an exp/ branch, push it."""
    worker = tmp_path / "worker-clone"
    git(tmp_path, "clone", str(origin), str(worker))
    git(worker, "checkout", "-b", branch)
    (worker / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    git(worker, "add", "-A")
    git(worker, "commit", "-m", message)
    git(worker, "push", "origin", branch)


def worker_reports(branch):
    return lambda request: httpx.Response(
        200, json={"status": "succeeded", "branch": branch, "pushed": True, "commit": "x"}
    )


def test_merge_fetches_and_merges_winning_branch(
    runner, fleet_key, fake_http, origin_repo, tmp_path, git, monkeypatch
):
    origin, local = origin_repo
    branch = f"exp/{RUN_ID}/ari-strategy-1"
    push_worker_branch(tmp_path, origin, git, branch)
    save_run(str(origin), branch)
    fake_http.handler = worker_reports(branch)
    monkeypatch.chdir(local)

    result = runner.invoke(cli, ["merge"], input="ari\ny\n")

    assert result.exit_code == 0, result.output
    assert "Merged" in result.output
    assert (local / "feature.py").exists()
    assert git(local, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(local, "log", "-1", "--format=%s") == f"hive: merge {branch} from {RUN_ID}"


def test_merge_refuses_dirty_working_tree(
    runner, fleet_key, fake_http, origin_repo, tmp_path, git, monkeypatch
):
    origin, local = origin_repo
    branch = f"exp/{RUN_ID}/ari-strategy-1"
    push_worker_branch(tmp_path, origin, git, branch)
    save_run(str(origin), branch)
    fake_http.handler = worker_reports(branch)
    (local / "app.py").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(local)

    result = runner.invoke(cli, ["merge"], input="ari\n")

    assert result.exit_code == 1
    assert "uncommitted changes" in result.output
    assert not (local / "feature.py").exists()


def test_merge_refuses_repo_with_different_origin(
    runner, fleet_key, fake_http, origin_repo, monkeypatch
):
    _, local = origin_repo
    branch = f"exp/{RUN_ID}/ari-strategy-1"
    save_run("git@github.com:someone/else.git", branch)
    fake_http.handler = worker_reports(branch)
    monkeypatch.chdir(local)

    result = runner.invoke(cli, ["merge"], input="ari\n")

    assert result.exit_code == 1
    assert "origin does not match" in result.output


def test_merge_preview_shows_agent_commit_messages_literally(
    runner, fleet_key, fake_http, origin_repo, tmp_path, git, monkeypatch
):
    origin, local = origin_repo
    branch = f"exp/{RUN_ID}/ari-strategy-1"
    push_worker_branch(tmp_path, origin, git, branch, message="[link=https://evil.example]safe fix[/link]")
    save_run(str(origin), branch)
    fake_http.handler = worker_reports(branch)
    monkeypatch.chdir(local)

    result = runner.invoke(cli, ["merge"], input="ari\nn\n")

    assert "[link=https://evil.example]" in result.output


def test_dispatch_runs_several_strategies_on_one_worker(runner, fleet_key, fake_http):
    ClusterConfig(repo_url="r", workers=[WorkerNode(id="ari", host="10.0.0.2")]).save()
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "starting"})

    result = runner.invoke(cli, ["dispatch", "fix it", "--strategy", "simd", "--strategy", "actor"])

    assert result.exit_code == 0, result.output
    run = RunState.load_latest()
    assert [(w.worker_id, w.strategy) for w in run.workers] == [("ari", "simd"), ("ari", "actor")]
    assert len(fake_http.requests) == 2


def test_merge_picks_one_of_several_strategies_on_same_worker(
    runner, fleet_key, fake_http, origin_repo, tmp_path, git, monkeypatch
):
    origin, local = origin_repo
    simd = f"exp/{RUN_ID}/ari-simd"
    actor = f"exp/{RUN_ID}/ari-actor"
    push_worker_branch(tmp_path, origin, git, actor)
    run = RunState(
        run_id=RUN_ID, prompt="p", repo_url=str(origin),
        workers=[
            WorkerRunState(
                worker_id="ari", host="10.0.0.2", port=7422,
                agent_engine="aider", strategy=strategy, branch=branch,
                remote_run_dir=f"{RUN_ID}-{i}", status="succeeded", pushed=True,
            )
            for i, (strategy, branch) in enumerate((("simd", simd), ("actor", actor)), 1)
        ],
    )
    run.save()
    branches = {f"/status/{RUN_ID}-1": simd, f"/status/{RUN_ID}-2": actor}
    fake_http.handler = lambda request: httpx.Response(
        200, json={"status": "succeeded", "branch": branches[request.url.path], "pushed": True}
    )
    monkeypatch.chdir(local)

    result = runner.invoke(cli, ["merge"], input="ari:actor\ny\n")

    assert result.exit_code == 0, result.output
    assert git(local, "log", "-1", "--format=%s") == f"hive: merge {actor} from {RUN_ID}"

    # A bare worker id is ambiguous when that worker ran several strategies.
    result = runner.invoke(cli, ["merge"], input="ari\n")
    assert result.exit_code == 1
    assert "ran several strategies" in result.output


@pytest.mark.parametrize("status, pushed", [("no_changes", False), ("cancelled", None), ("succeeded", False)])
def test_merge_offers_nothing_for_unpushed_branches(runner, fleet_key, fake_http, status, pushed):
    branch = f"exp/{RUN_ID}/ari-strategy-1"
    save_run("r", branch, status=status, pushed=pushed)
    fake_http.handler = lambda request: httpx.Response(
        200, json={"status": status, "branch": branch, "pushed": pushed}
    )
    result = runner.invoke(cli, ["merge"])
    assert "No worker branches are available" in result.output
