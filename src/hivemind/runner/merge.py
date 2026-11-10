"""Picking a winning strategy and merging its exp/* branch into a local clone.

Shared by ``hive merge`` and the MCP server. Errors are ``MergeError`` with plain
text messages; callers decide how to show them.
"""
from dataclasses import dataclass
from typing import Optional

from hivemind.runner.dispatch import DispatchError, validate_base_branch, validate_branch_ref
from hivemind.runner.state import RunState, WorkerRunState
from hivemind.runner.status import (
    local_base_branch,
    local_git,
    local_repo_origin,
    local_working_tree_dirty,
    normalize_git_url,
)


class MergeError(Exception):
    """A merge that cannot start or did not finish."""


def worker_label(run: RunState, worker: WorkerRunState) -> str:
    """Worker id, or id:strategy when that worker ran several strategies in this run."""
    if sum(w.worker_id == worker.worker_id for w in run.workers) > 1:
        return f"{worker.worker_id}:{worker.strategy}"
    return worker.worker_id


def pick_worker(run: RunState, choice: str, candidates: list[WorkerRunState]) -> WorkerRunState:
    """Resolve 'id' or 'id:strategy' to exactly one of ``candidates``."""
    matches = [w for w in candidates if choice in (worker_label(run, w), f"{w.worker_id}:{w.strategy}")]
    if len(matches) == 1:
        return matches[0]
    if sum(w.worker_id == choice for w in run.workers) > 1:
        raise MergeError(f"Worker '{choice}' ran several strategies in this run; use '{choice}:<strategy>'.")
    raise MergeError(f"Unknown worker '{choice}'.")


def mergeable_workers(run: RunState) -> list[WorkerRunState]:
    """Strategies that finished with a branch on origin (failed ones included, cancelled not)."""
    return [
        w for w in run.workers
        if w.branch
        and w.pushed is not False
        and w.status not in ("no_changes", "cancelled", "starting", "running")
        and not (w.status == "failed" and w.pushed is not True and w.agent_exit_code is None)
    ]


def default_winner(candidates: list[WorkerRunState]) -> WorkerRunState:
    return next((w for w in candidates if w.status == "succeeded"), candidates[0])


@dataclass
class MergePlan:
    winner: WorkerRunState
    base: str
    compare_base: str
    has_local_base: bool
    commits: str
    diff_stat: str


def prepare_merge(run: RunState, winner: WorkerRunState, cwd: Optional[str] = None) -> MergePlan:
    """Check the local clone, fetch the winner (and the run's base) and preview the merge."""
    try:
        validate_branch_ref(winner.branch)
        run_base = validate_base_branch(run.base_branch)
    except DispatchError as exc:
        raise MergeError(str(exc)) from exc

    origin = local_repo_origin(cwd)
    if origin is None:
        raise MergeError(f"Run hive merge from a clone of the shared origin. Expected repo: {run.repo_url}")
    if normalize_git_url(origin) != normalize_git_url(run.repo_url):
        raise MergeError(f"This directory's origin does not match the run.\n  local: {origin}\n  run:   {run.repo_url}")
    if local_working_tree_dirty(cwd):
        raise MergeError("Local working tree has uncommitted changes. Commit or stash them before running hive merge.")

    try:
        local_git("fetch", "origin", f"+refs/heads/{winner.branch}:refs/remotes/origin/{winner.branch}", cwd=cwd)
        if run_base:
            # A run started from --base merges back into that branch, not the default one.
            local_git("fetch", "origin", f"+refs/heads/{run_base}:refs/remotes/origin/{run_base}", cwd=cwd)
        base = validate_base_branch(run_base or local_base_branch(cwd)) or "main"
        has_local_base = (
            local_git("show-ref", "--verify", "--quiet", f"refs/heads/{base}", check=False, cwd=cwd).returncode == 0
        )
        compare_base = base if has_local_base else f"origin/{base}"
        log = local_git("log", "--oneline", f"{compare_base}..origin/{winner.branch}", check=False, cwd=cwd)
        diff = local_git("diff", "--stat", f"{compare_base}...origin/{winner.branch}", check=False, cwd=cwd)
    except (RuntimeError, DispatchError) as exc:
        raise MergeError(str(exc)) from exc
    return MergePlan(winner, base, compare_base, has_local_base, log.stdout, diff.stdout)


def run_merge(run: RunState, plan: MergePlan, cwd: Optional[str] = None) -> None:
    """Check out the base branch and merge the winner with --no-ff. Never pushes."""
    branch = plan.winner.branch
    try:
        if plan.has_local_base:
            local_git("checkout", plan.base, cwd=cwd)
        else:
            local_git("checkout", "-B", plan.base, f"origin/{plan.base}", cwd=cwd)
        local_git("merge", "--no-ff", f"origin/{branch}", "-m", f"hive: merge {branch} from {run.run_id}", cwd=cwd)
    except RuntimeError as exc:
        raise MergeError(
            f"{exc}\nIf the merge stopped on conflicts, resolve them and commit, or run git merge --abort."
        ) from exc
