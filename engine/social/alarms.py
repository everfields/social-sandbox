"""Alarms into the Company OS: a `cos-event` repository_dispatch to the company repo, which the incident loop owns.

`social-alarm` opens a `cos:alarm` issue keyed `social:<key>` (the engine dedupes by key) and `social-clear` closes
it. `metric-social-breakout` wakes the loops that listen to `metric*` when a post passes 5x the floor. Only a live
publisher sends: a dry run records what it would send. A still-open alarm is re-sent at most once a day.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping

from .http import Request, Transport, UrllibTransport
from .state import GuardState, iso, parse_iso

RESEND = timedelta(hours=24)


class Alarms:
    def __init__(self, state: GuardState, env: Mapping[str, str], *, live: bool, company_repo: str,
                 transport: Transport | None = None, now: datetime):
        self.state = state
        self.env = env
        self.live = live
        self.repo = company_repo
        self.transport = transport or UrllibTransport()
        self.now = now
        self.sent: list[dict] = []  # what went out (or would have, in a dry run)
        self.raised: set[str] = set()
        self.errors: list[str] = []  # an alarm that could not be sent never stops the run that raised it

    def _dispatch(self, payload: dict) -> bool:
        payload = {**payload, "social_repo": self.env.get("GITHUB_REPOSITORY", "")}
        self.sent.append({**payload, "dry_run": not self.live})
        if not self.live:
            return False
        token = (self.env.get("COS_EVENT_TOKEN") or "").strip()
        if not token or not self.repo:
            self.errors.append(f"{payload.get('key') or payload['type']}: COS_EVENT_TOKEN or company_repo is missing")
            return False
        try:
            resp = self.transport.send(Request("POST", f"https://api.github.com/repos/{self.repo}/dispatches", headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}, json={"event_type": "cos-event", "client_payload": payload}))
        except OSError as e:
            self.errors.append(f"{payload.get('key') or payload['type']}: {e}")
            return False
        if resp.status != 204:
            self.errors.append(f"{payload.get('key') or payload['type']}: dispatch to {self.repo} answered {resp.status}")
            return False
        return True

    def raise_(self, key: str, title: str, body: str, severity: str = "normal") -> None:
        self.raised.add(key)
        prev = self.state.alarms.get(key) or {}
        last = parse_iso(prev.get("sent_at", ""))
        if prev and not prev.get("cleared") and last and self.now - last < RESEND:
            return
        if self._dispatch({"type": "social-alarm", "key": key, "title": title, "body": body, "severity": severity}):
            self.state.alarms[key] = {"title": title, "sent_at": iso(self.now), "cleared": False}

    def clear(self, key: str, note: str) -> None:
        prev = self.state.alarms.get(key)
        if not prev or prev.get("cleared"):
            return
        if self._dispatch({"type": "social-clear", "key": key, "note": note}):
            prev["cleared"] = True
            prev["cleared_at"] = iso(self.now)

    def clear_others(self, prefix: str, note: str) -> None:
        """Clear every open alarm under `prefix` that this run did not raise again."""
        for key in [k for k, v in self.state.alarms.items() if k.startswith(prefix) and not v.get("cleared")]:
            if key not in self.raised:
                self.clear(key, note)

    def event(self, type_: str, payload: dict) -> None:
        self._dispatch({"type": type_, **payload})
