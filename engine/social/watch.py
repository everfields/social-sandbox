"""The conditions that raise an alarm without a post failing: the runway, the PC's heartbeat, a post with no outcome.

Run after intake, after metrics and at the end of each publisher run. An alarm clears itself once its condition does.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .alarms import Alarms
from .config import Config
from .intake import runway
from .state import PostLog, parse_iso


def check(root: Path, state_dir: Path, cfg: Config, alarms: Alarms, now: datetime) -> dict:
    log = PostLog(state_dir / "posts.csv")
    report: dict = {"runway": runway(root, cfg, log.used_ids(), now)}
    live = [n for n in cfg.enabled() if cfg.is_live(n.name)]
    for net in live:
        r = report["runway"][net.name]
        key = f"runway-{net.name}"
        if r["days"] < cfg.runway_alarm_days:
            alarms.raise_(key, f"{net.name}: {r['days']} days of posts left",
                          f"{r['posts']} checked posts wait for {net.name}, at {r['per_day']} a day: under "
                          f"{cfg.runway_alarm_days} days. Is the PC rendering? Did intake quarantine the batch? An empty "
                          f"queue goes quiet; it never reposts.")
        else:
            alarms.clear(key, f"{r['days']} days of runway")
    hb_file = root / "pc" / "heartbeat.json"
    hb = json.loads(hb_file.read_text(encoding="utf-8")) if hb_file.is_file() else {}
    at = parse_iso(str(hb.get("at", "")))
    report["heartbeat"] = hb.get("at", "")
    if live:
        age_h = (now - at).total_seconds() / 3600 if at else None
        if age_h is None or age_h > cfg.heartbeat_alarm_hours:
            alarms.raise_("heartbeat", "the PC sent no heartbeat",
                          f"The last batch run on the PC was {hb.get('at') or 'never'} "
                          f"({'never' if age_h is None else f'{age_h:.0f} h ago'}). Publishing carries on from the queue.")
        else:
            alarms.clear("heartbeat", f"heartbeat {hb.get('at')}")
    stuck = log.unresolved()
    report["unresolved"] = [f"{r['network']}/{r['id']}" for r in stuck]
    for r in stuck:
        alarms.raise_(f"unresolved-{r['network']}-{r['id']}", f"{r['network']}: a post has no outcome",
                      f"{r['id']} was being posted at {r['at']} and the run stopped before the answer. Check "
                      f"{r['network']}: if it is there, log it with `social resolve {r['network']} {r['id']} --url <link>`; "
                      f"if not, `--failed`. {r['network']} posts nothing until then.", "high")
    return report
