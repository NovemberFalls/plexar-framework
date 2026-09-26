"""D59 — the model is the user's choice. No default endpoint, and no key on disk."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import cli, model, slicer, sweep  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    for v in ("PLEXAR_LLM_URL", "PLEXAR_LLM_MODEL", "PLEXAR_LLM_KEY"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def test_nothing_configured_means_no_model_not_our_rig(env):
    with pytest.raises(model.NoModel):
        model.resolve()
    with pytest.raises(sweep.SweepRefused, match="no model configured"):
        slicer.slice_text("some words " * 50, "b")          # default llm = configured model


def test_key_is_referenced_by_name_never_stored(env, monkeypatch):
    model.configure("http://localhost:11434/v1", "qwen3:14b", key_env="MY_KEY")
    raw = model.config_path().read_text(encoding="utf-8")
    monkeypatch.setenv("MY_KEY", "sk-secret-123")
    assert "sk-secret" not in raw
    r = model.resolve()
    assert r["key"] == "sk-secret-123" and r["key_source"] == "env:MY_KEY"
    assert "key" not in model.describe()


def test_no_key_is_fine_for_a_local_endpoint(env):
    model.configure("http://localhost:11434/v1", "llama3")
    assert model.resolve()["key"] is None and model.resolve()["key_source"] == "none"


def _stub(status: int):
    """A one-shot OpenAI-compatible endpoint that records the headers it was sent."""
    import http.server
    import threading
    seen = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if status != 200:
                self.send_response(status)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_a_named_but_unset_key_sends_no_header_and_still_works(env):
    # The library is keyless; a missing key is never an error by itself.
    srv, seen = _stub(200)
    try:
        model.configure("http://127.0.0.1:%d/v1" % srv.server_port, "x", key_env="NOT_SET_ANYWHERE_42")
        assert model.resolve()["key_source"] == "missing:NOT_SET_ANYWHERE_42"
        assert model.chat("hi") == "ok"
        assert seen["auth"] is None                      # never "Bearer " with an empty key
    finally:
        srv.shutdown()


def test_a_401_names_the_variable_to_set(env):
    srv, seen = _stub(401)
    try:
        model.configure("http://127.0.0.1:%d/v1" % srv.server_port, "x", key_env="NOT_SET_ANYWHERE_42")
        with pytest.raises(model.NoModel, match="answered 401: no key was sent: NOT_SET_ANYWHERE_42 is not set"):
            model.chat("hi")
    finally:
        srv.shutdown()


def test_env_overrides_file(env, monkeypatch):
    model.configure("http://a/v1", "m1")
    monkeypatch.setenv("PLEXAR_LLM_URL", "http://b/v1")
    assert model.resolve()["url"] == "http://b/v1"


def test_bad_input_is_refused(env):
    with pytest.raises(ValueError):
        model.configure("localhost:11434", "m")
    with pytest.raises(ValueError, match="NAME"):
        model.configure("http://a/v1", "m", key_env="sk-live-abc-123")


def test_cli_sets_and_shows(env, capsys):
    assert cli.main(["model"]) == 1
    assert cli.main(["model", "--url", "http://h:1/v1", "--model", "m",
                     "--extra", '{"chat_template_kwargs": {"enable_thinking": false}}']) == 0
    out = capsys.readouterr().out
    assert "http://h:1/v1" in out and "enable_thinking" in out
    assert json.loads(model.config_path().read_text())["extra"]["chat_template_kwargs"]


def _models_stub(status: int, body: dict):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/v1" % srv.server_port


def test_list_models_skips_classifiers_and_names_refusals(env):
    srv, url = _models_stub(200, {"data": [{"id": "chat-a"}, {"id": "sig", "plexar": {"kind": "classifier"}},
                                           {"id": "chat-b"}]})
    try:
        assert model.list_models(url, None)["models"] == ["chat-a", "chat-b"]
    finally:
        srv.shutdown()
    for status, body, want in [(401, {}, "unauthorized"),
                               (403, {"error": {"code": "product_not_granted"}}, "not_granted"),
                               (403, {"error": {"code": "other"}}, "refused")]:
        srv, url = _models_stub(status, body)
        try:
            assert model.list_models(url, "k")["status"] == want
        finally:
            srv.shutdown()
    assert model.list_models("http://127.0.0.1:9/v1", None)["status"] == "unreachable"


def test_model_without_a_name_uses_the_only_chat_model_or_asks(env, capsys):
    srv, url = _models_stub(200, {"data": [{"id": "only-one"}, {"id": "sig", "plexar": {"kind": "classifier"}}]})
    try:
        assert cli.main(["model", "--url", url]) == 0
        assert model.resolve()["model"] == "only-one"
        assert "using the only chat model" in capsys.readouterr().out
        assert cli.main(["model", "--check"]) == 0
        assert "check:    ok" in capsys.readouterr().out
    finally:
        srv.shutdown()
    srv, url = _models_stub(200, {"data": [{"id": "big"}, {"id": "small"}]})
    try:
        assert cli.main(["model", "--url", url]) == 2          # several: the user chooses, never us
        out = capsys.readouterr().out
        assert "choose one with --model" in out and "  big" in out and "  small" in out
        assert model.resolve()["model"] == "only-one"         # nothing was changed
        model.configure(url, "gone-model")
        assert cli.main(["model", "--check"]) == 1
        assert "gone-model is not offered" in capsys.readouterr().out
    finally:
        srv.shutdown()
    srv, url = _models_stub(401, {})
    try:
        assert cli.main(["model", "--url", url]) == 2
        assert "does not know this key" in capsys.readouterr().out
    finally:
        srv.shutdown()
