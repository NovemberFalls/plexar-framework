"""The Python SDK: everything the /app page can do, from code. Standard library only.

    from plexar_agents.client import Client
    px = Client()                                   # http://127.0.0.1:8430 by default

    # spawn work: queue it, select it with a reason, and approve it as a NAMED human
    t = px.spawn("add a --json flag to plexar status", bucket="framework",
                 reason="one CLI nicety", approved_by="len")
    run = px.wait(t["id"], bucket="framework")      # blocks until done/failed
    print(run["gate_exit"], run["review_final"], run["branch"])

    # dump the logs, or stream them
    px.export_logs("today.ndjson", date_from="2026-09-23")
    for row in px.logs(event="review"):             # pages for you, oldest first
        ...

    # bring your own rows in (another orchestrator, CI, a harness)
    px.ingest([{"ts": "2026-09-23T18:00:00-04:00", "event": "ci_run", "run_id": "ci-812"}],
              source="github-actions")

Approval stays a human act (D17): `spawn` refuses without `approved_by`, and that name is
what the ledger records. An agent calling this should pass the name of the person who
said yes, never its own.

The same surface is on the CLI (`plexar logs`, `plexar spawn`) and over plain HTTP
(`/api-docs` on the running server lists every route).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:%s" % os.environ.get("PLEXAR_FRAMEWORK_PORT", "8430")
TERMINAL = {"done", "failed", "abandoned", "cancelled"}


class PlexarError(Exception):
    def __init__(self, status: int, detail):
        self.status, self.detail = status, detail
        super().__init__("%s: %s" % (status, detail))


class Client:
    def __init__(self, url: str | None = None, timeout: float = 30.0, transport=None):
        """`transport(method, path, body) -> (status, bytes)` replaces HTTP (tests use it)."""
        self.url = (url or os.environ.get("PLEXAR_FRAMEWORK_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self._transport = transport or self._http

    # -------------------------------------------------------------- transport
    def _http(self, method, path, body):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"content-type": "application/json"} if body is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except urllib.error.URLError as e:
            raise PlexarError(0, "Plexar-Framework is not reachable at %s (%s). Start it with `plexar up`."
                              % (self.url, e.reason))

    def _raw(self, method, path, body=None) -> bytes:
        status, data = self._transport(method, path, body)
        if status >= 400:
            try:
                detail = json.loads(data).get("detail")
            except ValueError:
                detail = data[:300].decode("utf-8", "replace")
            raise PlexarError(status, detail)
        return data

    def _call(self, method, path, body=None):
        return json.loads(self._raw(method, path, body) or b"null")

    @staticmethod
    def _q(**params) -> str:
        p = {k: v for k, v in params.items() if v not in (None, "")}
        return ("?" + urllib.parse.urlencode(p)) if p else ""

    @staticmethod
    def _b(bucket: str) -> str:
        return urllib.parse.quote(bucket, safe="")

    # -------------------------------------------------------------- the pool
    def health(self) -> bool:
        try:
            self._raw("GET", "/healthz")
            return True
        except PlexarError:
            return False

    def summary(self) -> dict:
        return self._call("GET", "/api/summary")

    def daemon(self) -> dict:
        return self._call("GET", "/api/daemon")

    def buckets(self) -> list:
        return self._call("GET", "/api/buckets")

    def tasks(self, bucket: str, state: str | None = None) -> list:
        return self._call("GET", "/api/buckets/%s/tasks%s" % (self._b(bucket), self._q(state=state)))

    def submit(self, prompt: str, bucket: str, source: str = "sdk", session_id: str | None = None) -> dict:
        body = {"prompt": prompt, "source": source}
        if session_id or os.environ.get("PLEXAR_SESSION_ID"):
            body["session_id"] = session_id or os.environ["PLEXAR_SESSION_ID"]
        return self._call("POST", "/api/buckets/%s/tasks" % self._b(bucket), body)

    def select(self, task_ids: list, bucket: str, reason: str, agent: str = "sdk") -> dict:
        return self._call("POST", "/api/buckets/%s/selections" % self._b(bucket),
                          {"task_ids": list(task_ids), "reason": reason, "agent": agent})

    def approve(self, selection_id: str, bucket: str, by: str) -> dict:
        return self._call("POST", "/api/buckets/%s/selections/%s/approve" % (self._b(bucket), selection_id), {"by": by})

    def reject(self, selection_id: str, bucket: str, by: str) -> dict:
        return self._call("POST", "/api/buckets/%s/selections/%s/reject" % (self._b(bucket), selection_id), {"by": by})

    def spawn(self, prompt: str, bucket: str, reason: str, approved_by: str, agent: str = "sdk") -> dict:
        """Queue, select and approve one task so the daemon runs it. Returns the task."""
        if not approved_by or not approved_by.strip():
            raise ValueError("approved_by is required: approval is a named human's yes (D17)")
        t = self.submit(prompt, bucket)
        s = self.select([t["id"]], bucket, reason, agent)
        if s.get("approval") != "auto":
            self.approve(s["selection_id"], bucket, approved_by)
        t["selection_id"] = s["selection_id"]
        return t

    def run(self, task_id: str, bucket: str) -> dict:
        return self._call("GET", "/api/buckets/%s/tasks/%s/run" % (self._b(bucket), task_id))

    def wait(self, task_id: str, bucket: str, timeout: float = 3600, poll: float = 5) -> dict:
        end = time.monotonic() + timeout
        while True:
            r = self.run(task_id, bucket)
            if r.get("state") in TERMINAL:
                return r
            if time.monotonic() > end:
                raise TimeoutError("%s still %s after %ss" % (task_id, r.get("state"), timeout))
            time.sleep(poll)

    def qa_verdict(self, task_id: str, bucket: str, case_id: str, verdict: str, by: str) -> dict:
        return self._call("POST", "/api/buckets/%s/tasks/%s/qa" % (self._b(bucket), task_id),
                          {"case_id": case_id, "verdict": verdict, "by": by})

    def review(self, task_id: str, bucket: str, verdict: str, by: str, note: str = "") -> dict:
        return self._call("POST", "/api/buckets/%s/tasks/%s/review" % (self._b(bucket), task_id),
                          {"verdict": verdict, "by": by, "note": note})

    def events(self, since: str = ""):
        """Task state changes, forever-paging; yields each event once."""
        while True:
            page = self._call("GET", "/api/events" + self._q(since=since, limit=1000))
            yield from page["events"]
            if len(page["events"]) < 1000:
                return
            since = page["cursor"]

    # -------------------------------------------------------------- the logs
    def logs_where(self) -> dict:
        return self._call("GET", "/api/logs/where")

    def logs(self, event=None, run_id=None, task=None, date_from=None, date_to=None, cursor=None, page=1000):
        """Every matching row, oldest first, paging transparently."""
        if isinstance(event, (list, tuple, set)):
            event = ",".join(event)
        while True:
            r = self._call("GET", "/api/logs" + self._q(cursor=cursor, limit=page, event=event, run_id=run_id,
                                                      task=task, date_from=date_from, date_to=date_to))
            yield from r["rows"]
            if not r["more"]:
                return
            cursor = r["cursor"]

    def export_logs(self, path: str, **filters) -> int:
        """Write the matching rows to `path` as NDJSON. Returns the row count."""
        data = self._raw("GET", "/api/logs/export" + self._q(**filters))
        with open(path, "wb") as fh:
            fh.write(data)
        return data.count(b"\n")

    def ingest(self, rows: list, source: str, batch: int = 5000) -> dict:
        """Bring rows in from another tool. Returns {accepted, rejected:[{index, problems}]}."""
        acc, rej = 0, []
        for start in range(0, len(rows), batch):
            r = self._call("POST", "/api/logs", {"source": source, "rows": rows[start:start + batch]})
            acc += r["accepted"]
            rej += [{**x, "index": x["index"] + start} for x in r["rejected"]]
        return {"accepted": acc, "rejected": rej}
