"""Start the framework when the user logs in. On by default; one command turns it off.

The framework should start when the user logs in, on by default, and be easy to turn off.
and `plexar autostart off` removes it and remembers the choice (a later `up` will not
re-add it). No admin rights anywhere:

    Windows  a hidden .vbs in the user's Startup folder (shell:startup)
    macOS    ~/Library/LaunchAgents/com.plexar.framework.plist
    Linux    ~/.config/autostart/plexar-framework.desktop  (XDG autostart)

Each entry runs `plexar up --no-open` with the same Python that installed it, which is a
no-op when the framework is already running.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

from . import paths

NAME = "plexar-framework"


def _python(windowless: bool) -> str:
    exe = pathlib.Path(sys.executable)
    if windowless and os.name == "nt":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def entry_path() -> pathlib.Path:
    if os.name == "nt":
        base = pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home() / "AppData" / "Roaming")
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / (NAME + ".vbs")
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "LaunchAgents" / "com.plexar.framework.plist"
    base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
    return base / "autostart" / (NAME + ".desktop")


def _content() -> str:
    py = _python(windowless=True)
    if os.name == "nt":
        cmd = '"%s" -m plexar_agents.cli up --no-open' % py
        # Run hidden (0), don't wait (False). Doubled quotes are VBScript's escape.
        return ('\' Plexar-Framework autostart. Remove with: plexar autostart off\r\n'
                'CreateObject("WScript.Shell").Run "%s", 0, False\r\n' % cmd.replace('"', '""'))
    if sys.platform == "darwin":
        return ("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.plexar.framework</string>
  <key>ProgramArguments</key><array>
    <string>%s</string><string>-m</string><string>plexar_agents.cli</string>
    <string>up</string><string>--no-open</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
""" % py)
    return ("[Desktop Entry]\nType=Application\nName=Plexar-Framework\n"
            "Comment=Remove with: plexar autostart off\n"
            "Exec=%s -m plexar_agents.cli up --no-open\nX-GNOME-Autostart-enabled=true\n" % py)


def is_on() -> bool:
    return entry_path().exists()


def user_disabled() -> bool:
    return paths.settings().get("autostart") == "off"


def enable() -> pathlib.Path:
    p = entry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_content(), encoding="utf-8", newline="")
    _remember(None)
    return p


def disable() -> pathlib.Path:
    p = entry_path()
    p.unlink(missing_ok=True)
    _remember("off")
    return p


def ensure_default() -> str:
    """Called by `plexar up`: install unless the user turned it off. Returns what happened."""
    if user_disabled():
        return "off (your choice; `plexar autostart on` to change)"
    if is_on():
        return "on"
    try:
        enable()
        return "on (installed: %s)" % entry_path()
    except OSError as e:                      # never let autostart stop the framework starting
        return "not installed: %s" % e


def _remember(value: str | None) -> None:
    s = paths.settings()
    if value is None:
        s.pop("autostart", None)
    else:
        s["autostart"] = value
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text(json.dumps(s, indent=2), encoding="utf-8")
