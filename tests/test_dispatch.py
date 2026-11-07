import json

import httpx
import pytest

from hivemind.config.cluster import ClusterConfig, WorkerNode
from hivemind.runner.dispatch import (
    DispatchError,
    assign_strategies,
    dispatch_run,
    resolve_repo_url,
    select_workers,
    validate_branch_ref,
)
from hivemind.runner.remote import branch_name, slugify
from hivemind.runner.state import RunState

ARI = WorkerNode(id="ari", host="100.101.58.76")
HERMES = WorkerNode(id="Hermes", host="100.115.119.6", port=7500, agent_engine="aider")
CLUSTER = ClusterConfig(repo_url="git@github.com:me/repo.git", workers=[ARI, HERMES])


def test_branch_name_is_scoped_by_run_id():
    assert branch_name("Hermes", "Quick Sort/SIMD", run_id="20261107-120000-abcd1234") == (
        "exp/20261107-120000-abcd1234/hermes-quick-sort-simd"
    )


def test_branch_name_without_run_id():
    assert branch_name("ari", "strategy-1") == "exp/ari-strategy-1"


def test_slugify_never_returns_empty():
    assert slugify("!!!") == "strategy"


@pytest.mark.parametrize(
    "branch", ["exp/20261107-120000-abcd1234/ari-strategy-1", "exp/ari-strategy-1"]
)
def test_valid_exp_branches_are_accepted(branch):
    assert validate_branch_ref(branch) == branch


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "main",
        "exp/",
        "exp",
        "-exp/a",
        "exp/../main",
        "exp/a//b",
        "exp/a.lock",
        "exp/a.",
        "exp/a@{1}",
        "exp/a b",
    ],
)
def test_non_exp_or_malformed_branches_are_rejected(branch):
    with pytest.raises(DispatchError):
        validate_branch_ref(branch)


def test_select_workers_defaults_to_all():
    assert select_workers(CLUSTER, None) == [ARI, HERMES]


def test_select_workers_keeps_requested_order():
    assert select_workers(CLUSTER, ["Hermes", " ari "]) == [HERMES, ARI]


def test_select_workers_rejects_unknown_ids():
    with pytest.raises(DispatchError, match="ghost"):
        select_workers(CLUSTER, ["ari", "ghost"])


def test_select_workers_on_empty_cluster():
    with pytest.raises(DispatchError):
        select_workers(ClusterConfig(), None)


def test_assign_strategies_fills_unnamed_slots():
    assert assign_strategies([ARI, HERMES], ["simd", " "]) == ([ARI, HERMES], ["simd", "strategy-2"])


def test_extra_strategies_wrap_round_the_workers():
    workers, strategies = assign_strategies([ARI, HERMES], ["a", "b", "c"])
    assert workers == [ARI, HERMES, ARI]
    assert strategies == ["a", "b", "c"]


def test_repeated_worker_gets_distinct_default_strategies():
    assert assign_strategies([ARI, ARI], []) == ([ARI, ARI], ["strategy-1", "strategy-2"])


def test_same_strategy_twice_on_one_worker_is_rejected():
    # Both would push to the same exp/<run_id>/ari-simd branch.
    with pytest.raises(DispatchError, match="twice"):
        assign_strategies([ARI], ["simd", "SIMD"])


def test_same_strategy_on_different_workers_is_fine():
    assert assign_strategies([ARI, HERMES], ["simd", "simd"])[1] == ["simd", "simd"]


def test_resolve_repo_url_prefers_override():
    assert resolve_repo_url(CLUSTER, " https://x/y.git ") == "https://x/y.git"
    assert resolve_repo_url(CLUSTER, None) == CLUSTER.repo_url


@pytest.mark.parametrize(
    "url",
    [
        "--upload-pack=touch /tmp/x",
        "ext::sh -c touch% /tmp/x",
        "fd::17",
        "\\\\attacker\\share\\repo",
        "//attacker/share/repo",
        "file:////attacker/share",
        "https://x/y.git\nmore",
        "https://x/ y.git",
    ],
)
def test_resolve_repo_url_rejects_injection(url):
    with pytest.raises(DispatchError):
        resolve_repo_url(CLUSTER, url)


def test_resolve_repo_url_requires_a_url():
    with pytest.raises(DispatchError):
        resolve_repo_url(ClusterConfig(), None)


def test_dispatch_run_posts_to_every_worker_with_auth(fleet_key, fake_http):
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "starting"})

    run = dispatch_run(
        CLUSTER,
        "  fix the bug  ",
        repo_url=CLUSTER.repo_url,
        workers=[ARI, HERMES],
        strategies=["simd", "Actor Queue"],
        test_command="pytest -q",
        model="sonnet",
    )

    assert len(fake_http.requests) == 2
    by_host = {r.url.host: r for r in fake_http.requests}
    hermes_req = by_host["100.115.119.6"]
    assert hermes_req.url.port == 7500
    assert hermes_req.url.path == "/dispatch"
    assert hermes_req.headers["authorization"] == f"Bearer {fleet_key}"
    body = json.loads(hermes_req.content)
    assert body["run_id"] == f"{run.run_id}-2"
    assert body["prompt"] == "fix the bug"
    assert body["branch"] == f"exp/{run.run_id}/hermes-actor-queue"
    assert body["agent_engine"] == "aider"
    assert (body["strategy_index"], body["strategy_count"]) == (2, 2)
    assert body["test_command"] == "pytest -q"

    assert [w.worker_id for w in run.workers] == ["ari", "Hermes"]
    assert [w.remote_run_dir for w in run.workers] == [f"{run.run_id}-1", f"{run.run_id}-2"]
    assert all(w.status == "starting" for w in run.workers)
    assert RunState.load(run.run_id) == run


def test_one_worker_runs_several_strategies_under_separate_ids(fleet_key, fake_http):
    fake_http.handler = lambda request: httpx.Response(200, json={"status": "starting"})
    workers, strategies = assign_strategies([ARI], ["simd", "actor-queue"])

    run = dispatch_run(CLUSTER, "p", repo_url="r", workers=workers, strategies=strategies)

    bodies = sorted((json.loads(r.content) for r in fake_http.requests), key=lambda b: b["run_id"])
    assert [b["run_id"] for b in bodies] == [f"{run.run_id}-1", f"{run.run_id}-2"]
    assert [b["branch"] for b in bodies] == [
        f"exp/{run.run_id}/ari-simd",
        f"exp/{run.run_id}/ari-actor-queue",
    ]
    assert {r.url.host for r in fake_http.requests} == {ARI.host}


def test_dispatch_run_records_rejections_and_offline_workers(fleet_key, fake_http):
    def handler(request):
        if request.url.host == ARI.host:
            return httpx.Response(401)
        raise httpx.ConnectError("refused", request=request)

    fake_http.handler = handler
    run = dispatch_run(CLUSTER, "p", repo_url="r", workers=[ARI, HERMES], strategies=["a", "b"])

    ari, hermes = run.workers
    assert ari.status == "failed" and "Unauthorized" in ari.error
    assert hermes.status == "failed" and "Cannot connect" in hermes.error


def test_dispatch_run_rejects_empty_prompt(fake_http):
    with pytest.raises(DispatchError):
        dispatch_run(CLUSTER, "   ", repo_url="r", workers=[ARI], strategies=["a"])
    assert fake_http.requests == []
