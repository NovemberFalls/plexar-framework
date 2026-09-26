"""B3n — the daemon leases work off the pool, and the GATE decides the verdict.

Real subprocesses throughout: the runner and the gate are separate programs, as they
are in production, so exit codes are real exit codes.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import daemon, pool, tasks  # noqa: E402

PY = sys.executable
WHY = "one repo, one gate, grouped on purpose"
# The runner copies the prompt into the repo — proof it received the prompt, byte-exact.
COPY = [PY, "-c", "import shutil,sys; shutil.copy(sys.argv[1], 'got.md')", "{prompt_file}"]
OK = [PY, "-c", "raise SystemExit(0)"]
BAD = [PY, "-c", "raise SystemExit(3)"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def cfg(repo, runner=COPY, gate=OK):
    return {"b": {"cwd": str(repo), "runner": runner, "gate": gate}}


def queue(prompt="write the thing, exactly", approve=True):
    t = tasks.create("b", prompt)
    tasks.hold("b", t["id"])
    s = pool.select("b", [t["id"]], WHY, "agent")
    if approve:
        pool.approve("b", s["selection_id"], "len")
    return t["id"]


def test_submitted_while_down_is_run_when_it_starts(env):
    tid = queue("über-prompt ✓ exact bytes")         # no daemon exists yet
    rep = daemon.pass_once(cfg(env))
    assert rep["started"] == [tid]
    assert tasks.get("b", tid)["state"] == tasks.DONE
    got = (env / "got.md").read_bytes()
    assert got.startswith("über-prompt ✓ exact bytes".encode("utf-8"))   # the prompt, byte-exact
    assert b".plexar" in got and b"PLEXAR_TASK_MEMORY" in got           # then where its memory is


def test_the_gate_decides_not_the_runner(env):
    tid = queue()
    daemon.pass_once(cfg(env, gate=BAD))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED and t["gate_exit"] == 3


def test_a_failing_runner_fails_even_with_a_green_gate(env):
    tid = queue()
    daemon.pass_once(cfg(env, runner=BAD))
    assert tasks.get("b", tid)["state"] == tasks.FAILED


def test_unapproved_work_is_not_leased(env):
    tid = queue(approve=False)
    rep = daemon.pass_once(cfg(env))
    assert rep["skipped"][tid] == "awaiting approval"
    assert tasks.get("b", tid)["state"] == tasks.SELECTED


def test_a_bucket_without_a_gate_is_unrunnable(env):
    tid = queue()
    rep = daemon.pass_once({"b": {"cwd": str(env), "runner": COPY}})
    assert "gate" in rep["unrunnable"]["b"]
    assert tasks.get("b", tid)["state"] == tasks.SELECTED


def test_a_leased_task_is_not_leased_twice(env):
    tid = queue()
    pool.start("b", tid, "someone-else")
    rep = daemon.pass_once(cfg(env))
    assert rep["started"] == []
    assert tasks.get("b", tid)["run_id"] == "someone-else"


def test_the_prompt_arrives_on_stdin(env):
    tid = queue("via stdin ✓")
    cat = [PY, "-c", "import sys; open('got.md','wb').write(sys.stdin.buffer.read())"]
    daemon.pass_once(cfg(env, runner=cat))
    assert (env / "got.md").read_bytes().startswith("via stdin ✓".encode("utf-8"))
    assert tasks.get("b", tid)["state"] == tasks.DONE


def test_config_is_read_from_disk(env):
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": cfg(env)}), encoding="utf-8")
    tid = queue()
    assert daemon.pass_once()["started"] == [tid]


def test_one_task_per_working_tree_at_a_time(env):
    # Found by compose_m2e: two tasks sharing a cwd ran at once and one gate judged the
    # other's work. Each runner brackets its work with S/E in a shared journal; any
    # overlap shows up as "SS" and fails the test.
    ids = [queue("t%d" % i) for i in range(3)]
    pool.set_policy("b", concurrency=4)
    slow = [PY, "-c", "import time; f=open('journal','a'); f.write('S'); f.flush();"
                      "time.sleep(0.4); f.write('E'); f.close()"]
    rep = daemon.pass_once(cfg(env, runner=slow))
    assert sorted(rep["started"]) == sorted(ids)
    assert (env / "journal").read_text() == "SE" * 3
    assert {tasks.get("b", t)["state"] for t in ids} == {tasks.DONE}


def test_configure_writes_a_runnable_entry_and_refuses_bad_ones(env):
    e = daemon.configure("b", str(env), '["x", "{prompt_file}"]', "python -m pytest -q")
    assert e["gate"] == ["python", "-m", "pytest", "-q"]
    assert daemon.load_config()["b"]["cwd"] == str(env)
    with pytest.raises(ValueError, match="cwd"):
        daemon.configure("b", str(env / "nope"), "x {prompt_file}", "pytest")
    assert daemon.main(["show"]) == 0


# ------------------------------------------------- one branch per task, committed there

import subprocess  # noqa: E402


def gitrepo(repo):
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True,
                                     text=True).stdout.strip()


def test_each_task_lands_on_its_own_branch_and_main_is_untouched(env):
    git = gitrepo(env)
    main_before = git("rev-parse", "main")
    tid = queue("write got.md")
    daemon.pass_once(cfg(env))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.DONE and t["branch"] == "plexar/" + tid
    assert git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git("rev-parse", "main") == main_before
    assert git("status", "--porcelain") == ""
    assert "got.md" in git("show", "--name-only", "--format=", t["commit"])
    assert t["base_sha"] == main_before and t["runner_exit"] == 0


def test_a_failed_run_is_committed_too_as_evidence(env):
    git = gitrepo(env)
    tid = queue()
    daemon.pass_once(cfg(env, gate=BAD))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED and t["commit"]
    assert git("rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_a_dirty_tree_is_refused_not_stashed(env):
    gitrepo(env)
    (env / "someones_work.txt").write_text("unsaved")
    tid = queue()
    daemon.pass_once(cfg(env))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED and "not clean" in t["refused"]
    assert (env / "someones_work.txt").read_text() == "unsaved"
    assert not (env / "got.md").exists()


def test_run_detail_over_http_shows_log_branch_and_diff(env):
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(ROOT / "docs"))
    import app as app_mod
    gitrepo(env)
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": cfg(env)}), encoding="utf-8")
    tid = queue("diff me please")
    daemon.pass_once()
    d = TestClient(app_mod.app).get("/api/buckets/b/tasks/%s/run" % tid).json()
    assert d["branch"] == "plexar/" + tid and d["gate_exit"] == 0
    assert "$ " in d["log"]                          # the commands that ran are in the log
    assert "got.md" in d["diff"] and "+diff me please" in d["diff"]


# The agent CLI's own words when it has no credentials (Claude Code's are the first).
NOT_SIGNED_IN = [PY, "-c", "import sys; print('Invalid API key · Please run /login'); raise SystemExit(1)"]
PLAIN_FAIL = [PY, "-c", "import sys; print('TypeError: cannot add int and str'); raise SystemExit(1)"]
GATE_MARK = [PY, "-c", "open('gate_ran','w').write('1')"]


def test_a_runner_that_is_not_signed_in_says_so_plainly(env):
    from plexar_agents import logs
    tid = queue()
    daemon.pass_once(cfg(env, runner=NOT_SIGNED_IN, gate=GATE_MARK))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED
    assert "is not signed in" in t["error"] and daemon.RUNNER_ACCESS in t["error"]
    assert "requeue" in t["error"]
    assert not (env / "gate_ran").exists()                 # nothing to gate: the agent never worked
    assert logs.read(event="runner_not_signed_in")["rows"]


def test_an_ordinary_runner_failure_is_not_called_a_login_problem(env):
    tid = queue()
    daemon.pass_once(cfg(env, runner=PLAIN_FAIL))
    t = tasks.get("b", tid)
    assert t["state"] == tasks.FAILED and "signed in" not in (t.get("error") or "")
    assert daemon.not_signed_in(["claude"], 0, "not logged in") is None   # exit 0 is never a login failure


@pytest.mark.parametrize("said", [
    "Not signed in. Run: plexar-harness login",
    "Please sign in again: plexar-harness login",
    "Your Plexar access is paused.",
    "Your Plexar access has been turned off. Ask the Plexar owner.",
    "Your account doesn't have Plexar Harness access yet. Ask the Plexar owner to enable it.",
    "Plexar Harness access was revoked. Ask the Plexar owner.",
])
def test_the_harness_saying_it_has_no_access_reads_as_not_signed_in(said):
    msg = daemon.not_signed_in(["C:/x/plexar-harness.cmd", "-p"], 1, said)
    assert msg and msg.startswith("plexar-harness is not signed in")


def test_harness_offline_warnings_are_not_login_failures():
    for said in ("Can't reach Plexar; starting offline.", "Can't check Plexar access right now; starting anyway."):
        assert daemon.not_signed_in(["plexar-harness.cmd"], 1, said) is None


def test_plexar_log_writes_checked_rows(tmp_path, monkeypatch):
    import json as _json
    from plexar_agents import cli
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(_json.dumps(
        {"event": "run_start", "run_id": "r1", "skill": "s", "skill_version": "v", "cwd": "C:\Code\X", "task_summary": "t"})))
    assert cli.main(["log", "-"]) == 0
    assert cli.main(["log", '{"type": "run_start", "run_id": "r2"}']) == 2           # no "event": refused
    assert cli.main(["log", '{"event": "run_start", "cwd": "C:\Code"}']) == 2        # invalid JSON: refused
    rows = [_json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["cwd"] == "C:\Code\X" and rows[0]["ts"]


def test_a_running_task_says_which_step_it_is_in_and_clears_it_after(env):
    """The board shows the live step. The gate reads its own task mid-run: stage = gate."""
    tid = queue()
    root = str(pathlib.Path(__file__).resolve().parents[1]).replace("\\", "/")
    peek = [PY, "-c", "import json,sys; sys.path.insert(0, %r); from plexar_agents import tasks; "
            "open('stage.json', 'w').write(json.dumps(tasks.get('b', %r).get('stage')))" % (root, tid)]
    daemon.pass_once(cfg(env, gate=peek))
    seen = json.loads((env / "stage.json").read_text())
    assert seen["name"] == "gate" and seen["attempt"] == 1
    assert tasks.get("b", tid).get("stage") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command-line splitting")
def test_a_windows_check_splits_like_the_program_it_starts():
    """`\\"` inside a quoted argument is a literal quote to every Windows program."""
    assert daemon._argv(r'''node -e "x.includes('id=\"s\"')"''') == ["node", "-e", """x.includes('id="s"')"""]
    assert daemon._argv(r'C:\t\a.exe "C:\Program Files\x"') == [r"C:\t\a.exe", r"C:\Program Files\x"]
    assert daemon._argv('python -c "print(1)"') == ["python", "-c", "print(1)"]
