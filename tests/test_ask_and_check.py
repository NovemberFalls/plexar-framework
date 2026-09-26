"""Ask a layer up, and a task's own check that must prove itself.

1. A worker with a doubt prints `QUESTION:`. The verifier tier is asked, then the approver
   tier; if neither can answer, the task waits in Held for a person, whose answer every later
   run carries. ("Hello Wortld" was copied and passed because nothing could ask.)
2. A task's check must FAIL on the code before the work and PASS after it. A check that
   already passes beforehand proves nothing, and the run stops saying so.
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
from plexar_agents import daemon, logs, pool, tasks  # noqa: E402

PY = sys.executable
WHY = "one standalone change, nothing else touches it"

# The worker asks until its prompt carries an answer; then it writes the page.
WORKER = r'''
import sys
sys.stdout.reconfigure(encoding="utf-8")      # like the real harness, which prints UTF-8
p = sys.stdin.read()
if "A (" in p or "Answer, from" in p:
    open("index.html", "w").write("<body>Hello World</body>")
    print("done")
else:
    print("● QUESTION: The task says 'Hello Wortld'. Did you mean 'Hello World'?")
'''
# A reviewer stand-in: in question mode it answers or escalates (argv[1]); otherwise it passes.
REVIEWER = r'''
import json, sys
GOOD_CHECK = "python -c \"import os,sys; sys.exit(0 if os.path.exists('done.txt') else 1)\""
mode = sys.argv[1]
p = sys.stdin.read()
if "THE QUESTION" in p:
    if mode == "knows":
        print(json.dumps({"verdict": "answer", "answer": "Yes: 'Hello World'.", "because": "README says so"}))
    elif mode == "fixcheck":      # a tooling blocker: settle it by correcting the check
        print(json.dumps({"verdict": "answer", "answer": "The check was broken; use the corrected one.",
                          "because": "it cannot parse", "check": GOOD_CHECK}))
    elif mode == "weakcheck":     # a "fix" that passes before any work: must be refused
        print(json.dumps({"verdict": "answer", "answer": "Use this.", "because": "x",
                          "check": "python -c \"raise SystemExit(0)\""}))
    else:
        print(json.dumps({"verdict": "escalate", "why": "only the task author knows"}))
elif "You are the APPROVER" in p:
    print(json.dumps({"verdict": "approve", "judgement": "fine", "evidence": [], "feedback": ""}))
else:
    print(json.dumps({"verdict": "pass", "judgement": "fine", "evidence": [], "qa_cases": [], "feedback": ""}))
'''
MAKE_FILE = [PY, "-c", "open('done.txt','w').write('x')"]
OK = [PY, "-c", "raise SystemExit(0)"]


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
    (tmp_path / "worker.py").write_text(WORKER, encoding="utf-8")
    (tmp_path / "reviewer.py").write_text(REVIEWER, encoding="utf-8")
    return tmp_path


def configure(env, verifier_mode=None, approver_mode="escalate", runner=None, gate=OK):
    c = {"cwd": str(env / "repo"), "runner": runner or [PY, str(env / "worker.py")], "gate": gate}
    if verifier_mode:
        c["review"] = {"verifier": [PY, str(env / "reviewer.py"), verifier_mode],
                       "approver": [PY, str(env / "reviewer.py"), approver_mode], "max_loops": 3}
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": {"b": c}}), encoding="utf-8")


def run_once(tid):
    s = pool.select("b", [tid], WHY, "agent")
    pool.approve("b", s["selection_id"], "len")
    daemon.pass_once()


def new(prompt="Create a page that only says Hello Wortld", check=None):
    return tasks.hold("b", tasks.create("b", prompt, meta={"check": check} if check else None)["id"])["id"]


def test_a_question_nobody_above_can_answer_waits_for_a_person(env):
    from fastapi.testclient import TestClient
    import app as app_mod
    configure(env, verifier_mode="escalate", approver_mode="escalate")
    tid = new()
    run_once(tid)
    t = tasks.get("b", tid)
    assert t["state"] == tasks.HELD                                  # waiting, not failed-and-forgotten
    assert "Hello World" in t["question_open"]["question"]
    assert [x["tier"] for x in t["question_open"]["tried"]] == ["verifier", "approver"]   # it climbed
    c = TestClient(app_mod.app)
    assert [q["id"] for q in c.get("/api/buckets/b/questions").json()] == [tid]
    r = c.post("/api/buckets/b/tasks/%s/answer" % tid, json={"answer": "Yes, Hello World.", "by": "len"})
    assert r.status_code == 200 and r.json()["question_open"] is None
    assert c.post("/api/buckets/b/tasks/%s/answer" % tid, json={"answer": "x", "by": "len"}).status_code == 409
    run_once(tid)
    done = tasks.get("b", tid)
    assert done["state"] == tasks.DONE and done["review_final"] == "approved"
    rows = logs.read(event="question")["rows"]
    assert rows[0]["answered"] is False and rows[0]["asker"] == "worker"


def test_a_tier_above_that_knows_answers_and_the_run_continues(env):
    configure(env, verifier_mode="knows")
    tid = new()
    run_once(tid)
    t = tasks.get("b", tid)
    assert t["state"] == tasks.DONE and t["review_final"] == "approved"   # one run, no human needed
    assert t["answers"][0]["answer"] == "Yes: 'Hello World'." and t["answers"][0]["by"].startswith("verifier")
    assert logs.read(event="question")["rows"][0]["answered_by"] == "verifier"


def test_without_reviewers_a_question_goes_straight_to_a_person(env):
    configure(env)
    tid = new()
    run_once(tid)
    t = tasks.get("b", tid)
    assert t["state"] == tasks.HELD and t["question_open"]["tried"] == []


def test_a_check_that_already_passes_before_the_work_proves_nothing(env):
    configure(env, runner=MAKE_FILE)
    tid = new("make done.txt", check='%s -c "raise SystemExit(0)"' % PY)
    run_once(tid)
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED and t["check_before_exit"] == 0
    assert "already passes" in t["error"]
    assert not (env / "repo" / "done.txt").exists()                  # the agent never ran


def test_a_check_must_fail_before_and_pass_after(env):
    configure(env, runner=MAKE_FILE)
    chk = '%s -c "import os,sys; print(\'done.txt present\' if os.path.exists(\'done.txt\') else \'expected done.txt, found none\'); sys.exit(0 if os.path.exists(\'done.txt\') else 1)"' % PY
    tid = new("make done.txt", check=chk)
    run_once(tid)
    t = tasks.get("b", tid)
    assert t["check_before_exit"] == 1 and "expected done.txt, found none" in t["check_before_output"]
    assert t["check_after_exit"] == 0 and t["state"] == tasks.DONE
    # and a task whose check still fails after the work does not land, even with a green gate
    configure(env, runner=[PY, "-c", "print('did nothing')"])
    tid2 = new("make other.txt", check=chk.replace("done.txt", "other.txt"))
    run_once(tid2)
    t2 = tasks.get("b", tid2)
    assert t2["state"] == tasks.FAILED and t2["check_after_exit"] == 1 and t2["gate_exit"] == 1


# The worker is blocked by a broken check: it asks until an answer arrives, then does the work.
BLOCKED_WORKER = r'''
import sys
p = sys.stdin.read()
if "Answer, from" in p:
    open("done.txt", "w").write("x")
    print("done")
else:
    print("QUESTION: The task's check command cannot parse (SyntaxError before and after). Fix the check?")
'''
BROKEN_CHECK = 'python -c "import sys; sys.exit("'          # a SyntaxError: fails before AND after


def test_a_reviewer_fixes_a_broken_check_and_the_run_carries_on(env):
    (env / "bw.py").write_text(BLOCKED_WORKER, encoding="utf-8")
    configure(env, verifier_mode="fixcheck", runner=[PY, str(env / "bw.py")])
    tid = new("Create done.txt", check=BROKEN_CHECK)
    run_once(tid)
    t = tasks.get("b", tid)
    assert t["state"] == tasks.DONE and t.get("question_open") is None     # no human was asked
    assert "done.txt" in t["check"] and t["check_after_exit"] == 0
    assert t["check_fixes"][0]["old"] == BROKEN_CHECK and t["check_fixes"][0]["before_exit"] != 0
    assert logs.read(event="check_replaced")["rows"][0]["by"].startswith("verifier")


def test_a_fix_that_would_always_pass_is_refused_and_a_person_decides(env):
    (env / "bw.py").write_text(BLOCKED_WORKER, encoding="utf-8")
    configure(env, verifier_mode="weakcheck", approver_mode="weakcheck", runner=[PY, str(env / "bw.py")])
    tid = new("Create done.txt", check=BROKEN_CHECK)
    run_once(tid)
    t = tasks.get("b", tid)
    assert t["state"] == tasks.HELD and t["question_open"]
    assert t["check"] == BROKEN_CHECK                                        # not replaced
    assert "prove nothing" in t["question_open"]["tried"][-1]["why"]


def test_an_open_question_can_go_back_to_the_reviewers_who_settle_it(env):
    (env / "bw.py").write_text(BLOCKED_WORKER, encoding="utf-8")
    configure(env, verifier_mode="escalate", approver_mode="escalate", runner=[PY, str(env / "bw.py")])
    tid = new("Create done.txt", check=BROKEN_CHECK)
    run_once(tid)
    assert tasks.get("b", tid)["question_open"]                      # nobody could, the first time
    configure(env, verifier_mode="fixcheck", runner=[PY, str(env / "bw.py")])
    t = daemon.reask("b", tid)
    assert t["question_open"] is None and "done.txt" in t["check"]
    assert t["answers"][-1]["by"].startswith("verifier")
    run_once(tid)
    assert tasks.get("b", tid)["state"] == tasks.DONE
