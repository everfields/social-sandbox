"""What the publisher remembers, on the `social-state` branch: the post log and the guard state.

The post log is append-only CSV with a `pending` row written before every API call, the only memory the publisher
trusts (ported from AntiSearch's `guard.PostLog`). The guard state (ported from `antiban.AntibanState`) holds each
network's first post (the warm-up clock), the brake, stopped networks and the alarms already sent.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

PENDING = "pending"
FAILED = "failed"
RETRY = "retry"  # transient (the media host was slow): the item stays in the queue
PUBLISHED = "published"
DRAFTED = "drafted"
PROCESSING = "processing"
HANDED = "handed"  # Buffer holds it; metrics reconcile it with Buffer's final status and the network's link
REMOVED = "removed"  # taken down by the network or by us: never reposted
DONE = (PUBLISHED, DRAFTED, PROCESSING, HANDED, REMOVED)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class PostRow:
    id: str
    network: str
    status: str
    at: str = ""
    route: str = ""
    slot: str = ""  # posting day|network|HH:MM: one post per slot
    format: str = ""
    batch: str = ""
    media: str = ""  # file name
    sha256: str = ""
    caption: str = ""
    ai: str = ""  # "", "ai", "ai-realistic"
    trial: str = ""
    remote_id: str = ""
    url: str = ""
    detail: str = ""


COLUMNS = [f.name for f in fields(PostRow)]


class PostLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def rows(self) -> list[dict[str, str]]:
        if not self.path.is_file():
            return []
        with self.path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def append(self, row: PostRow) -> None:
        if not row.at:
            row.at = iso(now_utc())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.is_file()
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(asdict(row))
            fh.flush()
            os.fsync(fh.fileno())

    def latest(self) -> dict[tuple[str, str], dict[str, str]]:
        """(item id, network) -> its last row."""
        out: dict[tuple[str, str], dict[str, str]] = {}
        for r in self.rows():
            out[(r["id"], r["network"])] = r
        return out

    def attempts(self, network: str) -> list[dict[str, str]]:
        """Rows whose post went, or may have gone, out on `network`: the pending row of every attempt that did not
        end failed or retry. A pending row without an outcome counts: it may have posted."""
        final = self.latest()
        return [r for r in self.rows() if r["network"] == network and r["status"] == PENDING
                and final.get((r["id"], network), {}).get("status") not in (FAILED, RETRY)]

    def sent_times(self, network: str) -> list[datetime]:
        return sorted(t for t in (parse_iso(r["at"]) for r in self.attempts(network)) if t)

    def filled_slots(self, network: str) -> set[str]:
        """Slots taken: by any attempt that may have gone out, and by a non-retryable failure (one try per slot, so a
        broken route never burns the runway)."""
        final = self.latest()
        out = set()
        for r in self.rows():
            if r["network"] != network or r["status"] != PENDING or not r["slot"]:
                continue
            if final.get((r["id"], network), {}).get("status") != RETRY:
                out.add(r["slot"])
        return out

    def used_ids(self) -> set[str]:
        """Queue items that went (or may have gone) out, or failed for good: never posted again."""
        return {k[0] for k, r in self.latest().items() if r["status"] != RETRY}

    def unresolved(self) -> list[dict[str, str]]:
        """Pending rows with no outcome: a crash mid-post. A person checks the network before anything retries it."""
        return [r for r in self.latest().values() if r["status"] == PENDING]

    def retries(self, item_id: str, network: str) -> int:
        return sum(1 for r in self.rows() if r["id"] == item_id and r["network"] == network and r["status"] == RETRY)


@dataclass
class GuardState:
    path: Path
    first_post: dict[str, str] = field(default_factory=dict)
    brake_until: dict[str, str] = field(default_factory=dict)
    brake_reason: dict[str, str] = field(default_factory=dict)
    stopped: dict[str, str] = field(default_factory=dict)  # network -> why; only a person resumes it
    failures: dict[str, int] = field(default_factory=dict)  # consecutive non-retryable failures
    alarms: dict[str, dict] = field(default_factory=dict)  # key -> {title, sent_at, cleared}
    seen: dict[str, dict] = field(default_factory=dict)  # remote id -> {network, first_seen}; for removal checks

    @classmethod
    def load(cls, path: Path) -> "GuardState":
        st = cls(Path(path))
        if st.path.is_file():
            raw = json.loads(st.path.read_text(encoding="utf-8"))
            for f in fields(cls):
                if f.name != "path" and f.name in raw:
                    setattr(st, f.name, raw[f.name])
        return st

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "path"}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def days_since_first(self, network: str, day) -> int:
        raw = self.first_post.get(network)
        if not raw:
            return 0
        from datetime import date

        return max(0, (day - date.fromisoformat(raw)).days)

    def braked(self, network: str, now: datetime) -> bool:
        until = parse_iso(self.brake_until.get(network, ""))
        return bool(until and now < until)

    def trip(self, network: str, now: datetime, reason: str, hours: int) -> bool:
        """Halve the network's posts for `hours`. Returns False when the same reason already tripped it."""
        if self.brake_reason.get(network) == reason[:300] and self.braked(network, now):
            return False
        self.brake_until[network] = iso(now + timedelta(hours=hours))
        self.brake_reason[network] = reason[:300]
        return True
