# Plexar-Framework

**Works with Claude Code, Codex, the Plexar Harness, or any agent command.** The Framework is
applied to whatever agent and models you already use; it has no model or provider of its own.

The control plane for agent runs. You submit prompts into a bucket, an agent (or you) picks a
batch with a stated reason, a named human approves it, and a daemon runs it. The task
lands only if the gate exits 0. `D<n>` labels in the code are design decisions; each is
explained where it is enforced.

## See it work

[**Plexar Notes**](https://github.com/NovemberFalls/plexar-notes) is an Obsidian-style note
taker built end to end by this framework: one goal in, a planner split it into 18 tasks with
checks, a named person approved the plan once, agents built each task on its own branch, the
gate and a verifier/approver review chain judged every one, and a person accepted the result.
Its [`showcase/`](https://github.com/NovemberFalls/plexar-notes/tree/main/showcase) folder is
the full record: the goal as typed, every task's check before and after, each reviewer's
verdict (including the ones they sent back), and the screenshots.

## Run it

```sh
pip install -e .                 # once, from this repo — gives you the `plexar` command

cd /path/to/your/repo
plexar init                      # this repo becomes a bucket; finds a gate (pytest / npm test)
plexar up                        # server + daemon in the background, opens /app
plexar status                    # what is running, what is configured
plexar down
```

`init` refuses a repo with no gate: a task nobody can check cannot land. Pass
`--gate "<cmd>"`, `--runner "<cmd>"` (default `claude -p --permission-mode acceptEdits`, the
prompt arrives on stdin) or `--bucket <name>` to override.

**The runner uses your own login or API key for that agent; the Framework holds none.**
Sign in to the agent you use (for example with your Claude or ChatGPT plan) or set its API
key on this machine before approving work. If the agent isn't signed in, the task stops
with that message rather than a generic failure; sign in and requeue it.

**Every task runs on its own branch** `plexar/<task id>` cut from the repo's current
branch, and is committed there — pass or fail. The base branch is never written to, a
dirty working tree is refused (never stashed), and merging is yours to do. Click a
finished task in `/app` to see its diff, the agent's output and the gate result.

Logs and state live in `%LOCALAPPDATA%\plexar-agents\`. The server is on
`127.0.0.1:8430` (8420 is Plexar Studio).

## About AI verification

The framework can put every task through a review chain: a worker, a verifier that
**runs the work and photographs anything visual**, and an approver that checks the verifier.
You get a QA report with the evidence, the screenshots, and each model's verdict, so you
can get through QA much faster.

**It is still AI judgement, and it does not replace yours.** How well it works depends on how
tasks are prompted and on your project; it works reliably for us, and your results may
differ. Treat every AI verdict as a lead to confirm, not a sign-off: open the report, check
the cases (the visual ones first), and record your own verdict. Those verdicts are kept
locally beside the AI's, and they are what make the next report more accurate.

## The API

Everything `/app` does is one `/api` call; the full list is at `/api-docs`.

| route | what |
|---|---|
| `POST /api/buckets/{b}/tasks` `{prompt}` | submit → 202, held |
| `POST /api/buckets/{b}/selections` `{task_ids, reason, agent}` | take a batch; 422 names the rule it broke |
| `POST /api/buckets/{b}/selections/{sid}/approve` `{by}` | the explicit yes (buckets default to `ask`) |
| `POST /api/buckets/{b}/tasks/{id}/start` `{run_id}` | the lease; 409 full/claimed, 428 unapproved |
| `POST …/beat` · `POST …/finish` `{ok, gate_exit}` | heartbeat; verdict (only `gate_exit == 0` lands) |
| `GET /api/daemon` | what the daemon would run, read-only |

Browsers cannot write to it from another origin, but the API has **no auth**: never bind
it off the machine (`PLEXAR_FRAMEWORK_HOST`) until O2 is decided.

## It keeps running

`plexar up` starts the server and daemon so that closing the terminal, pane or app you
ran it from does not stop them. Plexar Studio, for one, kills every process in a pane's
Job Object when the pane closes; the framework starts outside it (via WMI when the job
forbids breakaway). `plexar down` stops it.

**It also starts at login, by default.** The first `plexar up` adds a per-user login item
(no admin): a hidden script in your Startup folder on Windows, a LaunchAgent on macOS, an
XDG autostart entry on Linux. `plexar autostart off` removes it and is remembered;
`plexar autostart on` puts it back; `plexar up --no-autostart` skips it once.

## Agents ask instead of guessing

Every worker is told: if the task is unclear, contradictory or looks like a mistake (a likely
typo, say), print one line `QUESTION: ...` and stop. Reviewers can answer with the verdict
`question` too. The question climbs: the verifier is asked, then the approver. Each answers only
if the task, the repo and the task memory make the answer clear. If nobody above can answer,
the task goes back to Held with the question open, and you answer it in `/app` (the
Questions tab, or `POST /api/buckets/{b}/tasks/{id}/answer`). Every answer is kept, and every
later run's prompt carries it.

## A task's own check

A task can carry a check: a command that must **fail** on the code as it was before the work,
and **pass** after it (`plexar queue "..." --check "<cmd>"`, or the check field in `/app`). It
is run first on a clean copy of the base commit. A check that already passes there proves
nothing, so the run stops and says so. After the work, it runs beside the bucket's gate, and
the task only lands if both pass. The board shows the check's output before and after.

## Task memory

Each task keeps a memory file in the project it works on: `.plexar/tasks/<task id>.json`.
Every run, attempt, check result, reviewer verdict and person's note is added to it as it
happens. The agent is told where it is (`PLEXAR_TASK_MEMORY`), reads it first, and can add its
own notes. So a run that crashes, is retried or comes back from review starts from what is
known, not from zero, and you can read afterwards how the task went (`/app` → the task →
Task memory, or `plexar memory <task id>`).

`.plexar/` hides itself from git (it carries its own `.gitignore`), so it never touches your
branches. Memory is kept by default. `plexar memory --clear-on-accept on` makes a person's
final acceptance ("Merged / keep") clear it.

## Your logs

Every decision, dispatch, review hop, gate result and human verdict is a row in **your**
log folder: one JSON object per line, one file per day. The folder is the log. Open it,
grep it, copy it or ship it; nothing about it is hidden or proprietary.

| where | default |
|---|---|
| Windows | `%LOCALAPPDATA%\plexar-agents\logs` |
| macOS | `~/Library/Application Support/plexar-agents/logs` |
| Linux | `$XDG_DATA_HOME/plexar-agents/logs` (or `~/.local/share/...`) |

`plexar logs where` prints the real path. `plexar logs dir --set <folder>` moves where new
rows go (a team share, say); `PLEXAR_AGENTS_LOG_DIR` overrides both. `PLEXAR_AGENTS_HOME`
moves everything (tasks, counters, logs, artifacts) at once.

Four doors onto the same files:

| | read | dump | bring your own rows in |
|---|---|---|---|
| CLI (works with the server down) | `plexar logs [--event --run --task --from --to --follow]` | `plexar logs export --out f.ndjson` | `plexar logs ingest f.ndjson --source ci` |
| HTTP | `GET /api/logs?cursor=&event=…` (paged) | `GET /api/logs/export` (NDJSON download) | `POST /api/logs {source, rows}` |
| Python | `Client().logs(event="review")` | `Client().export_logs("f.ndjson")` | `Client().ingest(rows, source="ci")` |
| Claude Code | `/plexar-logs` | `/plexar-logs` | `/plexar-logs` |

Rows you bring in must carry `event`, `run_id` and a `ts` with a UTC offset (plus the
required keys for any event in `plexar_agents/ledger_schema.json`). They land in
`ingested-<day>.jsonl`, tagged with your `source`, never mixed into the framework's own
files; rejected rows come back with the reason. Nothing is repaired or guessed.

## Bring your own endpoint and key

The Framework has no account, no sign-in and no key of its own. It is a library: the key
belongs to whatever model endpoint you point it at.

```sh
plexar model --url http://localhost:11434/v1 --model qwen3:14b              # Ollama: no key
plexar model --url https://openrouter.ai/api/v1 --model <id> --key-env OPENROUTER_API_KEY
plexar model --url http://localhost:8000/v1 --model <id>     # vLLM, LM Studio, any OpenAI-compatible server
```

`--key-env` names the environment variable that holds the key; the key itself is never
written to disk. It is optional. With no key, no `Authorization` header is sent, which is
right for a local endpoint. A missing key only becomes an error when the endpoint answers
401/403, and the message then says which variable to set. Agents the Framework runs (Claude
Code, Codex, the Plexar Harness, ...) bring their own sign-in and models.

## Drive it from code

```python
from plexar_agents.client import Client
px = Client()                                            # http://127.0.0.1:8430
t = px.spawn("add a --json flag to plexar status", bucket="framework",
             reason="one standalone CLI change", approved_by="len")
run = px.wait(t["id"], bucket="framework")               # done / failed
print(run["gate_exit"], run["review_final"], run["branch"])
```

`spawn` queues, selects and approves in one call; `approved_by` is required and must be
the person who said yes (D17). The CLI twin is `plexar spawn "…" --reason … --approved-by …`.
Standard library only; every call maps to one `/api` route.

## Checks

```sh
python -m pytest tests -q
python scripts/compose_m2e.py      # M2 end to end over HTTP, daemon started after submission
```
