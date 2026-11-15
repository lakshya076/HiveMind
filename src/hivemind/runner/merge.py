"""Picking a winning strategy and merging its exp/* branch into a local clone.

Shared by ``hive merge`` and the MCP server. Errors are ``MergeError`` with plain
text messages; callers decide how to show them.
"""
from dataclasses import dataclass
from typing import Optional, Sequence

from hivemind.config.cluster import ClusterConfig
from hivemind.runner.dispatch import (
    DispatchError,
    resolve_repo_url,
    validate_base_branch,
    validate_branch_ref,
)
from hivemind.runner.state import RunState, WorkerRunState, validate_run_id
from hivemind.runner.status import (
    local_base_branch,
    local_git,
    local_repo_origin,
    local_working_tree_dirty,
    normalize_git_url,
)

MAX_DIFF_CHARS = 256 * 1024


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


def default_winner(
    candidates: list[WorkerRunState],
    *,
    rank_by: Optional[str] = None,
    higher_is_better: bool = False,
) -> WorkerRunState:
    pool = [w for w in candidates if w.status == "succeeded"] or candidates
    if rank_by:
        scored: list[tuple[float, WorkerRunState]] = []
        for w in pool:
            if isinstance(w.benchmark_data, dict):
                val = w.benchmark_data.get(rank_by)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    scored.append((float(val), w))
        if not scored:
            raise MergeError(f"No candidate has a numeric benchmark metric {rank_by!r}.")
        chooser = max if higher_is_better else min
        return chooser(scored, key=lambda item: item[0])[1]
    return pool[0]


@dataclass
class MergePlan:
    winner: WorkerRunState
    base: str
    compare_base: str
    has_local_base: bool
    commits: str
    diff_stat: str
    patch: str = ""


def prepare_merge(
    run: RunState,
    winner: WorkerRunState,
    cwd: Optional[str] = None,
    *,
    allow_dirty: bool = False,
    include_patch: bool = False,
) -> MergePlan:
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
    if not allow_dirty and local_working_tree_dirty(cwd):
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
        patch = ""
        if include_patch:
            patch_res = local_git("diff", f"{compare_base}...origin/{winner.branch}", check=False, cwd=cwd)
            patch = patch_res.stdout or ""
            if len(patch) > MAX_DIFF_CHARS:
                patch = patch[:MAX_DIFF_CHARS] + f"\n... [diff truncated at {MAX_DIFF_CHARS} chars]"
    except (RuntimeError, DispatchError) as exc:
        raise MergeError(str(exc)) from exc
    return MergePlan(winner, base, compare_base, has_local_base, log.stdout, diff.stdout, patch)


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


def list_remote_exp_branches(repo_url: str, run_id: Optional[str] = None) -> list[str]:
    """List ``exp/*`` branches (or ``exp/<run_id>/*``) on ``repo_url``."""
    try:
        clean_repo = resolve_repo_url(ClusterConfig(repo_url=repo_url), None)
        pattern = f"refs/heads/exp/{validate_run_id(run_id)}/*" if run_id else "refs/heads/exp/*"
        res = local_git("ls-remote", "--heads", "--", clean_repo, pattern)
    except (DispatchError, ValueError, RuntimeError) as exc:
        raise MergeError(str(exc)) from exc
    branches: list[str] = []
    for line in (res.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/heads/"):
            b = ref[len("refs/heads/"):]
            try:
                branches.append(validate_branch_ref(b))
            except DispatchError:
                continue
    return sorted(branches)


def delete_remote_exp_branches(repo_url: str, branches: Sequence[str]) -> list[str]:
    """Delete validated ``exp/*`` branches from ``repo_url``."""
    if not branches:
        return []
    try:
        clean_repo = resolve_repo_url(ClusterConfig(repo_url=repo_url), None)
        validated = [validate_branch_ref(b) for b in branches]
        local_git("push", "--", clean_repo, *(f":refs/heads/{b}" for b in validated))
    except (DispatchError, RuntimeError) as exc:
        raise MergeError(str(exc)) from exc
    return validated


def prune_local_runs(keep: int = 20, run_id: Optional[str] = None) -> list[str]:
    """Delete local saved run state files (either ``run_id`` or everything older than ``keep``)."""
    runs = RunState.load_all()
    if run_id is not None:
        clean_id = validate_run_id(run_id)
        targets = [r for r in runs if r.run_id == clean_id]
    else:
        targets = runs[max(0, int(keep)):]
    deleted: list[str] = []
    for r in targets:
        try:
            if r.path.exists():
                r.path.unlink()
                deleted.append(r.run_id)
        except OSError:
            continue
    return deleted

