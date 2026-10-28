"""Which Windows session this process runs in: the signed-in user's desktop, or the background session.

A scheduled task set to "run whether the user is logged on or not" (Password logon) runs in session 0, which
has no desktop: it keeps running while the owner is signed out. The tick records the session in its
heartbeat, so the dashboard can show from evidence (not from settings) whether work ran in the background.
Standard library only: the watchdog and notifications use it without loading the app.
"""

from __future__ import annotations

import os


def session_id(pid: int | None = None) -> int | None:
    """The Windows session of a process (0: the background session), or None where it cannot be read."""
    if os.name != "nt":
        return None
    import ctypes

    sid = ctypes.c_ulong()
    ok = ctypes.WinDLL("kernel32", use_last_error=True).ProcessIdToSessionId(pid or os.getpid(), ctypes.byref(sid))
    return sid.value if ok else None


def session_kind(sid: int | None = None) -> str:
    """'background' (session 0: runs while signed out), 'interactive' (a signed-in desktop) or 'unknown'."""
    sid = session_id() if sid is None else sid
    if sid is None:
        return "unknown"
    return "background" if sid == 0 else "interactive"


def has_desktop() -> bool:
    """False in the background session, where a desktop notification cannot be shown to anyone."""
    return session_kind() != "background"
