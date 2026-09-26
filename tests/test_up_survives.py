"""`plexar up` outlives whatever launched it, and starts at login by default.

Found 2026-09-23: Studio restarted every pane at once ("started 18m ago" on all 13), and the
framework's server and daemon died with the pane that had run `plexar up`, leaving Studio's
TASKS view on "Plexar-Framework isn't running". Studio runs each pane in a Job Object with
KILL_ON_JOB_CLOSE. The live test puts a "pane" in exactly that
job, runs `plexar up` from it, closes the job, and the server must still answer.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plexar_agents import autostart, paths  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXAR_AGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path / "userhome"))
    return tmp_path


def test_autostart_is_on_by_default_and_off_is_remembered(home):
    assert not autostart.is_on()
    assert autostart.ensure_default().startswith("on (installed")
    p = autostart.entry_path()
    assert str(p).startswith(str(home)) and p.exists()
    body = p.read_text(encoding="utf-8")
    assert "plexar_agents.cli up --no-open" in body
    if os.name == "nt":
        assert body.count('""') >= 2 and ", 0, False" in body     # quoted exe, hidden, no wait
    assert autostart.ensure_default() == "on"
    autostart.disable()
    assert not p.exists() and autostart.user_disabled()
    assert autostart.ensure_default().startswith("off (your choice")
    assert not p.exists()                                          # `up` respects the user's no
    autostart.enable()
    assert p.exists() and not autostart.user_disabled()
    assert "log_dir" not in paths.settings()                       # it does not clobber other settings


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


class _StudioJob:
    """The exact job Plexar Studio puts every terminal pane in: KILL_ON_JOB_CLOSE,
    no BREAKAWAY_OK. Closing the handle kills every process in it."""

    def __init__(self):
        import ctypes
        import ctypes.wintypes as wt
        self.k = k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wt.HANDLE
        k.OpenProcess.restype = wt.HANDLE
        k.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k.CloseHandle.argtypes = [wt.HANDLE]

        class BASIC(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wt.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wt.DWORD),
                        ("f", ctypes.c_size_t), ("g", wt.DWORD), ("h", wt.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [("x%d" % i, ctypes.c_uint64) for i in range(6)]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("p1", ctypes.c_size_t), ("p2", ctypes.c_size_t),
                        ("p3", ctypes.c_size_t), ("p4", ctypes.c_size_t)]
        self.job = k.CreateJobObjectW(None, None)
        info = EXT()
        info.Basic.LimitFlags = 0x2000                       # KILL_ON_JOB_CLOSE only
        assert k.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info))

    def add(self, pid: int) -> None:
        h = self.k.OpenProcess(0x1F0FFF, False, pid)
        assert self.k.AssignProcessToJobObject(self.job, h), ctypes_err(self.k)
        self.k.CloseHandle(h)

    def close(self) -> None:
        self.k.CloseHandle(self.job)


def ctypes_err(k) -> str:
    import ctypes
    return "AssignProcessToJobObject failed: %d" % ctypes.get_last_error()


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.killpg(os.getpgid(pid), 9)


@pytest.mark.skipif(os.name != "nt", reason="Job Objects are Windows; POSIX uses a new session")
def test_up_survives_studio_closing_the_pane_that_launched_it(tmp_path):
    port = _free_port()
    env = dict(os.environ, PLEXAR_AGENTS_HOME=str(tmp_path / "home"),
               PLEXAR_AGENTS_LOG_DIR=str(tmp_path / "logs"), PLEXAR_FRAMEWORK_PORT=str(port),
               APPDATA=str(tmp_path / "appdata"), XDG_CONFIG_HOME=str(tmp_path / "xdg"),
               PYTHONPATH=str(ROOT))
    env.pop("PLEXAR_AGENTS_STATE", None)
    # The "pane": a process that runs `plexar up` and then just stays open, like a terminal.
    pane_src = ("import subprocess,sys,time;time.sleep(1.5);"
                "subprocess.run([sys.executable,'-m','plexar_agents.cli','up','--no-open']);"
                "time.sleep(600)")
    pane = subprocess.Popen([sys.executable, "-c", pane_src], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=(os.name != "nt"))
    job = _StudioJob()
    job.add(pane.pid)             # before it runs `plexar up`: its children inherit the job
    pids = {}
    try:
        for _ in range(100):
            if _healthy(port):
                break
            time.sleep(0.2)
        assert _healthy(port), "plexar up never came up"
        pids = json.loads((tmp_path / "home" / "plexar-up.json").read_text(encoding="utf-8"))
        job.close()                               # what Studio does when a pane closes
        pane.wait(timeout=10)
        time.sleep(2)
        assert _healthy(port), "the server died with the pane that launched it"
        out = subprocess.run([sys.executable, "-m", "plexar_agents.cli", "status"], cwd=str(ROOT),
                             env=env, capture_output=True, text=True)
        assert "daemon pid: %d (alive)" % pids["daemon"] in out.stdout
    finally:
        subprocess.run([sys.executable, "-m", "plexar_agents.cli", "down"], cwd=str(ROOT), env=env,
                       capture_output=True)
        for pid in pids.values():
            _kill_tree(pid)
        if pane.poll() is None:
            _kill_tree(pane.pid)
    assert not _healthy(port)
