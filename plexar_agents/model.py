"""Which model the framework talks to: the user's choice, never ours (PLAN D59).

The framework needs a model for one job, the task builder. It speaks the one wire almost
every provider offers: OpenAI-compatible `POST {url}/chat/completions`. Ollama, vLLM,
OpenRouter, a hosted API or a Plexar-LLM rig all work, and none is the default. With nothing
configured, the task builder says so and stops; it never falls back to someone else's machine.

    plexar model --url http://localhost:11434/v1 --model qwen3:14b           # Ollama, no key
    plexar model --url https://openrouter.ai/api/v1 --model <id> --key-env OPENROUTER_API_KEY
    plexar model --url http://localhost:8000/v1 --model <id> --key-env <VAR holding YOUR key> \\
                 --extra '{"chat_template_kwargs": {"enable_thinking": false}}'

**The key is never written to disk by the framework.** The config stores the NAME of the
environment variable that holds it (`key_env`), so the file can be shared or committed
without leaking a credential. `PLEXAR_LLM_KEY` in the environment also works directly.

Environment overrides the file, for one-off runs: `PLEXAR_LLM_URL`, `PLEXAR_LLM_MODEL`,
`PLEXAR_LLM_KEY`.

**What is logged:** the endpoint's host, the model id, and WHERE the key came from
(`env:OPENROUTER_API_KEY`), never the key.
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request

from . import tasks

TIMEOUT_S = 300


class NoModel(RuntimeError):
    pass


def config_path() -> pathlib.Path:
    return tasks.dir_().parent / "model.json"


def _file() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def configure(url: str, model: str, key_env: str | None = None,
              extra: dict | None = None) -> dict:
    u = urllib.parse.urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("url must be http(s)://host[:port]/path — got %r" % url)
    if not (model or "").strip():
        raise ValueError("model is required: the id your endpoint serves")
    if key_env is not None and not key_env.replace("_", "").isalnum():
        raise ValueError("key_env is the NAME of an environment variable, not the key")
    cfg = {"url": url.rstrip("/"), "model": model.strip(), "key_env": key_env,
           "extra": extra or {}}
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return cfg


def resolve() -> dict:
    f = _file()
    url = os.environ.get("PLEXAR_LLM_URL") or f.get("url")
    model = os.environ.get("PLEXAR_LLM_MODEL") or f.get("model")
    if not url or not model:
        raise NoModel("no model configured. Point the framework at any OpenAI-compatible "
                      "endpoint: plexar model --url <.../v1> --model <id> [--key-env VAR]")
    key, source = None, "none"
    if os.environ.get("PLEXAR_LLM_KEY"):
        key, source = os.environ["PLEXAR_LLM_KEY"], "env:PLEXAR_LLM_KEY"
    elif f.get("key_env"):
        key = os.environ.get(f["key_env"]) or None
        source = "env:%s" % f["key_env"] if key else "missing:%s" % f["key_env"]
    return {"url": url.rstrip("/"), "model": model, "key": key, "key_source": source,
            "host": urllib.parse.urlparse(url).netloc, "extra": f.get("extra") or {}}


def list_models(url: str, key: str | None, timeout: float = 15) -> dict:
    """Ask an endpoint what this key may use: GET <url>/models.

    Returns {"status": "ok"|"unauthorized"|"not_granted"|"refused"|"unreachable", "code",
    "models": [chat model ids], "detail"}. Entries an endpoint marks as non-chat (e.g.
    `plexar.kind == "classifier"`) are skipped. Nothing is chosen here: which model to use
    is the caller's (or the user's) decision, never a hard-coded id in the library.
    """
    h = {"Authorization": "Bearer " + key} if key else {}
    req = urllib.request.Request(url.rstrip("/") + "/models", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}").get("error") or {}
        except ValueError:
            err = {}
        code = err.get("code") if isinstance(err, dict) else None
        status = ("unauthorized" if e.code == 401 else
                  "not_granted" if e.code == 403 and code == "product_not_granted" else "refused")
        return {"status": status, "code": e.code, "models": [], "detail": code or e.reason}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"status": "unreachable", "code": None, "models": [], "detail": str(e)}
    ids = []
    for m in (data.get("data") or []):
        kind = ((m.get("plexar") or {}).get("kind") if isinstance(m.get("plexar"), dict) else None)
        if isinstance(m, dict) and m.get("id") and kind not in ("classifier", "embedding"):
            ids.append(m["id"])
    return {"status": "ok", "code": 200, "models": ids, "detail": None}


def describe() -> dict:
    """resolve() without the key: what is safe to log or print."""
    r = resolve()
    return {k: v for k, v in r.items() if k != "key"}


def chat(prompt: str, headers: dict | None = None) -> str:
    """One streamed completion. Streaming keeps a long answer from being cut as idle
    (measured on a vLLM gateway: a non-streamed call was dropped at ~45 s)."""
    r = resolve()
    # The key is OPTIONAL: the library is keyless; the key belongs to the endpoint the user
    # points it at. No key -> no Authorization header, which is right for
    # a local endpoint (Ollama, vLLM on localhost). A missing key is only an error when the
    # endpoint itself says so (401/403), and then the message names the variable to set.
    body = {"model": r["model"], "temperature": 0, "stream": True, "max_tokens": 6000,
            "messages": [{"role": "user", "content": prompt}], **r["extra"]}
    h = {"Content-Type": "application/json", **(headers or {})}
    if r["key"]:
        h["Authorization"] = "Bearer " + r["key"]
    req = urllib.request.Request(r["url"] + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=h)
    out = []
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            hint = ("no key was sent: %s is not set in this environment"
                    % r["key_source"].split(":", 1)[1] if r["key_source"].startswith("missing:")
                    else "no key is configured (plexar model --key-env <VAR>)" if not r["key"]
                    else "the key in %s was refused" % r["key_source"].split(":", 1)[1])
            raise NoModel("%s answered %d: %s" % (r["host"], e.code, hint)) from None
        raise
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0].get("delta") or {}
            except (ValueError, KeyError, IndexError):
                continue
            out.append(delta.get("content") or "")
    return "".join(out)
