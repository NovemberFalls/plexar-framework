"""Start a process that does not belong to whoever asked for it.

`plexar up` used to Popen the server and daemon straight from the calling shell. That is
DETACHED on Windows, but the processes still named that shell as their parent, so when
Plexar Studio restarted its panes (2026-09-23: every session "started 18m ago") the tree
kill took the framework down with the pane it happened to be launched from.

What actually killed it was not the process tree (`plexar up` exits at once, so its
children were already orphans) but Studio's Job Object: every pane runs in a job with
KILL_ON_JOB_CLOSE, children inherit the job, and closing the pane kills the whole job.

Fix: a one-shot middleman. `spawn()` starts THIS module, which starts the real process
and exits at once, asking to break away from any job (CREATE_BREAKAWAY_FROM_JOB). When the
job forbids that, as Studio's does, Windows itself creates the process via WMI
(`_start_wmi`), which puts it outside the job. On POSIX it is simply a new session. The
middleman prints the real pid on stdout and nothing else.

    python -m plexar_agents.detach <log-file> <cwd> -- <argv...>
"""
from __future__ import annotations

import os
import subprocess
import sys

CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _flags(breakaway: bool) -> int:
    if os.name != "nt":
        return 0
    f = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return f | (CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)


def _start(argv: list[str], log: str, cwd: str, breakaway: bool) -> subprocess.Popen:
    fh = open(log, "ab")
    return subprocess.Popen(argv, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=_flags(breakaway),
                            start_new_session=(os.name != "nt"), close_fds=True)


_WMI = r"""
$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine=$env:PLEXAR_DETACH_CMD; CurrentDirectory=$env:PLEXAR_DETACH_CWD;
  ProcessStartupInformation=$si }
if ($r.ReturnValue -ne 0) { exit [int]$r.ReturnValue }
$r.ProcessId
"""


def _start_wmi(argv: list[str], log: str, cwd: str) -> int:
    """Have Windows (the WMI service) create the process, so it is in no caller's job.

    Plexar Studio runs every pane in a Job Object with KILL_ON_JOB_CLOSE and without
    BREAKAWAY_OK (its terminal panes), so CREATE_BREAKAWAY_FROM_JOB is refused
    and anything started normally dies when the pane closes. A process created by WMI's
    Win32_Process.Create is parented to the WMI provider host, outside that job.

    Two things WMI does not do, handled by a first stage (`carry`): it starts the process
    with the user's DEFAULT environment, not ours (measured: the server came up on the
    default port with the default state dir), and it cannot redirect output. So WMI starts
    `detach carry <envfile> <log> -- argv`, which loads our environment from a file only this
    user can read, deletes it, and runs argv with output to the log. The pid returned is that
    stage; it lives exactly as long as the real process, and `plexar down` (taskkill /T)
    takes both.
    """
    import json
    import tempfile
    fd, envfile = tempfile.mkstemp(prefix="plexar-detach-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(dict(os.environ), fh)
    stage = [sys.executable, "-m", "plexar_agents.detach", "carry", envfile, log, cwd, "--", *argv]
    env = dict(os.environ, PLEXAR_DETACH_CMD=subprocess.list2cmdline(stage), PLEXAR_DETACH_CWD=cwd)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WMI],
                       env=env, capture_output=True, text=True, timeout=60,
                       creationflags=0x08000000)                   # CREATE_NO_WINDOW
    if r.returncode != 0:
        try:
            os.unlink(envfile)
        except OSError:
            pass
        raise OSError("WMI Win32_Process.Create failed (%s): %s" % (r.returncode, r.stderr.strip()[:300]))
    return int(r.stdout.strip().splitlines()[-1])


def carry(envfile: str, log: str, cwd: str, argv: list[str]) -> int:
    """Stage two of the WMI path: restore the caller's environment, run argv, wait."""
    import json
    try:
        with open(envfile, encoding="utf-8") as fh:
            env = json.load(fh)
    finally:
        try:
            os.unlink(envfile)             # it holds the caller's environment (keys included)
        except OSError:
            pass
    with open(log, "ab") as out:
        return subprocess.call(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, creationflags=0x08000000)


def _start_any(argv, log, cwd) -> int:
    try:
        return _start(argv, log, cwd, True).pid
    except OSError:
        if os.name != "nt":
            raise
    # ERROR_ACCESS_DENIED: we are inside a job that forbids breakaway (Studio's panes).
    try:
        return _start_wmi(argv, log, cwd)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Last resort: still orphaned from the caller's tree, but inside its job.
        return _start(argv, log, cwd, False).pid


def spawn(argv: list[str], log: str, cwd: str) -> int:
    """Start `argv` fully detached from the caller; return the real process's pid."""
    # The middleman hands back the pid on a pipe; it lives for milliseconds, so it only
    # needs to not flash a console window.
    no_window = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW
    p = subprocess.Popen([sys.executable, "-m", "plexar_agents.detach", log, cwd, "--", *argv],
                         cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=no_window)
    out, _ = p.communicate(timeout=30)
    try:
        return int(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("detach: the middleman did not report a pid (see %s)" % log)


def main(args: list[str]) -> int:
    if args and args[0] == "carry":
        _, envfile, log, cwd, sep, *argv = args
        assert sep == "--" and argv
        return carry(envfile, log, cwd, argv)
    log, cwd, sep, *argv = args
    assert sep == "--" and argv, "usage: detach <log> <cwd> -- <argv...>"
    print(_start_any(argv, log, cwd), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
