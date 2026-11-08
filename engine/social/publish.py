"""One publisher run: for each network with a slot due, the next ready post, its route, a `pending` row, the call,
the result. Ported from AntiSearch's `publisher/run.py`, without the product-specific parts.

Dry run unless the run is live and the network is in SOCIAL_LIVE_NETWORKS: a dry run sends nothing, answers every
call from the adapter's own responder, writes the plan, and keeps its post log apart from the real one.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from .alarms import Alarms
from .base import TEXT, Adapter, Media, PostSpec, PublisherError, Retryable
from .buffer import BUFFER_ROUTES
from .config import Config
from .http import HttpError, RecordingTransport, Request, Transport, UrllibTransport, redact_text
from .instagram import InstagramDirect
from .queue import Item, media_url, ready, release_url
from .schedule import due, posting_day
from .state import FAILED, PENDING, RETRY, GuardState, PostLog, PostRow, iso
from .tiktok import TikTokInbox
from .x import XDirect
from .youtube import YouTubeDirect

LOOKAHEAD = timedelta(minutes=15)  # the cron interval: a slot opening before the next wake is waited for
MAX_RETRIES = 3
LIMIT = re.compile(r"\b429\b|rate[- ]?limit|too many requests|try again later|\bspam\b|temporarily blocked|action blocked", re.I)
RESTRICTED = re.compile(r"suspend|restricted|banned|disabled|account (is )?(locked|blocked)|violat|community guidelines",
                        re.I)
DIRECT = {"instagram": InstagramDirect, "youtube": YouTubeDirect, "x": XDirect}


def adapter_for(network: str, route: str) -> type[Adapter]:
    if route == "buffer":
        return BUFFER_ROUTES[network]
    if route == "inbox" and network == "tiktok":
        return TikTokInbox
    if route == "direct" and network in DIRECT:
        return DIRECT[network]
    raise PublisherError(f"no adapter for {network} via {route}")


@dataclass
class Outcome:
    network: str
    action: str  # skipped | posted | dry-run | retry | failed
    detail: str
    item: str = ""
    url: str = ""


def served(url: str, transport: Transport, *, attempts: int = 6, wait: float = 10.0,
           sleep: Callable[[float], None] = time.sleep) -> bool:
    for i in range(attempts):
        if transport.send(Request("HEAD", url), timeout=30).status == 200:
            return True
        if i < attempts - 1:
            sleep(wait)
    return False


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as r, dest.open("wb") as fh:
        shutil.copyfileobj(r, fh)
    return dest


def spec_of(item: Item) -> PostSpec:
    d = item.data
    return PostSpec(caption=str(d.get("caption") or ""), title=str(d.get("title") or ""),
                    ai_generated=bool(item.ai.get("generated")), ai_realistic=bool(item.ai.get("realistic")),
                    trial=bool(d.get("trial")), language=str(d.get("language") or "en"),
                    tags=tuple(d.get("tags") or ()), options=dict(d.get("options") or {}))


def run(root: Path, state_dir: Path, cfg: Config, env: Mapping[str, str], *, now: datetime, live: bool,
        out: Path, only: str | None = None, transport: Transport | None = None, wait: bool = True,
        sleep: Callable[[float], None] = time.sleep, fetch: Callable[[str, Path], Path] | None = None,
        repo: str = "", checkpoint: Callable[[], None] | None = None) -> list[Outcome]:
    out.mkdir(parents=True, exist_ok=True)
    real_log = PostLog(state_dir / "posts.csv")
    st = GuardState.load(state_dir / "state.json")
    any_live = live and any(cfg.is_live(n.name) for n in cfg.enabled())
    if not any_live:  # a dry run never touches the real log or state
        dry = out / "dry-posts.csv"
        if real_log.path.is_file():
            shutil.copy(real_log.path, dry)
        log = PostLog(dry)
    else:
        log = real_log
    live_transport = transport or UrllibTransport()
    alarms = Alarms(st, env, live=any_live, company_repo=cfg.company_repo, now=now, transport=live_transport)
    repo = repo or env.get("GITHUB_REPOSITORY", "")
    results: list[Outcome] = []
    for net in cfg.enabled():
        if only and net.name != only:
            continue
        verdict = due(cfg, net, now, log, st, lookahead=LOOKAHEAD if wait else timedelta(0))
        if not verdict.due:
            results.append(Outcome(net.name, "skipped", verdict.reason))
            continue
        queue = ready(root, net.name, log.used_ids(), now)
        if not queue:
            results.append(Outcome(net.name, "skipped", "the queue is empty: nothing is reposted to fill a gap"))
            continue
        item = queue[0]
        net_live = live and cfg.is_live(net.name)
        cls = adapter_for(net.name, net.route)
        if net_live and cls.missing(env):
            missing = ", ".join(cls.missing(env))
            alarms.raise_(f"secrets-{net.name}", f"{net.name}: secrets missing ({missing})",
                          f"The publisher is live on {net.name} via {net.route}, but these are not set: {missing}.")
            results.append(Outcome(net.name, "skipped", f"live, but missing {missing}", item.id))
            continue
        if net_live:
            adapter = cls(live_transport, env, sleep=sleep)
        else:
            adapter = cls(None, {**env, **cls.placeholder_env()}, sleep=lambda s: None)
            adapter.transport = RecordingTransport(adapter.dry_run_responder)
        slot = verdict.slot
        if wait and slot and slot.opens > now:
            pause = (slot.opens - now).total_seconds()
            sleep(pause)
            now = now + timedelta(seconds=pause)
        kind = (item.data.get("media") or {}).get("kind", "video")
        probe = (item.data.get("intake") or {}).get("probe") or {}
        url = media_url(cfg.public_base_url, item.file) if cfg.public_base_url else ""
        local = out / "media" / item.file
        row = PostRow(id=item.id, network=net.name, status=PENDING, at=iso(now), route=net.route,
                      slot=slot.key if slot else "", format=str(item.data.get("format", "")),
                      batch=str(item.data.get("batch", "")), media=item.file,
                      sha256=str((item.data.get("media") or {}).get("sha256", "")), caption=str(item.data.get("caption") or ""),
                      ai=("ai-realistic" if item.ai.get("realistic") else "ai") if item.ai.get("generated") else "",
                      trial="yes" if item.data.get("trial") else "")
        try:
            if net_live:
                if adapter.needs_public_url and not served(url, live_transport, sleep=sleep):
                    raise Retryable(f"media not served by Pages yet: {url}")
                if not adapter.needs_public_url and kind != TEXT and not local.is_file():
                    (fetch or (lambda f, d: download(release_url(repo, f), d)))(item.file, local)
            elif not local.is_file():
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(b"\0" * int(probe.get("bytes") or 1024))  # stand-in bytes for the dry-run upload
            media = Media(local, kind=kind, mime="video/mp4" if kind == "video" else "image/jpeg",
                          width=int(probe.get("width") or 1080), height=int(probe.get("height") or 1920),
                          duration_s=float(probe.get("duration_s") or 0), public_url=url,
                          cover_s=(item.data.get("media") or {}).get("cover_s"))
            log.append(row)  # before the call: a crash leaves a pending row that blocks a double post
            if net_live and checkpoint:
                checkpoint()  # and it is already on the state branch, so a lost runner cannot double-post either
            result = adapter.post(media, spec_of(item))
        except Retryable as e:
            log.append(PostRow(**{**row.__dict__, "status": RETRY, "at": iso(now), "detail": redact_text(str(e))}))
            if log.retries(item.id, net.name) >= MAX_RETRIES:
                alarms.raise_(f"media-{net.name}", f"{net.name}: media host keeps failing",
                              f"{item.id} was retried {MAX_RETRIES} times: {redact_text(str(e))}")
            results.append(Outcome(net.name, "retry", redact_text(str(e)), item.id))
            continue
        except (PublisherError, HttpError, OSError, ValueError, KeyError) as e:
            detail = redact_text(str(e))
            if row.at and log.latest().get((item.id, net.name), {}).get("status") != PENDING:
                log.append(row)  # the failure came before the pending row: record the attempt for the slot
            log.append(PostRow(**{**row.__dict__, "status": FAILED, "at": iso(now), "detail": detail[:500]}))
            results.append(Outcome(net.name, "failed", detail, item.id))
            if not net_live:
                continue
            if RESTRICTED.search(detail):
                st.stopped[net.name] = f"{iso(now)}: {detail[:200]}"
                alarms.raise_(f"restricted-{net.name}", f"{net.name}: the account looks restricted",
                              f"The network answered: {detail[:400]}\n\nThe publisher stopped {net.name}. Only a person "
                              f"resumes it (`social resume {net.name}`). Never a second account.", "high")
            elif LIMIT.search(detail):
                if st.trip(net.name, now, f"limit error: {detail[:200]}", cfg.brake_hours):
                    alarms.raise_(f"brake-{net.name}", f"{net.name}: the brake tripped",
                                  f"A rate-limit or spam answer: {detail[:400]}\n\nPosts are halved for {cfg.brake_hours} h.")
            elif "rejected it" in detail:
                st.failures[net.name] = st.failures.get(net.name, 0) + 1
                alarms.raise_(f"rejected-{net.name}", f"{net.name}: the network rejected a post",
                              f"{detail[:400]}\n\nA rejection with no reason was the first sign of AntiSearch's X suspension "
                              f"(2026-10-09). Check the account by hand.", "high")
            else:
                st.failures[net.name] = st.failures.get(net.name, 0) + 1
                if st.failures[net.name] >= 2:
                    alarms.raise_(f"failing-{net.name}", f"{net.name}: posts are failing",
                                  f"{st.failures[net.name]} failures in a row. The last: {detail[:400]}")
            continue
        finally:
            plan = getattr(adapter.transport, "plan", None)
            if plan:
                (out / f"plan-{net.name}.json").write_text(json.dumps(plan(), indent=2), encoding="utf-8")
        log.append(PostRow(**{**row.__dict__, "status": result.state, "at": iso(now), "remote_id": result.remote_id,
                              "url": result.url, "detail": result.note[:300]}))
        for w in adapter.warnings:
            alarms.raise_(f"warning-{net.name}", f"{net.name}: {w}", w)
        if net_live:
            st.first_post.setdefault(net.name, posting_day(cfg, now).isoformat())
            st.failures[net.name] = 0
            alarms.clear(f"failing-{net.name}", "a post went out")
            alarms.clear(f"rejected-{net.name}", "a post went out")
            st.seen[result.remote_id] = {"network": net.name, "item": item.id, "first_seen": iso(now)}
        results.append(Outcome(net.name, "posted" if net_live else "dry-run", result.state, item.id, result.url))
    if any_live:
        st.save()
    (out / "alarms.json").write_text(json.dumps({"sent": alarms.sent, "errors": alarms.errors}, indent=2), encoding="utf-8")
    (out / "run.json").write_text(json.dumps([r.__dict__ for r in results], indent=2), encoding="utf-8")
    return results
