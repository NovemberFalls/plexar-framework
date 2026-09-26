"""N3 — a thin HTTP + CLI transport over plans.py (final on disk; never modified here).

POST /plans returns 202 immediately; plans.run_planner runs in a daemon thread. These tests
poll GET until the plan leaves "planning", with a timeout, rather than sleeping blindly.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))
from plexar_agents import cli, daemon, plans, tasks  # noqa: E402

PY = sys.executable
BUCKET = "bk"

# A stand-in planner: reads the prompt on stdin, replies with two dependent tasks and a
# check that always exits 0 (the sweeper is W1's territory; these tests stop at the plan).
PLANNER_OK = r'''
import sys, json
sys.stdin.read()
print(json.dumps({"tasks": [
    {"key": "t1", "prompt": "write the first file, stated fully", "lane": "mundane",
     "deps": [], "check": "%s -c \"raise SystemExit(0)\""},
    {"key": "t2", "prompt": "write the second file, stated fully", "lane": "workhorse",
     "deps": ["t1"], "check": None}]}))
''' % PY.replace("\\", "\\\\")

PLANNER_QUESTION = r'''
import sys
sys.stdin.read()
print("QUESTION: which file should this touch?")
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    repo = tmp_path / "repo"
    repo.mkdir()
    return tmp_path


def configure(env, planner_src=PLANNER_OK):
    p = env / "planner.py"
    p.write_text(planner_src, encoding="utf-8")
    cfg = {"cwd": str(env / "repo"), "runner": [PY, "-c", "pass"], "gate": [PY, "-c", "pass"],
           "planner": [PY, str(p)]}
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": {BUCKET: cfg}}), encoding="utf-8")


@pytest.fixture
def client(env):
    import app as app_mod
    return TestClient(app_mod.app)


def _wait_draft_or_questions(client, pid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = client.get("/api/buckets/%s/plans/%s" % (BUCKET, pid)).json()
        if p["status"] != "planning":
            return p
        time.sleep(0.05)
    raise AssertionError("plan %s still 'planning' after %.1fs" % (pid, timeout))


def test_post_plans_is_202_and_runs_in_the_background(env, client):
    configure(env)
    r = client.post("/api/buckets/%s/plans" % BUCKET, json={"goal": "add a feature", "by": "len"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "planning"
    pid = body["id"]
    p = _wait_draft_or_questions(client, pid)
    assert p["status"] == "draft"
    assert [t["key"] for t in p["tasks"]] == ["t1", "t2"]
    # the merged live state comes from tasks.get, not the plan's own snapshot
    assert p["tasks"][0]["state"] == "drafted"
    assert p["tasks"][1]["deps"] == [p["tasks"][0]["task_id"]]


def test_list_plans(env, client):
    configure(env)
    r = client.post("/api/buckets/%s/plans" % BUCKET, json={"goal": "add a feature", "by": "len"})
    pid = r.json()["id"]
    _wait_draft_or_questions(client, pid)
    lst = client.get("/api/buckets/%s/plans" % BUCKET).json()
    assert [p["id"] for p in lst] == [pid]


def test_questions_reach_the_plan(env, client):
    configure(env, PLANNER_QUESTION)
    r = client.post("/api/buckets/%s/plans" % BUCKET, json={"goal": "vague goal", "by": "len"})
    pid = r.json()["id"]
    p = _wait_draft_or_questions(client, pid)
    assert p["status"] == "questions"
    assert "which file" in p["questions"][0]["question"]


def test_approve_then_reject_is_409_and_double_approve_is_409(env, client):
    configure(env)
    r = client.post("/api/buckets/%s/plans" % BUCKET, json={"goal": "add a feature", "by": "len"})
    pid = r.json()["id"]
    p = _wait_draft_or_questions(client, pid)
    assert p["status"] == "draft"
    r = client.post("/api/buckets/%s/plans/%s/approve" % (BUCKET, pid), json={"by": "len"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    t0 = tasks.get(BUCKET, p["tasks"][0]["task_id"])
    assert t0["state"] == tasks.HELD
    # a plan already approved cannot be rejected or approved again
    r = client.post("/api/buckets/%s/plans/%s/reject" % (BUCKET, pid), json={"by": "len", "note": "no"})
    assert r.status_code == 409
    r = client.post("/api/buckets/%s/plans/%s/approve" % (BUCKET, pid), json={"by": "len"})
    assert r.status_code == 409


def test_reject_with_blank_note_is_422(env, client):
    configure(env)
    r = client.post("/api/buckets/%s/plans" % BUCKET, json={"goal": "add a feature", "by": "len"})
    pid = r.json()["id"]
    _wait_draft_or_questions(client, pid)
    r = client.post("/api/buckets/%s/plans/%s/reject" % (BUCKET, pid), json={"by": "len", "note": "   "})
    assert r.status_code == 422, r.text
    r = client.post("/api/buckets/%s/plans/%s/reject" % (BUCKET, pid), json={"by": "len", "note": "not needed"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


def test_unknown_plan_is_404_and_bad_bucket_is_400(env, client):
    configure(env)
    assert client.get("/api/buckets/%s/plans/P-nope" % BUCKET).status_code == 404
    r = client.post("/api/buckets/%s/plans" % BUCKET, json={"goal": "x", "by": "len"})
    assert r.status_code == 202
    _wait_draft_or_questions(client, r.json()["id"])       # drain the background thread
    assert client.get("/api/buckets/bad bucket/plans").status_code == 400


# ----------------------------------------------------------------- CLI

def test_cli_plan_create_approve_reject(env, monkeypatch, capsys):
    configure(env)
    monkeypatch.chdir(env / "repo")
    rc = cli.main(["plan", "add a feature", "--bucket", BUCKET, "--by", "len"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "t1" in out and "t2" in out
    pid = plans.all_(BUCKET)[0]["id"]
    rc = cli.main(["plan", "approve", pid, "--by", "len", "--bucket", BUCKET])
    assert rc == 0
    assert plans.get(BUCKET, pid)["status"] == "approved"

    r2 = cli.main(["plan", "add another feature", "--bucket", BUCKET, "--by", "len"])
    assert r2 == 0
    pid2 = sorted((p["id"] for p in plans.all_(BUCKET) if p["id"] != pid))[0]
    rc = cli.main(["plan", "reject", pid2, "--by", "len", "--note", "not needed", "--bucket", BUCKET])
    assert rc == 0
    assert plans.get(BUCKET, pid2)["status"] == "rejected"

    rc = cli.main(["plan", "reject", pid2, "--bucket", BUCKET])
    capsys.readouterr()
    assert rc == 2                               # refused: no --by given


def test_cli_plans_list(env, monkeypatch, capsys):
    configure(env)
    monkeypatch.chdir(env / "repo")
    cli.main(["plan", "add a feature", "--bucket", BUCKET, "--by", "len"])
    capsys.readouterr()
    rc = cli.main(["plans", "--bucket", BUCKET])
    out = capsys.readouterr().out
    assert rc == 0
    assert "task(s)" in out
