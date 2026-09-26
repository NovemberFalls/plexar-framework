"""The user's logs: findable, readable, dumpable, and open to rows from other tools.

Requirement: never forgo logging, keep the logs somewhere the user can access and dump,
and give people skills / SDK / API to port logs in, spawn agents and dump out. These tests pin each door: paths, the logs module, the HTTP routes,
the SDK and the CLI.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs"))
from plexar_agents import cli, ledger, logs, paths, tasks  # noqa: E402
from plexar_agents.client import Client, PlexarError  # noqa: E402

TS = "2026-09-23T18:00:00-04:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    return tmp_path


@pytest.fixture
def api(env):
    from fastapi.testclient import TestClient
    import app as app_mod
    return TestClient(app_mod.app)


def sdk(api) -> Client:
    def transport(method, path, body):
        r = api.request(method, path, json=body)
        return r.status_code, r.content
    return Client(url="http://test", transport=transport)


def seed(n=5):
    for i in range(n):
        ledger.write("review" if i % 2 else "dispatch", "run-%d" % (i % 2), "T-%d" % i, n=i)


# ------------------------------------------------------------------ where they live

def test_default_home_is_per_user_never_a_developer_path(tmp_path, monkeypatch):
    for k in ("PLEXAR_AGENTS_HOME", "PLEXAR_AGENTS_STATE", "PLEXAR_AGENTS_LOG_DIR", "PLEXAR_AGENTS_ARTIFACTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    h = paths.home()
    assert str(h).startswith(str(tmp_path)) and h.name == "plexar-agents"
    assert paths.log_dir() == h / "logs" and paths.artifacts_dir() == h / "artifacts"
    assert tasks.dir_() == h / "tasks"


def test_settings_move_the_logs_and_env_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_HOME", str(tmp_path / "h"))
    monkeypatch.delenv("PLEXAR_AGENTS_LOG_DIR", raising=False)
    paths.set_setting("log_dir", str(tmp_path / "team-logs"))
    assert paths.log_dir() == (tmp_path / "team-logs").resolve()
    ledger.write("x", "r")
    assert list((tmp_path / "team-logs").glob("dispatch-*.jsonl"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "env-logs"))
    assert paths.log_dir() == tmp_path / "env-logs"
    paths.set_setting("log_dir", None)
    monkeypatch.delenv("PLEXAR_AGENTS_LOG_DIR")
    assert paths.log_dir() == tmp_path / "h" / "logs"


# ------------------------------------------------------------------ reading

def test_read_pages_filters_and_never_hides_a_corrupt_line(env):
    seed(5)
    f = next((env / "logs").glob("dispatch-*.jsonl"))
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    ledger.write("review", "run-9", "T-9")
    p1 = logs.read(limit=2)
    assert len(p1["rows"]) == 2 and p1["more"]
    p2 = logs.read(p1["cursor"], limit=100)
    seen = [r["node_id"] for r in p1["rows"] + p2["rows"]]
    assert seen == ["T-0", "T-1", "T-2", "T-3", "T-4", "T-9"]      # every row once, in order
    assert p2["corrupt"] == 1 and not p2["more"]
    assert {r["event"] for r in logs.read(event="review")["rows"]} == {"review"}
    assert [r["node_id"] for r in logs.read(run_id="run-0")["rows"]] == ["T-0", "T-2", "T-4"]
    assert [r["node_id"] for r in logs.read(task="T-3")["rows"]] == ["T-3"]
    with pytest.raises(ValueError):
        logs.read("nonsense:3")


# ------------------------------------------------------------------ ingesting

def test_ingest_validates_keeps_provenance_and_logs_itself(env):
    rows = [{"ts": TS, "event": "ci_run", "run_id": "ci-1", "status": "green"},
            {"event": "ci_run", "run_id": "ci-2", "ts": "2026-09-23T18:00:00"},         # no offset
            {"ts": TS, "event": "review", "run_id": "r", "node_id": "n"},              # schema keys missing
            "not an object",
            {"ts": TS, "run_id": "ci-3"}]                                               # no event
    r = logs.ingest(rows, "github-actions")
    assert r["accepted"] == 1 and [x["index"] for x in r["rejected"]] == [1, 2, 3, 4]
    assert any("UTC offset" in p for p in r["rejected"][0]["problems"])
    assert any("stage" in p for p in r["rejected"][1]["problems"])
    ing = list((env / "logs").glob("ingested-*.jsonl"))
    assert len(ing) == 1                                   # never mixed into the framework's files
    row = json.loads(ing[0].read_text(encoding="utf-8"))
    assert row["source"] == "github-actions" and row["ts"] == TS and row["ingested_at"]
    audit = logs.read(event="logs_ingested")["rows"]
    assert audit[0]["accepted"] == 1 and audit[0]["rejected"] == 4
    with pytest.raises(ValueError):
        logs.ingest(rows, " ")


# ------------------------------------------------------------------ HTTP

def test_http_routes(env, api):
    seed(3)
    w = api.get("/api/logs/where").json()
    assert w["log_dir"] == str(env / "logs") and w["files"]
    r = api.get("/api/logs", params={"limit": 2}).json()
    assert len(r["rows"]) == 2 and r["more"]
    assert api.get("/api/logs", params={"cursor": "junk:1"}).status_code == 400
    assert api.get("/api/logs", params={"date_from": "yesterday"}).status_code == 422
    ex = api.get("/api/logs/export", params={"run_id": 'x"\r\nSet-Cookie: a=b'})
    assert ex.status_code == 200 and ex.headers["content-type"].startswith("application/x-ndjson")
    assert "\n" not in ex.headers["content-disposition"] and '"' not in ex.headers["content-disposition"][22:-1]
    all_ = api.get("/api/logs/export")
    assert len(all_.text.strip().splitlines()) == 3
    ok = api.post("/api/logs", json={"source": "harness", "rows": [{"ts": TS, "event": "h", "run_id": "h1"}]})
    assert ok.status_code == 200 and ok.json()["accepted"] == 1
    assert api.post("/api/logs", json={"source": "x", "rows": []}).status_code == 422
    evil = api.post("/api/logs", json={"source": "x", "rows": [{"ts": TS, "event": "e", "run_id": "r"}]},
                    headers={"origin": "https://evil.example"})
    assert evil.status_code == 403                         # a web page cannot write into your logs


# ------------------------------------------------------------------ SDK

def test_sdk_spawns_reads_dumps_and_ingests(env, api, tmp_path):
    px = sdk(api)
    with pytest.raises(ValueError):
        px.spawn("do a thing that is long enough", "b", "a reason long enough", approved_by="")
    t = px.spawn("add a --json flag to plexar status", "b", reason="one CLI nicety, alone", approved_by="len")
    got = tasks.get("b", t["id"])
    assert got["state"] == tasks.SELECTED
    assert not px.tasks("b", state="held")
    assert px.run(t["id"], "b")["state"] == "selected"
    seed(4)
    rows = list(px.logs(page=1))                           # pages transparently, one row at a time
    assert len(rows) >= 4 and len({json.dumps(r, sort_keys=True) for r in rows}) == len(rows)
    n = px.export_logs(str(tmp_path / "dump.ndjson"), event="review")
    assert n == 2 and all(json.loads(l)["event"] == "review" for l in (tmp_path / "dump.ndjson").read_text().splitlines())
    res = px.ingest([{"ts": TS, "event": "e", "run_id": "r"}] * 3 + [{"event": "bad"}], "orch", batch=2)
    assert res["accepted"] == 3 and [x["index"] for x in res["rejected"]] == [3]
    with pytest.raises(PlexarError) as e:
        px.run("T-nope", "b")
    assert e.value.status == 404


def test_sdk_unreachable_says_how_to_start():
    with pytest.raises(PlexarError, match="plexar up"):
        Client(url="http://127.0.0.1:9", timeout=0.5).summary()


# ------------------------------------------------------------------ CLI

def test_cli_logs_and_spawn(env, tmp_path, capsys):
    seed(3)
    assert cli.main(["logs", "where"]) == 0
    assert str(env / "logs") in capsys.readouterr().out
    assert cli.main(["logs", "--event", "review"]) == 0
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert out and all(r["event"] == "review" for r in out)
    assert cli.main(["logs", "export", "--out", str(tmp_path / "d.ndjson")]) == 0
    assert len((tmp_path / "d.ndjson").read_text().splitlines()) == 3
    src = tmp_path / "mine.ndjson"
    src.write_text(json.dumps({"ts": TS, "event": "e", "run_id": "r"}) + "\nnot json\n", encoding="utf-8")
    assert cli.main(["logs", "ingest", str(src), "--source", "mine"]) == 1   # one line bad -> non-zero
    assert "accepted 1, rejected 0, unparseable lines 1" in capsys.readouterr().out
    assert cli.main(["spawn", "a prompt long enough to be a task", "--reason", "one standalone CLI change, nothing else touches it",
                     "--approved-by", "len", "--bucket", "b"]) == 0
    tid = capsys.readouterr().out.split()[0]
    assert tasks.get("b", tid)["state"] == tasks.SELECTED
    assert logs.read(event="spawn")["rows"][0]["approved_by"] == "len"
