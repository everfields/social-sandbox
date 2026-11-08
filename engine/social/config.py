"""The social repo's settings: `social.json` (the company's choices) under the engine's hard limits.

The limits are the method's (knowledge/social-native.md, "Keep the account healthy") and AntiSearch's evidence:
TikTok at most 5 in any 24 hours and 2 hours apart (every post after 6 in 24 hours got 0 views); Instagram and
YouTube 1-3 a day; X at most 2. Nothing overnight in the audience's time zone (overnight slots got 0 views). A
company may go below a limit, never above it. Repository variables override per run: SOCIAL_CRON,
SOCIAL_LIVE_NETWORKS, SOCIAL_ROUTE_<NETWORK>, SOCIAL_SLOTS_<NETWORK>, SOCIAL_PUBLIC_BASE_URL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import NETWORKS

HARD = {
    "tiktok": {"max_24h": 5, "min_gap_minutes": 120},
    "instagram": {"max_24h": 3, "min_gap_minutes": 120},
    "youtube": {"max_24h": 3, "min_gap_minutes": 120},
    "x": {"max_24h": 2, "min_gap_minutes": 240},
}
ROUTES = {
    "tiktok": ("buffer", "inbox"),  # TikTok's own API posts privately until an audit it refuses for own accounts
    "instagram": ("direct", "buffer"),
    "youtube": ("direct", "buffer"),
    "x": ("buffer", "direct"),  # direct only after a spend gate
}
DEFAULT_QUIET = ("01:00", "08:00")  # local to the audience: no slot inside it
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
CRON_MODES = ("off", "dry-run", "live")


class ConfigError(ValueError):
    pass


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise ConfigError(f"unknown time zone {name!r} (on Windows: pip install tzdata)") from e


def hhmm(s: str, where: str) -> time:
    m = HHMM.match(str(s).strip())
    if not m:
        raise ConfigError(f"{where}: {s!r} is not HH:MM (24 h)")
    return time(int(m.group(1)), int(m.group(2)))


@dataclass
class Network:
    name: str
    enabled: bool
    route: str
    slots: list[time]
    max_24h: int
    min_gap: timedelta


@dataclass
class Config:
    company: str
    company_repo: str
    tz: ZoneInfo
    networks: dict[str, Network]
    public_base_url: str
    warmup_days: int = 14
    warmup_start: int = 1
    jitter_minutes: int = 40
    brake_hours: int = 72
    reach_floor: int = 100
    reach_ratio: float = 0.5
    runway_alarm_days: float = 5
    runway_target_days: float = 10
    heartbeat_alarm_hours: float = 36
    intake: dict = field(default_factory=dict)
    cron: str = "off"
    live: frozenset[str] = frozenset()
    raw: dict = field(default_factory=dict)

    def enabled(self) -> list[Network]:
        return [n for n in self.networks.values() if n.enabled]

    def is_live(self, network: str) -> bool:
        return self.cron == "live" and network in self.live

    def posts_per_day(self) -> float:
        return float(sum(len(n.slots) for n in self.enabled())) or 1.0


INTAKE_DEFAULTS = {
    "aspect": [9, 16],
    "min_height": 1280,
    "min_s": 3,
    "max_s": 90,
    "max_mb": 60,  # TikTok's single-chunk upload stops at 64 MB; these clips are 1-20 MB
    "video_codecs": ["h264", "hevc"],
    "audio_codecs": ["aac"],
    "max_hashtags": 3,
    "phash_distance": 10,
    "caption_max": 2200,
}


def load(root: Path, env: Mapping[str, str]) -> Config:
    path = root / "social.json"
    if not path.is_file():
        raise ConfigError(f"missing {path}")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    tz = zone(raw.get("timezone") or "America/New_York")
    quiet = tuple(hhmm(x, "quiet") for x in raw.get("quiet", DEFAULT_QUIET))
    nets: dict[str, Network] = {}
    for name, n in (raw.get("networks") or {}).items():
        if name not in NETWORKS:
            raise ConfigError(f"networks.{name}: unknown network (one of {', '.join(NETWORKS)})")
        hard = HARD[name]
        route = (env.get(f"SOCIAL_ROUTE_{name.upper()}") or n.get("route") or ROUTES[name][0]).strip().lower()
        if route not in ROUTES[name]:
            raise ConfigError(f"{name}: route {route!r} is not one of {', '.join(ROUTES[name])}")
        override = (env.get(f"SOCIAL_SLOTS_{name.upper()}") or "").strip()
        items = [s for s in re.split(r"[,\s]+", override) if s] if override else list(n.get("slots") or [])
        if override.lower() in ("off", "none", "0"):
            items = []
        slots = sorted({hhmm(s, f"{name}.slots") for s in items})
        max_24h = int(n.get("max_24h", hard["max_24h"]))
        gap = int(n.get("min_gap_minutes", hard["min_gap_minutes"]))
        if max_24h > hard["max_24h"]:
            raise ConfigError(f"{name}: max_24h {max_24h} is above the engine's limit of {hard['max_24h']}")
        if gap < hard["min_gap_minutes"]:
            raise ConfigError(f"{name}: min_gap_minutes {gap} is below the engine's limit of {hard['min_gap_minutes']}")
        if len(slots) > max_24h:
            raise ConfigError(f"{name}: {len(slots)} slots, the limit is {max_24h} a day")
        for s in slots:
            if s.minute in (0, 30):
                raise ConfigError(f"{name}: slot {s:%H:%M} sits on :00 or :30; move it off (e.g. :07)")
            if in_quiet(s, quiet):
                raise ConfigError(f"{name}: slot {s:%H:%M} is overnight ({quiet[0]:%H:%M}-{quiet[1]:%H:%M} local)")
        mins = [s.hour * 60 + s.minute for s in slots]
        for a, b in zip(mins, mins[1:] + ([mins[0] + 1440] if len(mins) > 1 else [])):
            if b - a < gap:
                raise ConfigError(f"{name}: slots {a // 60 % 24:02d}:{a % 60:02d} and {b // 60 % 24:02d}:{b % 60:02d} "
                                  f"are closer than {gap} minutes")
        nets[name] = Network(name, bool(n.get("enabled", True)), route, slots, max_24h, timedelta(minutes=gap))
    cron = (env.get("SOCIAL_CRON") or raw.get("cron") or "off").strip().lower()
    if cron not in CRON_MODES:
        raise ConfigError(f"SOCIAL_CRON={cron!r}: one of {', '.join(CRON_MODES)}")
    live = frozenset(x for x in re.split(r"[,\s|]+", (env.get("SOCIAL_LIVE_NETWORKS") or "").lower()) if x)
    w, b, r = raw.get("warmup") or {}, raw.get("brake") or {}, raw.get("runway") or {}
    return Config(
        company=str(raw.get("company") or root.name),
        company_repo=str(raw.get("company_repo") or ""),
        tz=tz,
        networks=nets,
        public_base_url=(env.get("SOCIAL_PUBLIC_BASE_URL") or raw.get("public_base_url") or "").rstrip("/"),
        warmup_days=max(14, int(w.get("days", 14))),  # never shorter: AntiSearch's warm-up was switched off and it got 0 views
        warmup_start=min(2, max(1, int(w.get("start", 1)))),
        jitter_minutes=min(40, max(0, int(raw.get("jitter_minutes", 40)))),
        brake_hours=int(b.get("hours", 72)),
        reach_floor=int(b.get("reach_floor", 100)),
        reach_ratio=float(b.get("reach_ratio", 0.5)),
        runway_alarm_days=float(r.get("alarm_days", 5)),
        runway_target_days=float(r.get("target_days", 10)),
        heartbeat_alarm_hours=float(raw.get("heartbeat_alarm_hours", 36)),
        intake={**INTAKE_DEFAULTS, **(raw.get("intake") or {})},
        cron=cron,
        live=live,
        raw=raw,
    )


def in_quiet(t: time, quiet: tuple[time, time]) -> bool:
    a, b = quiet
    return (a <= t < b) if a < b else (t >= a or t < b)
