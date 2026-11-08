import pytest
from pydantic import ValidationError

from hivemind.runner.state import RunState, get_runs_dir, new_run_id, validate_run_id


def test_new_run_id_is_a_valid_run_id():
    run_id = new_run_id()
    assert validate_run_id(run_id) == run_id


@pytest.mark.parametrize(
    "run_id",
    ["", "..", "../evil", "a/b", "a\\b", "-x", "C:evil", "a..b", "x" * 129],
)
def test_traversal_and_malformed_run_ids_are_rejected(run_id):
    with pytest.raises(ValueError):
        validate_run_id(run_id)


def test_save_writes_inside_runs_dir_and_updates_latest(home):
    run = RunState(run_id="20261107-120000-abcd1234", prompt="p", repo_url="r")
    path = run.save()
    assert path.parent == get_runs_dir().resolve()
    assert path.parent.is_relative_to((home / ".hivemind").resolve())
    assert RunState.load_latest() == run
    assert RunState.load(run.run_id) == run


def test_run_state_refuses_traversal_run_id():
    with pytest.raises(ValidationError):
        RunState(run_id="../evil", prompt="p", repo_url="r")
    with pytest.raises(ValueError):
        RunState.load("../../etc/passwd")


def test_load_latest_without_runs_raises():
    with pytest.raises(FileNotFoundError):
        RunState.load_latest()


def test_load_unknown_run_raises():
    with pytest.raises(FileNotFoundError):
        RunState.load("20990101-000000-deadbeef")


def test_save_and_load_refuse_symlinks(home, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    runs_dir = get_runs_dir()
    link = runs_dir / "20261107-120000-symlink1.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks require extra privileges on this machine")

    with pytest.raises(ValueError, match="symlink"):
        RunState.load("20261107-120000-symlink1")
    with pytest.raises(ValueError, match="symlink"):
        RunState(run_id="20261107-120000-symlink1", prompt="p", repo_url="r").save()

