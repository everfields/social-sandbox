"""When a network may post: slots in the audience's time zone, the warm-up, the brake, the jitter and the caps.

Ported from AntiSearch's `guard.py`, `jitter.py` and `antiban.py`, generalised from its hard-coded New York and Madrid
slots to any company's `social.json`:
- a posting day starts when the quiet hours end (08:00 local by default), so a 00:07 slot belongs to the evening before;
- each slot opens at a seeded offset of 0-40 minutes, never on :00 or :30, and stays open until the next slot or for
  3 hours, whichever is later (the catch-up for dropped schedules), but never into the quiet hours;
- warm-up: 1 post a day on a network's first day, rising to its full cadence over 14 days;
- brake: half the posts while it holds (72 hours after a spam or rate-limit error, or when reach halves);
- caps: at most `max_24h` in any rolling 24 hours, `min_gap` apart, one post per slot, one post per network per run.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import DEFAULT_QUIET, Config, Network, hhmm, in_quiet
from .state import GuardState, PostLog

GRACE = timedelta(hours=3)
AVOID_MINUTES = (0, 30)


def quiet(cfg: Config) -> tuple[time, time]:
    q = cfg.raw.get("quiet", DEFAULT_QUIET)
    return hhmm(q[0], "quiet"), hhmm(q[1], "quiet")


def posting_day(cfg: Config, moment: datetime) -> date:
    start = quiet(cfg)[1]
    local = moment.astimezone(cfg.tz)
    return (local - timedelta(hours=start.hour, minutes=start.minute)).date()


def slot_time(cfg: Config, day: date, t: time) -> datetime:
    start = quiet(cfg)[1]
    return datetime.combine(day + timedelta(days=0 if t >= start else 1), t, tzinfo=cfg.tz)


def ramp(target: int, days_since_first: int, days: int, start: int) -> int:
    """Posts allowed on a posting day: `start` on day 0, the full `target` after `days` days."""
    if target <= 0:
        return 0
    start = min(max(1, start), target)
    if days_since_first >= days or target <= start:
        return target
    return start + (target - start) * max(0, days_since_first) // days


def spread(slots: list[time], n: int, cfg: Config) -> list[time]:
    """`n` of the day's slots, spread across it (one slot: the middle one)."""
    ordered = sorted(slots, key=lambda t: slot_time(cfg, date(2026, 1, 5), t))
    if n >= len(ordered):
        return ordered
    if n <= 0:
        return []
    if n == 1:
        return [ordered[len(ordered) // 2]]
    return [ordered[round(i * (len(ordered) - 1) / (n - 1))] for i in range(n)]


def offset(cfg: Config, day: date, network: str, t: time, taken: set[datetime]) -> timedelta:
    if cfg.jitter_minutes <= 0:
        return timedelta(0)
    seed = int.from_bytes(hashlib.sha256(f"{day.isoformat()}|{network}|{t:%H:%M}".encode()).digest()[:8], "big")
    rng = random.Random(seed)
    nominal = slot_time(cfg, day, t)
    for off in [rng.randint(0, cfg.jitter_minutes) for _ in range(200)] + list(range(cfg.jitter_minutes + 1)):
        when = nominal + timedelta(minutes=off)
        if when.minute not in AVOID_MINUTES and when not in taken:
            return timedelta(minutes=off)
    return timedelta(0)


@dataclass(frozen=True)
class Slot:
    network: str
    day: date
    nominal: datetime
    opens: datetime
    closes: datetime

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}|{self.network}|{self.nominal:%H:%M}"


def day_slots(cfg: Config, net: Network, day: date, state: GuardState, now: datetime) -> list[Slot]:
    n = ramp(len(net.slots), state.days_since_first(net.name, day), cfg.warmup_days, cfg.warmup_start)
    if state.braked(net.name, now):
        n //= 2
    chosen = spread(net.slots, n, cfg)
    taken: set[datetime] = set()
    for other in sorted(cfg.networks):  # distinct minutes across networks: drawn in a fixed order
        if other == net.name:
            break
        o = cfg.networks[other]
        for t in o.slots:
            taken.add(slot_time(cfg, day, t) + offset(cfg, day, other, t, taken))
    q0 = quiet(cfg)[0]
    nominals = sorted(slot_time(cfg, day, t) for t in net.slots)
    out = []
    for t in sorted(chosen, key=lambda x: slot_time(cfg, day, x)):
        nominal = slot_time(cfg, day, t)
        opens = nominal + offset(cfg, day, net.name, t, taken)
        later = [x for x in nominals if x > nominal]
        closes = max(nominal + GRACE, later[0]) if later else nominal + GRACE
        night = datetime.combine(nominal.date() if nominal.time() < q0 else nominal.date() + timedelta(days=1), q0,
                                 tzinfo=cfg.tz)
        if quiet(cfg)[0] < quiet(cfg)[1]:
            closes = min(closes, night)
        out.append(Slot(net.name, day, nominal, opens, closes))
    return out


@dataclass(frozen=True)
class Verdict:
    due: bool
    reason: str
    slot: Slot | None = None


def due(cfg: Config, net: Network, now: datetime, log: PostLog, state: GuardState, *,
        lookahead: timedelta = timedelta(0)) -> Verdict:
    """Whether `net` may post now (or within `lookahead`, the time to the next wake), and into which slot."""
    if not net.enabled:
        return Verdict(False, "disabled")
    if net.name in state.stopped:
        return Verdict(False, f"stopped: {state.stopped[net.name]}")
    if in_quiet(now.astimezone(cfg.tz).time().replace(tzinfo=None), quiet(cfg)):
        return Verdict(False, "quiet hours")
    if log.unresolved() and any(r["network"] == net.name for r in log.unresolved()):
        return Verdict(False, "a post without an outcome waits for a person to check it")
    day = posting_day(cfg, now)
    filled = log.filled_slots(net.name)
    open_ = [s for s in day_slots(cfg, net, day, state, now)
             if s.opens <= now + lookahead and now < s.closes and s.key not in filled]
    if not open_:
        return Verdict(False, "no open slot")
    slot = open_[-1]  # an earlier one still open was missed; the gap rule closes it
    sent = log.sent_times(net.name)
    recent = [t for t in sent if now - t < timedelta(hours=24)]
    if len(recent) >= net.max_24h:
        return Verdict(False, f"{len(recent)} posts in the last 24 h (limit {net.max_24h})")
    if sent and now - sent[-1] < net.min_gap:
        return Verdict(False, f"last post {int((now - sent[-1]).total_seconds() // 60)} min ago (gap {net.min_gap})")
    return Verdict(True, "due", slot)
