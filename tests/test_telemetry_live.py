"""Daemon runs are full §10 triples, Studio feeds work, and a human verdict is a label."""
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
WHY = "one repo, one gate, grouped on purpose"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return tmp_path


def rows(tmp):
    out = []
    for f in (tmp / "logs").glob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def run_one(tmp, gate_ok=True):
    t = tasks.create("b", "write three lines into got.txt please", session_id="S1")
    tasks.hold("b", t["id"])
    s = pool.select("b", [t["id"]], WHY, "agent")
    pool.approve("b", s["selection_id"], "len")
    cfg = {"b": {"cwd": str(tmp / "repo"),
                 "runner": [PY, "-c", "open('got.txt','w').write(chr(10).join('123') + chr(10))"],
                 "gate": [PY, "-c", "raise SystemExit(%d)" % (0 if gate_ok else 4)]}}
    daemon.pass_once(cfg)
    return tasks.get("b", t["id"])


def test_a_daemon_run_is_a_full_dispatch_outcome_run_triple(env):
    t = run_one(env)
    rs = [r for r in rows(env) if r.get("run_id") == t["run_id"]]
    ev = {r["event"]: r for r in rs}
    assert {"dispatch", "outcome", "run"} <= set(ev)
    d, o = ev["dispatch"], ev["outcome"]
    assert d["brief_sha256"] and pathlib.Path(d["brief_path"]).read_text(encoding="utf-8") == t["prompt"]
    assert d["approved_by"] == "len" and d["selection_reason"] == WHY and d["selection_size"] == 1
    assert d["session_id"] == "S1" and d["held_to_selected_s"] is not None
    assert o["status"] == "DONE" and o["node_check_exit"] == 0 and o["runner_exit"] == 0
    assert o["diff_files"] == 1 and o["diff_insertions"] == 3 and o["files_touched"] == ["got.txt"]
    assert o["wall_ms"] >= 0 and ev["run"]["gate_exit"] == 0


def test_a_failed_gate_is_GATE_FAIL_not_DONE(env):
    t = run_one(env, gate_ok=False)
    o = [r for r in rows(env) if r.get("run_id") == t["run_id"] and r["event"] == "outcome"][0]
    assert o["status"] == "GATE_FAIL" and o["node_check_exit"] == 4


def test_review_is_a_label_joined_to_the_run(env):
    from fastapi.testclient import TestClient
    import app as app_mod
    c = TestClient(app_mod.app)
    t = run_one(env)
    r = c.post("/api/buckets/b/tasks/%s/review" % t["id"], json={"verdict": "merged", "by": "len"})
    assert r.status_code == 200 and r.json()["review"]["verdict"] == "merged"
    lab = [x for x in rows(env) if x["event"] == "task_review"][0]
    assert lab["run_id"] == t["run_id"] and lab["verdict"] == "merged" and lab["gate_exit"] == 0
    assert c.post("/api/buckets/b/tasks/%s/review" % t["id"],
                  json={"verdict": "meh", "by": "len"}).status_code == 422
    held = tasks.hold("b", tasks.create("b", "a task that has not run yet at all")["id"])
    assert c.post("/api/buckets/b/tasks/%s/review" % held["id"],
                  json={"verdict": "merged", "by": "len"}).status_code == 409


def test_summary_and_events_feed_studio(env):
    from fastapi.testclient import TestClient
    import app as app_mod
    c = TestClient(app_mod.app)
    t0 = tasks.create("b", "a pending one for the badge count please")
    tasks.hold("b", t0["id"])
    pool.select("b", [t0["id"]], WHY, "agent")
    s = c.get("/api/summary").json()
    assert s["pending_approvals"] == 1 and s["buckets"]["b"]["pending_approvals"] == 1
    e1 = c.get("/api/events").json()
    assert [e["to"] for e in e1["events"]][-1] == "selected"
    e2 = c.get("/api/events", params={"since": e1["cursor"]}).json()
    assert e2["events"] == [] and e2["cursor"] == e1["cursor"]
