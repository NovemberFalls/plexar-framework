"""Evidence says what each picture shows, and a picture saved twice is not two pieces of proof."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))

from plexar_agents import daemon, tasks  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    repo = tmp_path / "repo"
    ev = repo / "qa" / "evidence" / "T-x"
    ev.mkdir(parents=True)
    (ev / "01_added.png").write_bytes(b"one")
    (ev / "02_after_reload.png").write_bytes(b"two")
    (ev / "03_copy.png").write_bytes(b"two")                  # the same image, saved again
    (ev / "captions.json").write_text(json.dumps({"02_after_reload.png": "the notes are still there after a reload"}))
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "evidence")
    daemon.config_path().parent.mkdir(parents=True, exist_ok=True)
    daemon.config_path().write_text(json.dumps({"buckets": {"b": {"cwd": str(repo), "runner": ["x"], "gate": ["x"]}}}))
    t = tasks.create("b", "p")
    tasks.annotate("b", t["id"], "commit", g("rev-parse", "HEAD"))
    import app as app_mod
    from fastapi.testclient import TestClient
    return TestClient(app_mod.app), t["id"]


def test_duplicates_are_marked_and_captions_are_read(client):
    c, tid = client
    repo = pathlib.Path(json.loads(daemon.config_path().read_text())["buckets"]["b"]["cwd"])
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    g("mv", "qa/evidence/T-x", "qa/evidence/%s" % tid)
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "rename")
    tasks.annotate("b", tid, "commit", g("rev-parse", "HEAD"))
    r = c.get("/api/buckets/b/tasks/%s/evidence/meta" % tid).json()
    by = {f["path"].split("/")[-1]: f for f in r["files"]}
    assert by["03_copy.png"]["same_as"].endswith("02_after_reload.png")
    assert by["01_added.png"]["same_as"] is None and by["02_after_reload.png"]["same_as"] is None
    assert r["captions"]["02_after_reload.png"] == "the notes are still there after a reload"


def test_the_board_keeps_a_link_per_task_and_plan():
    html = (ROOT / "docs" / "client.html").read_text(encoding="utf-8")
    assert 'searchParams.set(kind, id)' in html and 'remember("task", id)' in html and 'remember("plan", id)' in html
    assert 'qs.get("task")' in html and 'qs.get("plan")' in html
