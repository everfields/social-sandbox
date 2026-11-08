"""The Pages runway: the media of the posts not yet published, served at plain public URLs for Buffer and Meta.

Media stay out of git. `site` builds a folder with every checked, unposted item's file (from the release assets) and
the files posted in the last 48 hours (a network may fetch late), and `social-pages.yml` deploys it from an artifact.
A Pages site is capped at 1 GB, so the runway is capped below that.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .queue import items
from .state import PostLog, parse_iso

KEEP_AFTER_POST = timedelta(hours=48)
SITE_CAP = 900 * 1024 * 1024


def build(root: Path, state_dir: Path, out: Path, fetch: Callable[[str], Path], now: datetime) -> dict:
    log = PostLog(state_dir / "posts.csv")
    final = log.latest()
    posted_at = {}
    for r in log.rows():
        if r["status"] == "pending":
            posted_at[(r["id"], r["network"])] = parse_iso(r["at"])
    wanted: dict[str, None] = {}
    for it in items(root):
        if not it.checked:
            continue
        last = final.get((it.id, it.network))
        when = posted_at.get((it.id, it.network))
        if last is None or last["status"] == "retry" or (when and now - when < KEEP_AFTER_POST):
            wanted[it.file] = None
    if out.exists():
        shutil.rmtree(out)
    (out / "m").mkdir(parents=True)
    size, kept, dropped = 0, [], []
    for f in wanted:
        src = fetch(f)
        if size + src.stat().st_size > SITE_CAP:
            dropped.append(f)
            continue
        shutil.copy(src, out / "m" / f)
        size += src.stat().st_size
        kept.append(f)
    (out / "index.html").write_text('<!doctype html><meta name="robots" content="noindex"><title>media</title>\n',
                                    encoding="utf-8")
    return {"files": len(kept), "mb": round(size / 1048576, 1), "over_cap": dropped}
