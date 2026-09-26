"""Scope detection catches what the hook cannot — D32's measured hole.

The whole point of this module is the Bash case, so the Bash case is a test: the file
is created by a subprocess shell redirect, never by a write tool.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from plexar_agents import scope  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    r = tmp_path / "repo"
    (r / "lib").mkdir(parents=True)
    (r / "lib" / "session.py").write_text("x = 1\n", encoding="utf-8")
    (r / "lib" / "store.py").write_text("y = 1\n", encoding="utf-8")
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def rows(tmp_path):
    out = []
    for f in (tmp_path / "logs").glob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def test_a_bash_redirect_outside_owned_is_caught(repo):
    """THE defect. A PreToolUse matcher never sees this; git does."""
    owned = [str(repo / "lib" / "session.py")]
    before = scope.snapshot(str(repo))
    subprocess.run("echo bypass > stray.txt", shell=True, cwd=str(repo))
    got = scope.sweep(str(repo), owned, before)
    assert got["observable"] is True
    assert "stray.txt" in got["violations"]


def test_a_write_inside_owned_is_not_a_violation(repo):
    owned = [str(repo / "lib" / "session.py")]
    before = scope.snapshot(str(repo))
    (repo / "lib" / "session.py").write_text("x = 2\n", encoding="utf-8")
    got = scope.sweep(str(repo), owned, before)
    assert got["changed"] == ["lib/session.py"]
    assert got["violations"] == []


def test_an_owned_directory_covers_files_under_it(repo):
    before = scope.snapshot(str(repo))
    (repo / "lib" / "new.py").write_text("z = 1\n", encoding="utf-8")
    got = scope.sweep(str(repo), [str(repo / "lib")], before)
    assert got["violations"] == []


def test_a_pre_existing_change_is_not_blamed_on_this_run(repo):
    """Without a baseline every dirty file looks like a violation."""
    (repo / "lib" / "store.py").write_text("y = 99\n", encoding="utf-8")   # before the run
    before = scope.snapshot(str(repo))
    subprocess.run("echo new > fresh.txt", shell=True, cwd=str(repo))
    got = scope.sweep(str(repo), [str(repo / "lib" / "session.py")], before)
    assert got["violations"] == ["fresh.txt"], "the earlier edit predates the baseline"


def test_no_baseline_is_reported_rather_than_assumed_clean(repo):
    got = scope.sweep(str(repo), [], None)
    assert got["baselined"] is False
    assert "no baseline" in got["reason"]


def test_an_unobservable_tree_is_not_a_clean_sweep(tmp_path):
    """'could not observe' and 'nothing changed' are opposite claims."""
    got = scope.sweep(str(tmp_path / "not-a-repo"), [])
    assert got["observable"] is False
    assert got["violations"] == []
    assert got["reason"]


def test_a_clean_sweep_is_still_written_down(repo, tmp_path):
    before = scope.snapshot(str(repo))
    got = scope.sweep(str(repo), [str(repo / "lib")], before)
    scope.record("R1", "N01", str(repo), got)
    r = [x for x in rows(tmp_path) if x["event"] == "scope_sweep"][0]
    assert r["violation_count"] == 0
    assert r["detector"] == "git", "the row must say which surface observed it"


def test_the_row_names_the_violations(repo, tmp_path):
    before = scope.snapshot(str(repo))
    subprocess.run("echo x > stray.txt", shell=True, cwd=str(repo))
    got = scope.sweep(str(repo), [str(repo / "lib")], before)
    scope.record("R1", "N01", str(repo), got)
    r = [x for x in rows(tmp_path) if x["event"] == "scope_sweep"][0]
    assert r["violations"] == ["stray.txt"] and r["violation_count"] == 1


def test_revert_is_a_plan_and_never_executes(repo):
    subprocess.run("echo x > stray.txt", shell=True, cwd=str(repo))
    (repo / "lib" / "store.py").write_text("y = 42\n", encoding="utf-8")
    got = scope.propose_revert(str(repo), ["stray.txt", "lib/store.py"])
    assert (repo / "stray.txt").exists(), "proposing must not delete anything"
    assert (repo / "lib" / "store.py").read_text(encoding="utf-8") == "y = 42\n"
    by = {e["path"]: e for e in got["plan"]}
    assert by["lib/store.py"]["tracked"] is True
    assert by["stray.txt"]["tracked"] is False
    assert "unrecoverable" in by["stray.txt"]["rollback"]
    assert "stash" in got["safe_prelude"]


def test_untracked_and_tracked_carry_different_danger(repo):
    subprocess.run("echo x > stray.txt", shell=True, cwd=str(repo))
    got = scope.propose_revert(str(repo), ["stray.txt"])
    e = got["plan"][0]
    assert "only copy" in e["destroys"], "an untracked file may be someone's only copy"
