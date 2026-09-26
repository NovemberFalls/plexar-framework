"""A person's "needs rework" / "rejected" closes the loop: the task goes back on the list
with the note, the next run's prompt carries it, and the branch keeps the earlier work.
A rejected selection keeps its reason on the tasks. The check's own output is kept apart
from the agent's so the board can show it.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))
from plexar_agents import daemon, pool, tasks  # noqa: E402

PY = sys.executable
WHY = "one standalone change, nothing else touches it"
# The runner appends the prompt it received to prompts.log (committed), so every attempt's
# prompt is visible on the branch.
RUNNER = [PY, "-c", "import sys; open('prompts.log','a',encoding='utf-8').write(sys.stdin.read()+chr(10)+'====='+chr(10))"]
GATE = [PY, "-c", "print('expected prompts.log, found it'); raise SystemExit(0)"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    (repo / "README").write_text("x\n")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": {"b": {
        "cwd": str(repo), "runner": RUNNER, "gate": GATE}}}), encoding="utf-8")
    return repo


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    import app as app_mod
    return TestClient(app_mod.app)


def run_once(tid):
    s = pool.select("b", [tid], WHY, "agent")
    pool.approve("b", s["selection_id"], "len")
    daemon.pass_once()


def git_show(repo, rev_path):
    return subprocess.run(["git", "show", rev_path], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8").stdout


def test_rework_needs_a_note_then_loops_with_it(env, client):
    t = tasks.hold("b", tasks.create("b", "make the thing blue")["id"])
    run_once(t["id"])
    assert tasks.get("b", t["id"])["state"] == tasks.DONE
    url = "/api/buckets/b/tasks/%s/review" % t["id"]
    assert client.post(url, json={"verdict": "rework", "by": "len"}).status_code == 422
    assert client.post(url, json={"verdict": "rework", "by": "len", "note": " "}).status_code == 422
    r = client.post(url, json={"verdict": "rework", "by": "len", "note": "it should be RED, not blue"})
    assert r.status_code == 200 and r.json()["state"] == tasks.HELD
    assert r.json()["feedback"][0]["round"] == 1
    run_once(t["id"])                                       # the loop: runs again, same branch
    done = tasks.get("b", t["id"])
    assert done["state"] == tasks.DONE and done["branch"] == "plexar/" + t["id"]
    log = git_show(env, "plexar/%s:prompts.log" % t["id"])
    first, second = log.split("=====")[0], log.split("=====")[1]
    assert "RED" not in first                               # the first run never saw a note
    assert "make the thing blue" in second and "Round 1 (rework, by len): it should be RED, not blue" in second
    d = client.get("/api/buckets/b/tasks/%s/run" % t["id"]).json()
    assert d["gate_output"].startswith("expected prompts.log, found it")
    assert len(d["feedback"]) == 1
    # a merged verdict records and leaves the task done
    assert client.post(url, json={"verdict": "merged", "by": "len"}).json()["state"] == tasks.DONE


def test_a_rejected_selection_keeps_its_reason(env, client):
    t = tasks.hold("b", tasks.create("b", "a task to group badly")["id"])
    s = pool.select("b", [t["id"]], WHY, "agent")
    r = client.post("/api/buckets/b/selections/%s/reject" % s["selection_id"],
                    json={"by": "len", "note": "wrong batch: this needs the schema change first"})
    assert r.status_code == 200
    got = tasks.get("b", t["id"])
    assert got["state"] == tasks.HELD
    assert got["selection_rejected"]["note"].startswith("wrong batch")
