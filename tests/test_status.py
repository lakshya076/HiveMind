import httpx
import pytest

from hivemind.config.cluster import WorkerNode
from hivemind.runner.remote import read_worker_result
from hivemind.runner.state import RunState, WorkerRunState
from hivemind.runner import status as status_mod
from hivemind.runner.status import normalize_git_url, refresh_run

RUN_ID = "20261107-120000-abcd1234"
BRANCH = f"exp/{RUN_ID}/ari-strategy-1"
ARI = WorkerNode(id="ari", host="100.101.58.76")


def make_run() -> RunState:
    return RunState(
        run_id=RUN_ID,
        prompt="p",
        repo_url="r",
        workers=[
            WorkerRunState(
                worker_id="ari",
                host=ARI.host,
                port=ARI.port,
                agent_engine="claude-code",
                strategy="strategy-1",
                branch=BRANCH,
                remote_run_dir=RUN_ID,
            )
        ],
    )


def test_refresh_run_copies_worker_result(fleet_key, fake_http):
    fake_http.handler = lambda request: httpx.Response(200, json={
        "run_id": RUN_ID,
        "status": "succeeded",
        "branch": BRANCH,
        "pushed": True,
        "commit": "abc123",
        "agent_exit_code": 0,
        "test_exit_code": 0,
        "bench_exit_code": 0,
        "benchmark_data": {"ops": 42},
        "log_tail": "done",
    })

    run = refresh_run(make_run(), {"ari": ARI})

    (req,) = fake_http.requests
    assert req.url.path == f"/status/{RUN_ID}"
    assert req.headers["authorization"] == f"Bearer {fleet_key}"
    worker = run.workers[0]
    assert (worker.status, worker.pushed, worker.commit) == ("succeeded", True, "abc123")
    assert worker.benchmark_data == {"ops": 42}
    assert worker.log_tail == "done"
    assert RunState.load(RUN_ID).workers[0].status == "succeeded"


@pytest.mark.parametrize("rogue_branch", ["main", "exp/../main", "--force"])
def test_refresh_run_ignores_non_exp_branch_from_worker(fleet_key, fake_http, rogue_branch):
    fake_http.handler = lambda request: httpx.Response(
        200, json={"status": "succeeded", "branch": rogue_branch}
    )
    run = refresh_run(make_run(), {"ari": ARI})
    assert run.workers[0].branch == BRANCH


def test_refresh_run_uses_saved_host_when_worker_left_cluster(fleet_key, fake_http):
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "running"})
    run = refresh_run(make_run(), {})
    assert fake_http.requests[0].url.host == ARI.host
    assert run.workers[0].status == "running"


def test_terminal_status_survives_offline_worker(fleet_key, fake_http):
    run = make_run()
    run.workers[0].status = "succeeded"

    def offline(request):
        raise httpx.ConnectError("refused", request=request)

    fake_http.handler = offline
    run = refresh_run(run, {"ari": ARI})
    assert run.workers[0].status == "succeeded"


@pytest.mark.parametrize(
    "code, expected_status, expected_tail",
    [(404, "running", ""), (401, "unknown", "Authentication failed (invalid fleet key)")],
)
def test_read_worker_result_http_errors(fleet_key, fake_http, code, expected_status, expected_tail):
    fake_http.handler = lambda request: httpx.Response(code)
    status, payload, tail = read_worker_result(ARI, RUN_ID)
    assert (status, payload, tail) == (expected_status, None, expected_tail)


def test_read_worker_result_rejects_traversal_without_request(fake_http):
    status, payload, tail = read_worker_result(ARI, "../../health")
    assert status == "unknown" and payload is None and "Invalid run_id" in tail
    assert fake_http.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Me/Repo.git",
        "https://github.com/me/repo",
        "ssh://git@github.com/me/repo.git/",
        "http://github.com/me/repo.git",
    ],
)
def test_normalize_git_url_treats_ssh_and_https_alike(url):
    assert normalize_git_url(url) == "github.com/me/repo"


def test_local_git_helpers(origin_repo, monkeypatch, git):
    origin, local = origin_repo
    monkeypatch.chdir(local)

    assert status_mod.local_repo_origin() == str(origin)
    assert status_mod.local_base_branch() == "main"
    assert status_mod.local_working_tree_dirty() is False

    (local / "app.py").write_text("changed\n", encoding="utf-8")
    assert status_mod.local_working_tree_dirty() is True

    # Untracked files alone do not block a merge.
    git(local, "checkout", "--", "app.py")
    (local / "scratch.txt").write_text("x", encoding="utf-8")
    assert status_mod.local_working_tree_dirty() is False


def test_local_base_branch_detects_non_main_default(tmp_path, monkeypatch, git):
    repo = tmp_path / "trunk-repo"
    repo.mkdir()
    git(repo, "init", "-b", "trunk")
    (repo / "f").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    monkeypatch.chdir(repo)
    assert status_mod.local_base_branch() == "trunk"
