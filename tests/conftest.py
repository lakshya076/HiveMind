import subprocess
from pathlib import Path

import httpx
import pytest

FLEET_KEY = "hive-key-test0000000000000000000000000000000000"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point ~ at a temp dir so tests never touch the real ~/.hivemind."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home_dir))
    monkeypatch.delenv("HIVEMIND_FLEET_KEY", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.com")
    return home_dir


@pytest.fixture
def fleet_key(home):
    key_dir = home / ".hivemind"
    key_dir.mkdir(exist_ok=True)
    (key_dir / "fleet.key").write_text(FLEET_KEY, encoding="utf-8")
    return FLEET_KEY


class FakeWorkers:
    """Records every HTTP request the master makes and answers with ``handler``."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: httpx.Response(404)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def fake_http(monkeypatch):
    """Route every httpx.Client the master creates through an in-memory transport."""
    fake = FakeWorkers()
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    return fake


def run_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"
    return res.stdout.strip()


@pytest.fixture
def git():
    return run_git


@pytest.fixture
def origin_repo(tmp_path):
    """A bare origin with one commit on main, plus a local clone of it."""
    seed = tmp_path / "seed"
    seed.mkdir()
    run_git(seed, "init", "-b", "main")
    (seed / "app.py").write_text("print('v1')\n", encoding="utf-8")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-m", "initial")
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "clone", "--bare", str(seed), str(origin))
    local = tmp_path / "local"
    run_git(tmp_path, "clone", str(origin), str(local))
    return origin, local
