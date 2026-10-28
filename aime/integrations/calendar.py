"""The owner's calendar, for A4 meeting booking: Google Calendar (API v3) or Microsoft 365 (Microsoft Graph).

Four operations only: when the owner is busy, create one event, move it, cancel it. Every create is idempotent: the
event carries an id derived from the proposal (Google) or a transaction id (Microsoft), so a retry never books twice.
By default the provider sends no invitation (sendUpdates=none); the prospect hears from the owner's own email, which
a person approves. Access tokens come from aime/integrations/oauth.py (purpose "calendar"). Nothing calls this unless
CALENDAR_PROVIDER is set, MEETING_BOOKING_ENABLED is true and a person clicks Book, Move or Cancel.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx

from aime.timeutil import to_naive_utc


class CalendarError(RuntimeError):
    """A calendar call that failed, in words for the owner (never contains a token)."""


def _iso(at: datetime) -> str:
    return at.replace(tzinfo=None).isoformat(timespec="seconds")


def _utc(text: str) -> datetime:
    """A provider's date-time (with or without an offset; Graph sends 7 decimal places) as naive UTC."""
    text = text.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        text = head + "." + digits[:6] + tail[len(digits):]
    at = datetime.fromisoformat(text)
    return to_naive_utc(at) if at.tzinfo else at


class GoogleCalendar:
    base = "https://www.googleapis.com/calendar/v3"
    name = "google"

    def __init__(self, http: httpx.Client, token: Callable[[], str], send_invites: bool = False):
        self.http, self.token, self.send_updates = http, token, "all" if send_invites else "none"

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = self.http.request(method, self.base + path, headers={"Authorization": f"Bearer {self.token()}"}, **kw)
        except httpx.HTTPError as exc:
            raise CalendarError(f"Google Calendar could not be reached ({type(exc).__name__})") from exc
        return resp

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        resp = self._call("POST", "/freeBusy", json={"timeMin": _iso(start) + "Z", "timeMax": _iso(end) + "Z",
                                                     "items": [{"id": "primary"}]})
        if resp.status_code != 200:
            raise CalendarError(f"Google Calendar free/busy failed ({resp.status_code})")
        cal = (resp.json().get("calendars") or {}).get("primary") or {}
        if cal.get("errors"):
            raise CalendarError("Google Calendar did not return the busy times")
        return [(_utc(b["start"]), _utc(b["end"])) for b in cal.get("busy", [])]

    def create_event(self, key: str, summary: str, description: str, start: datetime, end: datetime,
                     attendee: str) -> str:
        event_id = "aime" + "".join(ch for ch in key.lower() if ch in "0123456789abcdefghijklmnopqrstuv")
        body = {"id": event_id, "summary": summary, "description": description,
                "start": {"dateTime": _iso(start) + "Z"}, "end": {"dateTime": _iso(end) + "Z"},
                "attendees": [{"email": attendee}]}
        resp = self._call("POST", f"/calendars/primary/events?sendUpdates={self.send_updates}", json=body)
        if resp.status_code == 409:  # created by an earlier try: the same booking, not a second one
            return event_id
        if resp.status_code not in (200, 201):
            raise CalendarError(f"Google Calendar did not create the event ({resp.status_code})")
        return resp.json().get("id", event_id)

    def move_event(self, event_id: str, start: datetime, end: datetime) -> None:
        resp = self._call("PATCH", f"/calendars/primary/events/{event_id}?sendUpdates={self.send_updates}",
                          json={"start": {"dateTime": _iso(start) + "Z"}, "end": {"dateTime": _iso(end) + "Z"}})
        if resp.status_code != 200:
            raise CalendarError(f"Google Calendar did not move the event ({resp.status_code})")

    def cancel_event(self, event_id: str) -> None:
        resp = self._call("DELETE", f"/calendars/primary/events/{event_id}?sendUpdates={self.send_updates}")
        if resp.status_code not in (200, 204, 404, 410):  # already gone counts as cancelled
            raise CalendarError(f"Google Calendar did not cancel the event ({resp.status_code})")


class MicrosoftCalendar:
    base = "https://graph.microsoft.com/v1.0"
    name = "microsoft"

    def __init__(self, http: httpx.Client, token: Callable[[], str], send_invites: bool = False):
        self.http, self.token, self.send_invites = http, token, send_invites

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token()}", "Prefer": 'outlook.timezone="UTC"'}
        try:
            return self.http.request(method, self.base + path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise CalendarError(f"Microsoft 365 could not be reached ({type(exc).__name__})") from exc

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        resp = self._call("GET", "/me/calendarView", params={"startDateTime": _iso(start) + "Z", "endDateTime": _iso(end) + "Z",
                                                             "$select": "start,end,showAs", "$top": "200"})
        if resp.status_code != 200:
            raise CalendarError(f"Microsoft 365 calendar view failed ({resp.status_code})")
        return [(_utc(e["start"]["dateTime"]), _utc(e["end"]["dateTime"])) for e in resp.json().get("value", [])
                if e.get("showAs", "busy") not in ("free", "workingElsewhere")]

    def create_event(self, key: str, summary: str, description: str, start: datetime, end: datetime,
                     attendee: str) -> str:
        body = {"subject": summary, "body": {"contentType": "text", "content": description},
                "start": {"dateTime": _iso(start), "timeZone": "UTC"}, "end": {"dateTime": _iso(end), "timeZone": "UTC"},
                "transactionId": key,  # Graph returns the same event for the same transaction id: no double booking
                "attendees": [{"emailAddress": {"address": attendee}, "type": "required"}] if self.send_invites else []}
        resp = self._call("POST", "/me/events", json=body)
        if resp.status_code not in (200, 201):
            raise CalendarError(f"Microsoft 365 did not create the event ({resp.status_code})")
        return resp.json()["id"]

    def move_event(self, event_id: str, start: datetime, end: datetime) -> None:
        resp = self._call("PATCH", f"/me/events/{event_id}", json={"start": {"dateTime": _iso(start), "timeZone": "UTC"},
                                                                   "end": {"dateTime": _iso(end), "timeZone": "UTC"}})
        if resp.status_code != 200:
            raise CalendarError(f"Microsoft 365 did not move the event ({resp.status_code})")

    def cancel_event(self, event_id: str) -> None:
        resp = self._call("DELETE", f"/me/events/{event_id}")
        if resp.status_code not in (200, 204, 404):
            raise CalendarError(f"Microsoft 365 did not cancel the event ({resp.status_code})")


PROVIDERS = {"google": GoogleCalendar, "microsoft": MicrosoftCalendar}


def calendar_for(settings, http: httpx.Client, token: Callable[[], str]):
    """The configured calendar, or None when CALENDAR_PROVIDER is not set."""
    cls = PROVIDERS.get((settings.calendar_provider or "").lower())
    return cls(http, token, send_invites=settings.calendar_send_invites) if cls else None

