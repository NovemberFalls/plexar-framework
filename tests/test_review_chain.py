"""D60 — the review chain, driven by real subprocesses standing in for three models.

The canonical example: the worker paints the thing blue, the verifier (which can execute)
passes it, the approver catches that the task said red, the verifier relays that to the
worker, the worker fixes it, and the chain approves. Every hop is a `review` row that
conforms to the pinned schema, and the human's QA file lands in the repo's qa/ folder.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import daemon, ledger, pool, tasks  # noqa: E402

PY = sys.executable

WORKER = r'''
import sys
prompt = sys.stdin.read()
colour = "red" if "colour is wrong" in prompt else "blue"
open("colour.txt", "w").write(colour + "\n")
print("DONE: painted it " + colour)
'''

VERIFIER = r'''
import json, os, re, sys, subprocess
prompt = sys.stdin.read()
seen = open("colour.txt").read().strip()
open("verifier_scratch.tmp", "w").write("scratch")       # a reviewer must not leave this
evdir = re.search(r"(qa/evidence/T-[0-9a-f]+)/", prompt).group(1)
os.makedirs(evdir, exist_ok=True)
open(evdir + "/swatch.png", "wb").write(b"PNGFAKE")        # a reviewer MAY leave this
print("I ran cat colour.txt")
print(json.dumps({"verdict": "pass", "judgement": "the file exists and has a colour",
    "evidence": [{"cmd": "cat colour.txt", "observed": seen}],
    "qa_cases": [{"id": "QA-1", "class": "AUTO", "section": "paint", "test": "colour file",
                  "how": "cat colour.txt", "expected": "red", "observed": seen},
                 {"id": "QA-2", "class": "VISUAL", "section": "ui", "test": "swatch looks red",
                  "how": "open index.html", "expected": "a red square", "observed": "a " + seen + " square",
                  "screenshot": evdir + "/swatch.png", "ai_verdict": "pass" if seen == "red" else "fail"},
                 {"id": "QA-3", "class": "MANUAL", "section": "ui", "test": "hover tooltip",
                  "how": "hover the swatch", "expected": "says red", "observed": "could not capture hover"}],
    "feedback": ""}))
'''

APPROVER = r'''
import json, sys
sys.stdin.read()
seen = open("colour.txt").read().strip()
if seen == "red":
    print(json.dumps({"verdict": "approve", "judgement": "red, as asked", "feedback": ""}))
else:
    print(json.dumps({"verdict": "reject", "judgement": "task said red",
                      "feedback": "the colour is wrong: the task says red"}))
'''

GARBAGE = r'''
import sys
sys.stdin.read()
print("looks fine to me!")
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (repo / "README").write_text("x\n")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, src in (("worker", WORKER), ("verifier", VERIFIER), ("approver", APPROVER),
                      ("garbage", GARBAGE)):
        (bin_ / (name + ".py")).write_text(src, encoding="utf-8")
    return tmp_path


def cfg(tmp, verifier="verifier", max_loops=3):
    b = tmp / "bin"
    return {"b": {"cwd": str(tmp / "repo"),
                  "runner": [PY, str(b / "worker.py"), "--model", "haiku"],
                  "gate": [PY, "-c", "raise SystemExit(0)"],
                  "review": {"verifier": [PY, str(b / (verifier + ".py")), "--model", "sonnet"],
                             "approver": [PY, str(b / "approver.py"), "--model", "opus"],
                             "max_loops": max_loops}}}


def queue():
    t = tasks.create("b", "paint the colour file red, please, exactly red")
    tasks.hold("b", t["id"])
    s = pool.select("b", [t["id"]], "one task, the colour example", "agent")
    pool.approve("b", s["selection_id"], "len")
    return t["id"]


def rows(tmp):
    out = []
    for f in (tmp / "logs").glob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def git(tmp, *a):
    return subprocess.run(["git", *a], cwd=tmp / "repo", capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def test_the_approver_catches_the_colour_and_the_loop_fixes_it(env):
    tid = queue()
    daemon.pass_once(cfg(env))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.DONE and t["review_final"] == "approved"
    assert t["review_attempts"] == 2
    rs = [r for r in rows(env) if r.get("run_id") == t["run_id"]]
    hops = [r for r in rs if r["event"] == "review"]
    assert [(h["stage"], h["verdict"]) for h in hops] == [
        ("verifier", "pass"), ("approver", "reject"), ("verifier", "pass"), ("approver", "approve")]
    assert hops[1]["feedback"].startswith("the colour is wrong")
    assert hops[0]["reviewer_model"] == "sonnet" and hops[0]["subject_model"] == "haiku"
    assert hops[1]["reviewer_model"] == "opus" and hops[1]["subject_model"] == "sonnet"
    for h in hops:
        assert ledger.validate(h) == [], ledger.validate(h)
    assert hops[0]["discarded_changes"] == ["verifier_scratch.tmp"]
    # the branch has the fixed work and the QA file; never the reviewer's scratch
    branch = "plexar/" + tid
    assert git(env, "show", branch + ":colour.txt").strip() == "red"
    qa = git(env, "show", "%s:qa/QA-%s.md" % (branch, tid))
    assert "| QA-2 | ui | swatch looks red | VISUAL | pass | PENDING | a red square |" in qa
    assert "![QA-2](evidence/%s/swatch.png)" % tid in qa
    assert "| QA-3 | ui | hover tooltip | MANUAL | — | PENDING |" in qa
    assert "| QA-1 | paint | colour file | AUTO | — | VERIFIED (chain) |" in qa
    assert qa.index("(VISUAL)") < qa.index("(MANUAL)") < qa.index("(AUTO")
    assert "not a replacement for yours" in qa                   # the human-validation notice
    assert git(env, "show", "%s:qa/evidence/%s/swatch.png" % (branch, tid)) == "PNGFAKE"
    assert "the colour is wrong" in qa
    assert "verifier_scratch.tmp" not in git(env, "ls-tree", "-r", "--name-only", branch)
    # main untouched
    assert "colour.txt" not in git(env, "ls-tree", "-r", "--name-only", "main")
    o = [r for r in rs if r["event"] == "outcome"][0]
    assert o["status"] == "DONE" and o["attempts"] == 2 and o["qa_file"] == "qa/QA-%s.md" % tid


def test_an_unparseable_verifier_is_never_an_approval(env):
    tid = queue()
    daemon.pass_once(cfg(env, verifier="garbage", max_loops=2))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED and t["review_final"] == "LADDER_EXHAUSTED"
    rs = [r for r in rows(env) if r.get("run_id") == t["run_id"]]
    assert [h["verdict"] for h in rs if h["event"] == "review"] == ["error", "error"]
    o = [r for r in rs if r["event"] == "outcome"][0]
    assert o["status"] == "LADDER_EXHAUSTED" and o["escalated_to"] == "human"
    chain_row = [r for r in rs if r["event"] == "review_chain"][0]
    assert chain_row["final"] == "LADDER_EXHAUSTED" and chain_row["approver_model"] == "opus"


def test_no_review_config_behaves_exactly_as_before(env):
    tid = queue()
    c = cfg(env)
    del c["b"]["review"]
    daemon.pass_once(c)
    t = tasks.get("b", tid)
    assert t["state"] == tasks.DONE and "review_final" not in t
    assert not [r for r in rows(env) if r["event"] == "review"]


def test_human_per_case_verdicts_are_logged_against_the_ai_and_screenshots_serve(env, monkeypatch):
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(ROOT / "docs"))
    import app as app_mod
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": cfg(env)}), encoding="utf-8")
    tid = queue()
    daemon.pass_once()
    c = TestClient(app_mod.app)
    d = c.get("/api/buckets/b/tasks/%s/run" % tid).json()
    assert {q["id"] for q in d["qa_cases"]} == {"QA-1", "QA-2", "QA-3"}
    # the human disagrees with the AI on the visual case: that disagreement is the label
    r = c.post("/api/buckets/b/tasks/%s/qa" % tid, json={"case_id": "QA-2", "verdict": "FAIL", "by": "len"})
    assert r.status_code == 200 and r.json()["agree"] is False and r.json()["ai_verdict"] == "pass"
    r = c.post("/api/buckets/b/tasks/%s/qa" % tid, json={"case_id": "QA-1", "verdict": "PASS", "by": "len"})
    assert r.json()["agree"] is True                          # AUTO: chain approved = implied pass
    lab = [x for x in rows(env) if x["event"] == "qa_result"]
    assert len(lab) == 2 and all(ledger.validate(x) == [] for x in lab)
    assert c.post("/api/buckets/b/tasks/%s/qa" % tid, json={"case_id": "QA-9", "verdict": "PASS", "by": "len"}).status_code == 404
    shot = next(q["screenshot"] for q in d["qa_cases"] if q["id"] == "QA-2")
    img = c.get("/api/buckets/b/tasks/%s/evidence" % tid, params={"path": shot})
    assert img.status_code == 200 and img.content == b"PNGFAKE"
    assert c.get("/api/buckets/b/tasks/%s/evidence" % tid, params={"path": "plexar_agents/api.py"}).status_code == 400
    assert c.get("/api/buckets/b/tasks/%s/evidence" % tid,
                 params={"path": "qa/evidence/%s/../../README" % tid}).status_code == 400


def test_every_evidence_file_is_listed(env):
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(ROOT / "docs"))
    import app as app_mod
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": cfg(env)}), encoding="utf-8")
    tid = queue()
    daemon.pass_once()
    files = TestClient(app_mod.app).get("/api/buckets/b/tasks/%s/evidence/list" % tid).json()
    assert files == ["qa/evidence/%s/swatch.png" % tid]
