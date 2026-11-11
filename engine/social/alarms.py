"""Alarms: to the Company OS when the company has it, or straight to GitHub issues when it does not.

- `dispatch` (the default): a `cos-event` repository_dispatch to the company repo, which the incident loop owns.
  `social-alarm` opens a `cos:alarm` issue keyed `social:<key>` (the engine dedupes by key), and `social-clear` closes
  it. `metric-social-breakout` wakes the loops that listen to `metric*`. Needs COS_EVENT_TOKEN.
- `issues`, for a company without the Company OS (`"alarms": {"mode": "issues", "repo": "owner/name"}` in
  social.json): one issue per alarm labelled `social:alarm`, opened and closed here. It goes to `repo` with
  COS_EVENT_TOKEN (Issues write on that repo), else to the social repo itself with the job's GITHUB_TOKEN, so an alarm
  is never lost while a token is missing. A breakout opens a `social:event` issue.
Only a live publisher sends: a dry run records what it would send. A still-open alarm is re-sent at most once a day.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping

from .http import Request, Transport, UrllibTransport
from .state import GuardState, iso, parse_iso

RESEND = timedelta(hours=24)
API = "https://api.github.com"


class Alarms:
    def __init__(self, state: GuardState, env: Mapping[str, str], *, live: bool, company_repo: str,
                 transport: Transport | None = None, now: datetime, mode: str = "dispatch", issues_repo: str = ""):
        self.state = state
        self.env = env
        self.live = live
        self.repo = company_repo
        self.transport = transport or UrllibTransport()
        self.now = now
        self.mode = mode if mode in ("dispatch", "issues") else "dispatch"
        self.issues_repo = issues_repo
        self.sent: list[dict] = []  # what went out (or would have, in a dry run)
        self.raised: set[str] = set()
        self.errors: list[str] = []  # an alarm that could not be sent never stops the run that raised it

    @classmethod
    def from_config(cls, cfg, state: GuardState, env: Mapping[str, str], *, live: bool, now: datetime,
                    transport: Transport | None = None) -> "Alarms":
        a = (cfg.raw.get("alarms") or {}) if hasattr(cfg, "raw") else {}
        return cls(state, env, live=live, company_repo=cfg.company_repo, transport=transport, now=now,
                   mode=str(a.get("mode", "dispatch")), issues_repo=str(a.get("repo", "")))

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def _fail(self, payload: dict, why: str) -> bool:
        self.errors.append(f"{payload.get('key') or payload['type']}: {why}")
        return False

    def _dispatch(self, payload: dict) -> bool:
        payload = {**payload, "social_repo": self.env.get("GITHUB_REPOSITORY", "")}
        self.sent.append({**payload, "dry_run": not self.live, "mode": self.mode})
        if not self.live:
            return False
        try:
            return self._issue(payload) if self.mode == "issues" else self._event(payload)
        except (OSError, ValueError, KeyError) as e:
            return self._fail(payload, str(e)[:200])

    def _event(self, payload: dict) -> bool:
        token = (self.env.get("COS_EVENT_TOKEN") or "").strip()
        if not token or not self.repo:
            return self._fail(payload, "COS_EVENT_TOKEN or company_repo is missing")
        resp = self.transport.send(Request("POST", f"{API}/repos/{self.repo}/dispatches", headers=self._headers(token),
                                           json={"event_type": "cos-event", "client_payload": payload}))
        return resp.status == 204 or self._fail(payload, f"dispatch to {self.repo} answered {resp.status}")

    def _target(self) -> tuple[str, str]:
        token = (self.env.get("COS_EVENT_TOKEN") or "").strip()
        if token and self.issues_repo:
            return self.issues_repo, token
        return self.env.get("GITHUB_REPOSITORY", ""), (self.env.get("GITHUB_TOKEN") or "").strip()

    def _issue(self, payload: dict) -> bool:
        repo, token = self._target()
        if not repo or not token:
            return self._fail(payload, "no repo or token for alarm issues (COS_EVENT_TOKEN or the job's GITHUB_TOKEN)")
        h = self._headers(token)
        kind = payload["type"]
        label = "social:alarm" if kind in ("social-alarm", "social-clear") else "social:event"
        marker = f"<!-- social-alarm:{payload.get('key') or kind} -->"
        if kind == "social-clear":
            found = self._find(repo, h, label, marker)
            if found is None:
                return True
            self.transport.send(Request("POST", f"{API}/repos/{repo}/issues/{found}/comments", headers=h,
                                        json={"body": f"Cleared: {payload.get('note', '')}"}))
            r = self.transport.send(Request("PATCH", f"{API}/repos/{repo}/issues/{found}", headers=h, json={"state": "closed"}))
            return r.ok or self._fail(payload, f"closing #{found} answered {r.status}")
        if kind == "social-alarm" and self._find(repo, h, label, marker) is not None:
            return True
        title = f"[alarm] {payload.get('title', payload.get('key'))}" if kind == "social-alarm" else f"[social] {kind}: " \
            f"{payload.get('network', '')} {payload.get('format', '')}".strip()
        body = payload.get("body") or "\n".join(f"- {k}: {v}" for k, v in payload.items() if k not in ("type", "social_repo"))
        text = (f"{body}\n\nSeverity: **{payload.get('severity', 'normal')}**. From the social repo "
                f"{payload.get('social_repo', '')}, {iso(self.now)}. It closes itself when the condition clears.\n\n{marker}")
        r = self.transport.send(Request("POST", f"{API}/repos/{repo}/issues", headers=h,
                                        json={"title": title[:200], "body": text, "labels": [label]}))
        return r.status == 201 or self._fail(payload, f"opening an issue in {repo} answered {r.status}")

    def _find(self, repo: str, h: dict, label: str, marker: str) -> int | None:
        r = self.transport.send(Request("GET", f"{API}/repos/{repo}/issues", headers=h,
                                        params={"state": "open", "labels": label, "per_page": "100"}))
        if not r.ok:
            raise ValueError(f"listing issues in {repo} answered {r.status}")
        for issue in r.json() or []:
            if marker in (issue.get("body") or ""):
                return int(issue["number"])
        return None

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
