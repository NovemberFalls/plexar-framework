"""D28 — the local HTTP API is the product surface.

The pool's rules are tested in test_pool.py. These test that the TRANSPORT keeps them:
every refusal reaches the client as a refusal with a status code, and nothing the HTTP
layer does can move a task somewhere the pool would not.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))
from plexar_agents import pool  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    # These tests are about other mechanics; approval (D17) has its own tests.
    monkeypatch.setattr(pool, "DEFAULT_APPROVAL", "auto")
    import app as app_mod
    return TestClient(app_mod.app)


def submit(c, n=3, b="bk"):
    ids = []
    for i in range(n):
        r = c.post("/api/buckets/%s/tasks" % b, json={"prompt": "task %d, stated fully" % i})
        assert r.status_code == 202, r.text
        ids.append(r.json()["id"])
    return ids


REASON = "these three touch the same module and share one test file"


def test_submit_is_202_held_and_hashed(client):
    import hashlib
    r = client.post("/api/buckets/bk/tasks", json={"prompt": "write the thing"})
    assert r.status_code == 202
    body = r.json()
    assert body["state"] == "held"
    assert body["prompt_sha256"] == hashlib.sha256(b"write the thing").hexdigest()
    t = client.get("/api/buckets/bk/tasks/%s" % body["id"]).json()
    assert t["prompt"] == "write the thing"          # byte-identical, not summarised


def test_full_lifecycle_over_http(client):
    ids = submit(client)
    assert [b["bucket"] for b in client.get("/api/buckets").json()] == ["bk"]
    r = client.post("/api/buckets/bk/selections",
                    json={"task_ids": ids[:2], "reason": REASON, "agent": "a1"})
    assert r.status_code == 201, r.text
    tid = ids[0]
    assert client.post("/api/buckets/bk/tasks/%s/start" % tid, json={"run_id": "r1"}).status_code == 200
    assert client.post("/api/buckets/bk/tasks/%s/beat" % tid).status_code == 200
    r = client.post("/api/buckets/bk/tasks/%s/finish" % tid, json={"ok": True, "gate_exit": 0})
    assert r.json()["state"] == "done"
    held = client.get("/api/buckets/bk/tasks", params={"state": "held"}).json()
    assert [t["id"] for t in held] == [ids[2]]


def test_ok_without_zero_gate_is_failed(client):
    tid = submit(client, 1)[0]
    client.post("/api/buckets/bk/selections", json={"task_ids": [tid], "reason": REASON, "agent": "a"})
    client.post("/api/buckets/bk/tasks/%s/start" % tid, json={"run_id": "r"})
    r = client.post("/api/buckets/bk/tasks/%s/finish" % tid, json={"ok": True, "gate_exit": None})
    assert r.json()["state"] == "failed"
    assert client.post("/api/buckets/bk/tasks/%s/requeue" % tid).json()["state"] == "held"


def test_refusals_carry_status_and_rule(client):
    ids = submit(client, 3)
    r = client.post("/api/buckets/bk/selections", json={"task_ids": ids, "reason": "x", "agent": "a"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "no_reason"
    client.put("/api/buckets/bk/policy", json={"cap": 2})
    r = client.post("/api/buckets/bk/selections", json={"task_ids": ids, "reason": REASON, "agent": "a"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "over_cap"
    # held -> done is not a move the transport can make
    r = client.post("/api/buckets/bk/tasks/%s/finish" % ids[0], json={"ok": True, "gate_exit": 0})
    assert r.status_code == 409
    assert client.get("/api/buckets/bk/tasks/%s" % ids[0]).json()["state"] == "held"


def test_lease_is_never_double(client):
    tid = submit(client, 1)[0]
    client.post("/api/buckets/bk/selections", json={"task_ids": [tid], "reason": REASON, "agent": "a"})
    assert client.post("/api/buckets/bk/tasks/%s/start" % tid, json={"run_id": "r1"}).status_code == 200
    r = client.post("/api/buckets/bk/tasks/%s/start" % tid, json={"run_id": "r2"})
    assert r.status_code == 409
    assert client.get("/api/buckets/bk/tasks/%s" % tid).json()["run_id"] == "r1"


def test_concurrency_cap_is_409(client):
    ids = submit(client, 2)
    client.put("/api/buckets/bk/policy", json={"concurrency": 1})
    client.post("/api/buckets/bk/selections", json={"task_ids": ids, "reason": REASON, "agent": "a"})
    assert client.post("/api/buckets/bk/tasks/%s/start" % ids[0], json={"run_id": "r1"}).status_code == 200
    r = client.post("/api/buckets/bk/tasks/%s/start" % ids[1], json={"run_id": "r2"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "concurrency"


def test_unknown_and_bad_names(client):
    assert client.get("/api/buckets/bk/tasks/T-nope").status_code == 404
    assert client.post("/api/buckets/a%20b/tasks", json={"prompt": "p"}).status_code == 400
    assert client.post("/api/buckets/bk/tasks", json={"prompt": ""}).status_code == 422


def test_server_pages_still_serve(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 200                 # redirects to /app
    assert client.get("/api-docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/api/logs" in spec["paths"] and "/api/buckets/{b}/tasks" in spec["paths"]
    assert client.get("/board").status_code == 404            # estate views are not in the product


def test_approval_over_http(client, monkeypatch):
    monkeypatch.setattr(pool, "DEFAULT_APPROVAL", "ask")
    tid = submit(client, 1, b="ask")[0]
    sid = client.post("/api/buckets/ask/selections",
                      json={"task_ids": [tid], "reason": REASON, "agent": "a"}).json()["selection_id"]
    r = client.post("/api/buckets/ask/tasks/%s/start" % tid, json={"run_id": "r"})
    assert r.status_code == 428 and r.json()["detail"]["code"] == "approval_required"
    pend = client.get("/api/buckets/ask/selections", params={"approval": "pending"}).json()
    assert [p["id"] for p in pend] == [sid]
    assert client.post("/api/buckets/ask/selections/%s/approve" % sid, json={"by": ""}).status_code == 422
    assert client.post("/api/buckets/ask/selections/%s/approve" % sid, json={"by": "len"}).status_code == 200
    assert client.post("/api/buckets/ask/tasks/%s/start" % tid, json={"run_id": "r"}).status_code == 200


def test_client_page_and_daemon_status(client):
    r = client.get("/app")
    assert r.status_code == 200 and "/api" in r.text
    d = client.get("/api/daemon").json()
    assert d["buckets"] == {} and d["config"].endswith("daemon.json")


def test_cross_origin_writes_are_refused(client):
    r = client.post("/api/buckets/bk/tasks", json={"prompt": "p"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/buckets/bk/tasks").json() == []
    ok = client.post("/api/buckets/bk/tasks", json={"prompt": "p"},
                     headers={"origin": "http://127.0.0.1:8430"})
    assert ok.status_code == 202


def test_client_script_parses():
    # Twice now an escape in a generator turned "\n" into a raw newline inside a JS string
    # and broke the whole page silently. A syntax check is cheap; node is on the machine.
    import shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (ROOT / "docs" / "client.html").read_text(encoding="utf-8")
    js = html.split("<script>")[1].split("</script>")[0]
    r = subprocess.run([node, "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
                       input=js, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
