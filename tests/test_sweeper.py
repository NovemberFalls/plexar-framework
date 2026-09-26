"""The sweeper runs an approved plan: dependency order, a plan branch, retries, a final say.

Everything real: a git repo in tmp_path, the daemon's own pass_once, and subprocess stand-ins
for the planner (also the final reviewer) and the worker. The worker obeys lines in its prompt:

    NEED <file>                 do nothing unless <file> exists (proves it runs on its deps)
    WRITE <file> <text>         write it
    ASK_BEFORE <n>              after writing, ask a QUESTION on every run before the n-th
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import daemon, logs, plans, pool, sweeper, tasks  # noqa: E402

PY = sys.executable

PLANNER = r'''
import sys
sys.stdout.reconfigure(encoding="utf-8")
p = sys.stdin.read()
reply, final = sys.argv[1], sys.argv[2]
if p.startswith("FINAL REVIEW"):
    print('{"verdict": "%s", "judgement": "looked at the whole diff — %s"}' % (final, final))
else:
    print(open(reply, encoding="utf-8").read())
'''

WORKER = r'''
import os, pathlib, re, sys
p = sys.stdin.read()
counts, lane = pathlib.Path(sys.argv[1]), sys.argv[2]
key = re.search(r"^KEY (\S+)$", p, re.M).group(1)
f = counts / key
n = int(f.read_text()) + 1 if f.exists() else 1
f.write_text(str(n))
with open(counts / ("%s.lanes" % key), "a") as fh:
    fh.write(lane + "\n")
for m in re.finditer(r"^NEED (\S+)$", p, re.M):
    if not os.path.exists(m.group(1)):
        print("missing", m.group(1))
        raise SystemExit(0)
for m in re.finditer(r"^WRITE (\S+) (\S+)$", p, re.M):
    open(m.group(1), "w").write(m.group(2))
for m in re.finditer(r"^ASK_BEFORE (\d+)$", p, re.M):
    if n < int(m.group(1)):
        print("QUESTION: shall I go on?")
'''

CHECK = r'''
import os, sys
sys.exit(0 if all(os.path.exists(f) for f in sys.argv[1:]) else 1)
'''


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    (tmp_path / "counts").mkdir()
    for name, src in (("planner.py", PLANNER), ("worker.py", WORKER), ("check.py", CHECK)):
        (tmp_path / name).write_text(src, encoding="utf-8")
    return tmp_path


def cfg(env, final="approve"):
    w = lambda lane: [PY, str(env / "worker.py"), str(env / "counts"), lane]
    c = {"cwd": str(env / "repo"), "runner": w("default"), "gate": [PY, "-c", "raise SystemExit(0)"],
         "planner": [PY, str(env / "planner.py"), str(env / "reply.json"), final],
         "lanes": {"critical": w("critical"), "workhorse": w("workhorse"), "mundane": w("mundane")}}
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": {"b": c}}), encoding="utf-8")
    return {"b": c}


def check(env, *files):
    return json.dumps([PY, str(env / "check.py"), *files])


def plan_of(env, specs, final="approve"):
    c = cfg(env, final)
    (env / "reply.json").write_text(json.dumps({"tasks": specs}), encoding="utf-8")
    p = plans.create("b", "build the chain", "len")
    assert p["status"] == "draft", p.get("error")
    plans.approve("b", p["id"], "len")
    return c, p


def chain_specs(env):
    return [
        {"key": "t1", "prompt": "KEY t1\nWRITE a.txt A", "lane": "mundane", "deps": [],
         "check": check(env, "a.txt")},
        {"key": "t2", "prompt": "KEY t2\nNEED a.txt\nWRITE b.txt B", "lane": "workhorse",
         "deps": ["t1"], "check": check(env, "a.txt", "b.txt")},
        {"key": "t3", "prompt": "KEY t3\nNEED b.txt\nWRITE c.txt C", "lane": "critical",
         "deps": ["t2"], "check": check(env, "c.txt")},
    ]


def test_an_approved_chain_runs_in_order_on_the_plan_branch_and_is_reviewed(env):
    repo = env / "repo"
    main_before = git(repo, "rev-parse", "main")
    c, p = plan_of(env, chain_specs(env))
    ids = {t["key"]: t["task_id"] for t in p["tasks"]}
    daemon.pass_once(c)
    p = plans.get("b", p["id"])
    assert p["status"] == "done", p
    assert p["final"]["verdict"] == "approve" and "whole diff" in p["final"]["judgement"]
    assert p["batches"] == [[ids["t1"]], [ids["t2"]], [ids["t3"]]]         # one at a time, in order
    for k in ids:
        t = tasks.get("b", ids[k])
        assert t["state"] == tasks.DONE and t["plan_merged"] is True
        assert t["check_before_exit"] == 1 and t["check_after_exit"] == 0
    # the lanes chose the runner
    assert (env / "counts" / "t1.lanes").read_text().split() == ["mundane"]
    assert (env / "counts" / "t3.lanes").read_text().split() == ["critical"]
    # the plan branch has all three files; the person's base branch is untouched
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(repo, "rev-parse", "main") == main_before and p["base"] == "main"
    assert set(git(repo, "ls-tree", "--name-only", p["branch"]).split()) >= {"a.txt", "b.txt", "c.txt"}
    assert not (repo / "a.txt").exists()
    assert git(repo, "status", "--porcelain") == ""
    ev = {r["event"] for r in logs.read(limit=5000)["rows"]}
    assert {"plan_created", "plan_approved", "plan_batch", "plan_merge", "plan_review", "plan_done"} <= ev


def test_the_final_review_can_reject(env):
    c, p = plan_of(env, chain_specs(env)[:1], final="reject")
    daemon.pass_once(c)
    p = plans.get("b", p["id"])
    assert p["status"] == "rejected" and p["final"]["verdict"] == "reject"
    assert git(env / "repo", "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_failing_task_is_retried_with_feedback_then_one_lane_up_then_blocked(env):
    specs = [{"key": "f", "prompt": "KEY f\nWRITE f.txt F", "lane": "mundane", "deps": [],
              "check": check(env, "never.txt")}]
    c, p = plan_of(env, specs)
    tid = p["tasks"][0]["task_id"]
    for _ in range(4):
        rep = sweeper.tick("b", c["b"])
        daemon.pass_once(c)
    p = plans.get("b", p["id"])
    assert p["status"] == "blocked" and "failed 3 times" in p["error"]
    assert p["tasks"][0]["attempts"] == 3 and p["tasks"][0]["lane"] == "workhorse"
    assert (env / "counts" / "f.lanes").read_text().split() == ["mundane", "mundane", "workhorse"]
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED
    assert len(t["feedback"]) == 2 and "never.txt" in t["feedback"][0]["note"]
    assert len(logs.read(event="plan_task_requeued")["rows"]) == 2
    assert logs.read(event="plan_blocked")["rows"]
    assert rep == {"started": [], "requeued": [], "blocked": [], "reviewed": []}


def test_a_merge_conflict_fails_the_task_and_blocks_the_plan(env):
    # x asks a question on its first run (its branch already holds f.txt=X) and waits; y lands
    # f.txt=Y; once answered, x reruns on its own old branch, passes, and cannot merge.
    specs = [{"key": "x", "prompt": "KEY x\nWRITE f.txt X\nASK_BEFORE 2", "lane": "mundane",
              "deps": [], "check": check(env, "f.txt")},
             {"key": "y", "prompt": "KEY y\nWRITE f.txt Y", "lane": "mundane", "deps": [],
              "check": check(env, "f.txt")}]
    repo = env / "repo"
    main_before = git(repo, "rev-parse", "main")
    c, p = plan_of(env, specs)
    ids = {t["key"]: t["task_id"] for t in p["tasks"]}
    daemon.pass_once(c)
    x = tasks.get("b", ids["x"])
    assert x["state"] == tasks.HELD and x["question_open"]              # waiting, not failed
    assert tasks.get("b", ids["y"])["state"] == tasks.DONE
    assert plans.get("b", p["id"])["status"] == "running"
    tasks.annotate("b", ids["x"], "question_open", None)               # a person answered
    daemon.pass_once(c)
    p = plans.get("b", p["id"])
    assert p["status"] == "blocked" and "merge conflict" in p["error"]
    assert tasks.get("b", ids["y"])["state"] == tasks.DONE
    x = tasks.get("b", ids["x"])
    assert x["state"] == tasks.FAILED and x["error"].startswith(sweeper.MERGE_CONFLICT)
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(repo, "rev-parse", "main") == main_before and git(repo, "status", "--porcelain") == ""
    assert git(repo, "show", "%s:f.txt" % p["branch"]) == "Y"
    # a blocked plan is left alone
    assert sweeper.tick("b", c["b"]) == {"started": [], "requeued": [], "blocked": [], "reviewed": []}


def test_a_task_waiting_on_a_question_is_not_a_failure(env):
    c, p = plan_of(env, chain_specs(env)[:1])
    tid = p["tasks"][0]["task_id"]
    tasks.annotate("b", tid, "question_open", {"question": "which?"})
    assert sweeper.tick("b", c["b"])["started"] == []
    assert plans.get("b", p["id"])["status"] == "approved"
    tasks.annotate("b", tid, "question_open", None)
    assert sweeper.tick("b", c["b"])["started"] == [tid]
    assert tasks.get("b", tid)["state"] == tasks.SELECTED
    sel = pool.selection("b", tasks.get("b", tid)["selection_id"])
    assert sel["approval"] == "approved" and sel["decided_by"] == "len (plan %s)" % p["id"]
