"""B0bn — the sweep proves nothing was silently dropped.

The runtime never splits. Every test here hands it a split a model might have produced
and asks what the runtime can say about it.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import sweep, tasks  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def rows(tmp_path):
    out = []
    for f in (tmp_path / "logs").glob("*.jsonl"):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


A = "Fix the login redirect loop on mobile, it bounces twice before settling. "
B = "Then make the invoice PDF stop dropping the tax line for EU customers. "
C = "And the settings page needs a dark mode that actually persists across reloads. "
MONO = A + B + C


def whole_split():
    out, at = [], 0
    for part in (A, B, C):
        out.append({"prompt": part.strip(), "span": [at, at + len(part)]})
        at += len(part)
    return out


# ------------------------------------------------- coverage is the point

def test_a_full_split_is_committable(env):
    r = sweep.review(MONO, whole_split())
    assert r["ok"] is True and len(r["tasks"]) == 3
    assert r["coverage"] == 1.0 and r["uncovered"] == []


def test_a_split_that_DROPS_a_third_of_the_monologue_is_refused(env):
    """THE failure: you said three things, two became tasks, nobody noticed."""
    r = sweep.review(MONO, whole_split()[:2])
    assert r["ok"] is False
    assert r["coverage"] < 0.85
    assert len(r["uncovered"]) == 1
    assert "dark mode" in r["uncovered"][0]["text"], "the dropped text is SHOWN, not counted"


def test_the_dropped_text_is_quoted_so_a_human_can_see_what_fell_out(env):
    r = sweep.review(MONO, whole_split()[:1])
    joined = " ".join(u["text"] for u in r["uncovered"])
    assert "invoice PDF" in joined and "dark mode" in joined


def test_committing_a_refused_split_creates_nothing(env):
    r = sweep.review(MONO, whole_split()[:2])
    with pytest.raises(sweep.SweepRefused) as e:
        sweep.commit("b", r)
    assert "Nothing was created" in str(e.value)
    assert "silence is not a decision" in str(e.value)
    assert tasks.counts("b")["drafted"] == 0


# ------------------------------------------------- declining is a decision

def test_a_span_can_be_declined_but_only_with_a_reason(env):
    split = whole_split()[:2] + [{"kind": "not_a_task", "why": "an aside about the weather",
                                  "span": [len(A) + len(B), len(MONO)]}]
    r = sweep.review(MONO, split)
    assert r["ok"] is True and len(r["tasks"]) == 2 and len(r["declined"]) == 1
    assert r["coverage"] == 1.0, "a declined span is COVERED — it was read and judged"


def test_declining_without_a_reason_is_a_problem(env):
    split = whole_split()[:2] + [{"kind": "not_a_task",
                                  "span": [len(A) + len(B), len(MONO)]}]
    r = sweep.review(MONO, split)
    assert r["ok"] is False
    assert any("without saying why" in p for p in r["problems"])


# ------------------------------------------------- shape checks

def test_a_fragment_is_not_a_brief(env):
    split = [{"prompt": "fix it", "span": [0, len(MONO)]}]
    r = sweep.review(MONO, split)
    assert r["ok"] is False
    assert any("fragment, not a brief" in p for p in r["problems"])


def test_a_candidate_with_no_usable_span_is_a_problem(env):
    split = [{"prompt": A.strip(), "span": [0, 5000]}]
    r = sweep.review(MONO, split)
    assert any("no usable span" in p for p in r["problems"])


def test_an_empty_split_is_never_ok(env):
    r = sweep.review(MONO, [])
    assert r["ok"] is False and r["tasks"] == []


# ------------------------------------------------- the grouping is shown first

def test_review_creates_nothing(env):
    sweep.review(MONO, whole_split())
    assert tasks.counts("b")["drafted"] == 0, "the grouping is shown BEFORE anything is held"


def test_commit_holds_by_default(env):
    r = sweep.review(MONO, whole_split())
    made = sweep.commit("b", r)
    assert len(made) == 3
    c = tasks.counts("b")
    assert c["held"] == 3 and c["drafted"] == 0 and c["running"] == 0


def test_the_prompt_survives_the_sweep_intact(env):
    r = sweep.review(MONO, whole_split())
    sweep.commit("b", r)
    prompts = [t["prompt"] for t in tasks.all_("b").values()]
    assert A.strip() in prompts, "a task's prompt is the source text, not a paraphrase"


# ------------------------------------------------- the record

def test_the_floor_is_labelled_unmeasured(env, tmp_path):
    r = sweep.review(MONO, whole_split())
    assert "UNMEASURED" in r["floor_source"]
    row = [x for x in rows(tmp_path) if x["event"] == "sweep_review"][0]
    assert "UNMEASURED" in row["floor_source"], "the guess is labelled in the DATA"


def test_a_refusal_is_recorded_with_what_was_missed(env, tmp_path):
    r = sweep.review(MONO, whole_split()[:2])
    with pytest.raises(sweep.SweepRefused):
        sweep.commit("b", r)
    row = [x for x in rows(tmp_path) if x["event"] == "sweep_refused"][0]
    assert row["uncovered_runs"] == 1 and row["coverage"] < 0.85


def test_a_commit_records_coverage_alongside_the_count(env, tmp_path):
    r = sweep.review(MONO, whole_split())
    sweep.commit("b", r, source="conv-a")
    row = [x for x in rows(tmp_path) if x["event"] == "sweep_commit"][0]
    assert row["created"] == 3 and row["coverage"] == 1.0 and row["source"] == "conv-a"
