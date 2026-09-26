"""Plans: a planner's reply becomes a draft plan with mapped dependencies, or fails loudly.

Real subprocess planner stand-ins; real task store. The planner asks QUESTION lines until its
prompt carries answers.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import daemon, logs, plans, tasks  # noqa: E402

PY = sys.executable

# argv: reply-file  mode(plain|ask)
PLANNER = r'''
import sys
sys.stdout.reconfigure(encoding="utf-8")
p = sys.stdin.read()
reply, mode = sys.argv[1], sys.argv[2]
if mode == "ask" and "here are the answers" not in p:
    print("● QUESTION: Which file should the greeting go in?")
    print("QUESTION: Should it be in English?")
else:
    print("Here is the plan — thought about it.")
    print(open(reply, encoding="utf-8").read())
'''

CHAIN = {"tasks": [
    {"key": "t1", "prompt": "make a", "lane": "mundane", "deps": [], "check": "python -c 1"},
    {"key": "t2", "prompt": "make b", "lane": "workhorse", "deps": ["t1"], "check": "python -c 2"},
    {"key": "t3", "prompt": "make c", "lane": "critical", "deps": ["t2", "t1"], "check": "python -c 3"},
]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    (tmp_path / "planner.py").write_text(PLANNER, encoding="utf-8")
    (tmp_path / "repo").mkdir()
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": {"b": {"cwd": str(tmp_path / "repo")}}}),
                                    encoding="utf-8")
    return tmp_path


def planner(env, reply, mode="plain"):
    f = env / "reply.json"
    f.write_text(reply if isinstance(reply, str) else json.dumps(reply), encoding="utf-8")
    return [PY, str(env / "planner.py"), str(f), mode]


def test_three_tasks_become_a_draft_with_mapped_deps(env):
    p = plans.create("b", "greet", "len", planner=planner(env, CHAIN))
    assert p["status"] == "draft" and p["error"] is None
    assert [t["key"] for t in p["tasks"]] == ["t1", "t2", "t3"]
    ids = {t["key"]: t["task_id"] for t in p["tasks"]}
    by = {t["key"]: t for t in p["tasks"]}
    assert by["t1"]["deps"] == [] and by["t2"]["deps"] == [ids["t1"]]
    assert by["t3"]["deps"] == [ids["t2"], ids["t1"]]
    t3 = tasks.get("b", ids["t3"])
    assert t3["state"] == tasks.DRAFTED and t3["plan_id"] == p["id"] and t3["lane"] == "critical"
    assert t3["check"] == "python -c 3" and t3["deps"] == by["t3"]["deps"]
    assert plans.get("b", p["id"])["status"] == "draft" and plans.all_("b")[0]["id"] == p["id"]
    assert "Here is the plan" in p["planner_output_tail"]


@pytest.mark.parametrize("bad", [
    {"tasks": [{"key": "a", "prompt": "x", "lane": "mundane", "deps": ["b"]},
               {"key": "b", "prompt": "y", "lane": "mundane", "deps": ["a"]}]},
    {"tasks": [{"key": "a", "prompt": "x", "lane": "mundane", "deps": ["nope"]}]},
    {"tasks": [{"key": "a", "prompt": "x", "lane": "royal", "deps": []}]},
    "no json at all",
])
def test_a_cycle_unknown_dep_or_junk_fails_and_creates_no_tasks(env, bad):
    p = plans.create("b", "greet", "len", planner=planner(env, bad))
    assert p["status"] == "failed" and p["error"] and p["tasks"] == []
    assert tasks.all_("b") == {}


def test_questions_then_answers_then_a_draft(env):
    p = plans.create("b", "greet", "len", planner=planner(env, CHAIN, mode="ask"))
    assert p["status"] == "questions" and p["tasks"] == []
    assert [q["question"] for q in p["questions"]] == [
        "Which file should the greeting go in?", "Should it be in English?"]
    with pytest.raises(plans.PlanError):
        plans.approve("b", p["id"], "len")                  # not a draft yet
    p = plans.answer("b", p["id"], ["hello.txt", "yes"], "len")
    assert p["status"] == "draft" and len(p["tasks"]) == 3
    assert [q["answer"] for q in p["questions"]] == ["hello.txt", "yes"]
    assert logs.read(event="plan_questions")["rows"]


def test_approve_holds_the_tasks_and_reject_needs_a_note(env):
    p = plans.create("b", "greet", "len", planner=planner(env, CHAIN))
    with pytest.raises(plans.PlanError):
        plans.approve("b", p["id"], " ")
    a = plans.approve("b", p["id"], "len")
    assert a["status"] == "approved" and a["approved_by"] == "len" and a["approved_at"]
    assert all(tasks.get("b", t["task_id"])["state"] == tasks.HELD for t in a["tasks"])
    with pytest.raises(plans.PlanError):
        plans.approve("b", p["id"], "len")                  # approved once
    q = plans.create("b", "greet", "len", planner=planner(env, CHAIN))
    with pytest.raises(plans.PlanError):
        plans.reject("b", q["id"], "len", "  ")
    r = plans.reject("b", q["id"], "len", "too big")
    assert r["status"] == "rejected" and r["rejected"] == {"by": "len", "note": "too big"}
    assert all(tasks.get("b", t["task_id"])["state"] == tasks.CANCELLED for t in r["tasks"])
    with pytest.raises(plans.PlanError):
        plans.get("b", "P-nope") or plans.approve("b", "P-nope", "len")
    assert logs.read(event="plan_approved")["rows"] and logs.read(event="plan_rejected")["rows"]


def test_start_stores_planning_without_calling_the_planner(env):
    p = plans.start("b", "greet", "len", planner=[PY, "-c", "raise SystemExit(9)"])
    assert p["status"] == "planning" and plans.get("b", p["id"])["status"] == "planning"
    assert p["branch"] == "plexar/plan/%s" % p["id"]
    assert plans.run_planner("b", p["id"])["status"] == "failed"


def test_a_task_title_is_kept_tidy_and_optional():
    rows = plans.parse_tasks({"tasks": [
        {"key": "t1", "title": "  Notes   survive a reload ", "prompt": "p1", "check": "c"},
        {"key": "t2", "prompt": "p2", "deps": ["t1"], "check": "c"}]})
    assert [r["title"] for r in rows] == ["Notes survive a reload", None]
