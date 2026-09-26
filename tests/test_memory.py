"""Per-task memory in the project: <repo>/.plexar/tasks/<id>.json.

Every run, attempt, check, reviewer verdict and send-back is appended; the agent is pointed at
the file and can add notes; the next run of the same task (after a crash, a retry or a
review) sees what happened. It never dirties the working tree, and it is cleared only on the
final human acceptance when that (default-off) setting is on.
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
from plexar_agents import daemon, memory, pool, tasks  # noqa: E402

PY = sys.executable
WHY = "one standalone change, nothing else touches it"
# The agent: saves the prompt it got, and adds a note to its memory file, like a real one would.
AGENT = [PY, "-c", (
    "import json,os,sys;"
    "p=sys.stdin.read();open('prompt_seen.txt','a',encoding='utf-8').write(p+chr(10)+'====='+chr(10));"
    "m=os.environ['PLEXAR_TASK_MEMORY'];d=json.load(open(m,encoding='utf-8'));"
    "d['notes'].append({'text':'tried the obvious fix'});json.dump(d,open(m,'w',encoding='utf-8'))")]
GATE_OK = [PY, "-c", "print('expected x, found x'); raise SystemExit(0)"]


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
        "cwd": str(repo), "runner": AGENT, "gate": GATE_OK}}}), encoding="utf-8")
    return repo


def run_once(tid):
    s = pool.select("b", [tid], WHY, "agent")
    pool.approve("b", s["selection_id"], "len")
    daemon.pass_once()


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, encoding="utf-8").stdout


def test_memory_lives_in_the_project_and_never_dirties_the_tree(env):
    t = tasks.hold("b", tasks.create("b", "make the thing blue")["id"])
    run_once(t["id"])
    p = env / ".plexar" / "tasks" / ("%s.json" % t["id"])
    assert p.exists()
    mem = json.loads(p.read_text(encoding="utf-8"))
    kinds = [e["kind"] for e in mem["events"]]
    assert kinds[:1] == ["run_start"] and "runner" in kinds and "gate" in kinds and kinds[-1] == "run_end"
    gate = next(e for e in mem["events"] if e["kind"] == "gate")
    assert gate["exit"] == 0 and gate["output"].startswith("expected x, found x")
    assert mem["notes"] == [{"text": "tried the obvious fix"}]          # the agent wrote to it
    assert git(env, "status", "--porcelain") == ""                      # ignored: tree stays clean
    assert ".plexar" not in git(env, "ls-tree", "-r", "--name-only", "plexar/" + t["id"])
    # a second task in the same repo is not refused as a dirty tree
    t2 = tasks.hold("b", tasks.create("b", "a second task")["id"])
    run_once(t2["id"])
    assert tasks.get("b", t2["id"])["state"] == tasks.DONE


def test_the_next_run_starts_from_what_is_known(env):
    from fastapi.testclient import TestClient
    import app as app_mod
    c = TestClient(app_mod.app)
    t = tasks.hold("b", tasks.create("b", "make the thing blue")["id"])
    run_once(t["id"])
    r = c.post("/api/buckets/b/tasks/%s/review" % t["id"], json={"verdict": "rework", "by": "len", "note": "RED please"})
    assert r.status_code == 200
    run_once(t["id"])
    seen = git(env, "show", "plexar/%s:prompt_seen.txt" % t["id"]).split("=====")[1]
    assert "PLEXAR_TASK_MEMORY" in seen
    assert "What happened on this task before" in seen
    assert "a person sent it back (rework): RED please" in seen
    assert "your own note: tried the obvious fix" in seen                # its own note came back
    d = c.get("/api/buckets/b/tasks/%s/run" % t["id"]).json()
    assert d["memory"]["events"] and d["memory_clear_on_accept"] is False


def test_accept_keeps_memory_unless_clearing_is_switched_on(env):
    from fastapi.testclient import TestClient
    import app as app_mod
    c = TestClient(app_mod.app)
    a = tasks.hold("b", tasks.create("b", "task a")["id"])
    run_once(a["id"])
    c.post("/api/buckets/b/tasks/%s/review" % a["id"], json={"verdict": "merged", "by": "len"})
    assert memory.path(str(env), a["id"]).exists()                        # default: kept
    memory.set_clear_on_accept(True)
    try:
        bb = tasks.hold("b", tasks.create("b", "task b")["id"])
        run_once(bb["id"])
        c.post("/api/buckets/b/tasks/%s/review" % bb["id"], json={"verdict": "merged", "by": "len"})
        assert not memory.path(str(env), bb["id"]).exists()               # switched on: cleared on accept
    finally:
        memory.set_clear_on_accept(False)
