"""C1n's named accept — the 3-for-3 hash failure, as a test.

Measured 2026-09-21 on a live 12-node SWARM: all three `brief_sha256` values failed to
verify because the orchestrator hashed the string it held (LF) while Windows wrote the
file as CRLF. The cards were byte-perfect; only the verification was broken.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import cards  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PLEXAR_AGENTS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PLEXAR_AGENTS_STATE", str(tmp_path / "state" / "runs"))
    return tmp_path


def test_a_crlf_card_hashes_to_its_lf_content(env):
    """THE defect. A card typed with CRLF must not produce a hash nobody can verify."""
    got = cards.write("R1", "W1", "line one\r\nline two\r\n")
    assert got["verified"] is True
    raw = pathlib.Path(got["path"]).read_bytes()
    assert b"\r\n" not in raw, "the file on disk is LF"
    assert got["sha256"] == hashlib.sha256(b"line one\nline two\n").hexdigest()


def test_the_hash_is_of_the_bytes_on_disk_not_of_a_string(env):
    got = cards.write("R1", "W1", "some card body\n")
    on_disk = hashlib.sha256(pathlib.Path(got["path"]).read_bytes()).hexdigest()
    assert got["sha256"] == on_disk


def test_a_logged_hash_verifies_against_the_file_it_names(env):
    got = cards.write("R1", "W1", "brief\r\nwith\r\nCRLF\r\n")
    v = cards.verify(got["path"], got["sha256"])
    assert v["matches"] is True, "this is the join that returned zero matches, 3 for 3"


def test_a_wrong_hash_is_reported_as_a_mismatch_not_a_silence(env):
    got = cards.write("R1", "W1", "body\n")
    v = cards.verify(got["path"], "0" * 64)
    assert v["matches"] is False and v["actual"] == got["sha256"]


def test_a_lone_cr_is_normalised_too(env):
    got = cards.write("R1", "W1", "old\rmac\rline endings\r")
    assert b"\r" not in pathlib.Path(got["path"]).read_bytes()
    assert got["verified"] is True


def test_a_resumed_worker_writes_a_delta_not_a_card(env):
    """Both produce attempts: 2 and they are completely different inputs."""
    got = cards.write("R1", "W1", "just the unblocking answer\n", attempt=2, is_delta=True)
    body = pathlib.Path(got["path"]).read_text(encoding="utf-8")
    assert body.startswith("[DELTA"), "it opens saying it is not a standalone card"
    assert "W1-attempt2" in got["path"]
    assert got["retry_mode"] == "resume_delta"


def test_a_respawn_is_labelled_differently_from_a_resume(env):
    a = cards.write("R1", "W2", "fresh card\n", attempt=2, is_delta=False)
    b = cards.write("R1", "W3", "delta\n", attempt=2, is_delta=True)
    assert a["retry_mode"] == "respawn" and b["retry_mode"] == "resume_delta"


def test_an_unwritable_path_never_raises_into_the_caller(env, monkeypatch):
    # A FILE where a directory must go. An earlier version of this test used a null
    # byte, which monkeypatch.setenv rejects before the code under test ever runs —
    # a malformed check, not a failing state (§10).
    blocker = env / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setenv("PLEXAR_AGENTS_ARTIFACTS", str(blocker))
    got = cards.write("R1", "W1", "body\n")
    assert got["verified"] is False and got["sha256"] is None
    assert "error" in got, "the failure is reported, not swallowed into a false success"


def test_verified_is_computed_from_disk_never_asserted(env):
    """If the file is altered after writing, verified must not still claim true."""
    got = cards.write("R1", "W1", "original\n")
    pathlib.Path(got["path"]).write_text("tampered\n", encoding="utf-8", newline="")
    assert cards.verify(got["path"], got["sha256"])["matches"] is False
