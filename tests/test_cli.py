"""`plexar init` — a repo becomes a bucket only when it has a gate."""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import cli, daemon  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    r = tmp_path / "my repo"
    r.mkdir()
    monkeypatch.chdir(r)
    return r


def test_no_gate_is_refused(repo):
    assert cli.main(["init"]) == 2
    assert daemon.load_config() == {}


def test_pytest_repo_gets_a_pytest_gate_and_a_safe_bucket_name(repo):
    (repo / "tests").mkdir()
    assert cli.main(["init"]) == 0
    c = daemon.load_config()["my-repo"]
    assert c["gate"] == ["python", "-m", "pytest", "-q"] and c["cwd"] == str(repo)
    assert c["runner"][:2] == ["claude", "-p"]


def test_explicit_gate_wins(repo):
    assert cli.main(["init", "--bucket", "x", "--gate", "npm run check"]) == 0
    assert daemon.load_config()["x"]["gate"] == ["npm", "run", "check"]


def test_queue_adds_a_held_task_to_the_cwd_bucket_and_logs_it(repo, monkeypatch):
    from plexar_agents import tasks
    (repo / "tests").mkdir()
    cli.main(["init"])
    monkeypatch.setenv("PLEXAR_SESSION_ID", "sess-42")
    assert cli.main(["queue", "short"]) == 2
    assert cli.main(["queue", "Add a --verbose flag to the CLI that prints each step"]) == 0
    ts = list(tasks.all_("my-repo").values())
    assert len(ts) == 1 and ts[0]["state"] == "held" and ts[0]["session_id"] == "sess-42"


def test_queue_outside_a_bucket_is_refused(repo):
    assert cli.main(["queue", "Add a --verbose flag to the CLI that prints each step"]) == 2


def test_init_with_a_review_chain_needs_both_tiers(repo):
    (repo / "tests").mkdir()
    assert cli.main(["init", "--verifier", "claude -p --model sonnet"]) == 2
    assert cli.main(["init", "--verifier", "claude -p --model sonnet",
                     "--approver", "claude -p --model opus"]) == 0
    r = daemon.load_config()["my-repo"]["review"]
    assert r["verifier"][-1] == "sonnet" and r["approver"][-1] == "opus" and r["max_loops"] == 3
