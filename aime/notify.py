"""Desktop notifications for new alerts: a Windows toast through the built-in PowerShell (no extra software).

Best effort only: alerts are always stored in the database and shown on the dashboard; a toast is an extra
nudge for whoever is logged in. Text is redacted first and never includes secrets. Kept import-light so the
scheduler watchdog can use it without loading the app.
"""

from __future__ import annotations

import os
import subprocess
from html import escape

from aime.redact import redact
from aime.session_info import has_desktop

APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
TITLE = "AI Marketing Employee"


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def toast_script(title: str, message: str) -> str:
    xml = (f'<toast><visual><binding template="ToastGeneric"><text>{escape(title)}</text>'
           f"<text>{escape(message)}</text></binding></visual></toast>")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml({_ps_quote(xml)});"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(APP_ID)})"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )


def toast(title: str, message: str, popen=subprocess.Popen) -> bool:
    """Show a Windows toast without blocking. Returns False where toasts are unavailable, including the background
    session (signed out): nobody can see it there, and the alert is on the dashboard anyway."""
    if os.name != "nt" or not has_desktop():
        return False
    text = (redact(message) or "")[:240]
    try:
        popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
               toast_script(title, text)],
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        return False
    return True


def notify_alert(settings, level: str, message: str, sender=toast) -> bool:
    """Toast for a newly created warning/error alert, if enabled. Never in the test data role."""
    if settings is None or not getattr(settings, "alert_toast", False) or getattr(settings, "data_role", "") == "test":
        return False
    if level not in ("warning", "error"):
        return False
    return sender(f"{TITLE}: {level}", message)


def notify_event(settings, message: str, sender=toast) -> bool:
    """Toast for a sales event the Sales Manager should see now (e.g. an interested reply). Never in tests."""
    if settings is None or not getattr(settings, "alert_toast", False) or getattr(settings, "data_role", "") == "test":
        return False
    return sender(f"{TITLE}: sales", message)
