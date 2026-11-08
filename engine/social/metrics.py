"""The numbers: per post and per account, through each network's own API where the route is direct.

Ported from AntiSearch's `metrics.py` and moved to first-party numbers:
- YouTube: the Data API (views, likes, comments, whether the video still exists) and YouTube Analytics (average view
  duration, shares, subscribers gained per video);
- Instagram: the media insights of the Instagram Login API (views, reach, likes, comments, shares, saves) and the
  account's followers;
- TikTok, and X while it is on Buffer: Buffer's post metrics about once a day, and TikTok's public pages.
A missing number is blank, never 0. A live network that returns nothing turns the run red. Snapshots are append-only
CSVs under `metrics/`; `summary.md` and `scoreboard.json` (what the cos-social loop reads) are rebuilt each run.
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .alarms import Alarms
from .base import Adapter
from .buffer import API as BUFFER_API, ORGS as BUFFER_ORGS, STATUS as BUFFER_STATUS
from .config import Config
from .http import HttpError, Request, Transport, UrllibTransport, redact_text
from .state import DONE, FAILED, HANDED, PENDING, PUBLISHED, REMOVED, GuardState, PostLog, PostRow, iso, parse_iso
from .youtube import API as YT_API, access_token

POST_FIELDS = ("ts_utc", "network", "item", "remote_id", "url", "format", "posted_at", "views", "likes", "comments",
               "shares", "saves", "avg_view_s", "follows", "source")
ACCOUNT_FIELDS = ("ts_utc", "network", "followers", "source", "note")
BUFFER_EVERY = timedelta(hours=20)
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BUFFER_POSTS = """query Posts($input: PostsInput!, $first: Int, $after: String) {
  posts(input: $input, first: $first, after: $after) {
    edges { node { id channelService externalLink sentAt metricsUpdatedAt metrics { type value } } }
    pageInfo { hasNextPage endCursor }
  }
}"""
BUFFER_SERVICE = {"tiktok": "tiktok", "instagram": "instagram", "twitter": "x", "youtube": "youtube"}


def _int(v) -> int | None:
    try:
        return None if v is None or v == "" else int(float(v))
    except (TypeError, ValueError):
        return None


@dataclass
class PostNumbers:
    network: str
    remote_id: str
    url: str = ""
    item: str = ""
    format: str = ""
    posted_at: str = ""
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    avg_view_s: float | None = None
    follows: int | None = None
    source: str = ""


@dataclass
class Collection:
    posts: list[PostNumbers] = field(default_factory=list)
    accounts: list[dict] = field(default_factory=list)
    removed: list[tuple[str, str, str]] = field(default_factory=list)  # (network, remote id, why)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class _Client(Adapter):
    """A bare adapter used for its HTTP seam and env; it never posts."""

    name = "metrics"

    def _post(self, media, spec):  # pragma: no cover - never posts
        raise NotImplementedError

    def dry_run_responder(self, req):  # pragma: no cover
        raise NotImplementedError


def posted(log: PostLog) -> dict[str, list[dict]]:
    """network -> the last row of each post that went out, with its remote id."""
    out: dict[str, list[dict]] = {}
    for (_, net), r in log.latest().items():
        if r["status"] in DONE and r["status"] != REMOVED and r.get("remote_id"):
            pending = next((p for p in log.rows() if p["id"] == r["id"] and p["network"] == net and p["status"] == PENDING), r)
            out.setdefault(net, []).append({**r, "posted_at": pending["at"]})
    return out


def collect_youtube(c: _Client, rows: list[dict], now: datetime, col: Collection) -> None:
    token = access_token(c)
    auth = {"Authorization": f"Bearer {token}"}
    by_id = {r["remote_id"]: r for r in rows}
    ids = list(by_id)
    found: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        items = c.send(Request("GET", f"{YT_API}/videos", headers=auth, params={
            "part": "statistics,status", "id": ",".join(ids[i:i + 50])})).json().get("items") or []
        found.update({it["id"]: it for it in items})
    analytics: dict[str, dict] = {}
    if ids:
        try:
            first = min((parse_iso(r["posted_at"]) or now) for r in rows).date().isoformat()
            rep = c.send(Request("GET", "https://youtubeanalytics.googleapis.com/v2/reports", headers=auth, params={
                "ids": "channel==MINE", "startDate": first, "endDate": now.date().isoformat(),
                "metrics": "views,averageViewDuration,shares,subscribersGained", "dimensions": "video",
                "filters": "video==" + ",".join(ids[:200]), "maxResults": "200"})).json()
            heads = [h["name"] for h in rep.get("columnHeaders") or []]
            for row in rep.get("rows") or []:
                analytics[row[0]] = dict(zip(heads, row))
        except HttpError as e:
            col.notes.append(f"youtube analytics: {redact_text(str(e))[:200]}")
    for vid, r in by_id.items():
        it = found.get(vid)
        if not it or (it.get("status") or {}).get("uploadStatus") in ("rejected", "deleted", "failed"):
            col.removed.append(("youtube", vid, (it or {}).get("status", {}).get("rejectionReason", "not found")))
            continue
        s, a = it.get("statistics") or {}, analytics.get(vid) or {}
        col.posts.append(PostNumbers("youtube", vid, r["url"], r["id"], r["format"], r["posted_at"],
                                     views=_int(s.get("viewCount")), likes=_int(s.get("likeCount")),
                                     comments=_int(s.get("commentCount")), shares=_int(a.get("shares")),
                                     avg_view_s=a.get("averageViewDuration"), follows=_int(a.get("subscribersGained")),
                                     source="youtube-api"))
    ch = c.send(Request("GET", f"{YT_API}/channels", headers=auth, params={"part": "statistics", "mine": "true"})).json()
    stats = ((ch.get("items") or [{}])[0]).get("statistics") or {}
    if stats.get("hiddenSubscriberCount"):
        col.notes.append("youtube: the subscriber count is hidden")
    col.accounts.append({"network": "youtube", "followers": _int(stats.get("subscriberCount")), "source": "youtube-api"})


def collect_instagram(c: _Client, rows: list[dict], col: Collection) -> None:
    api = f"https://graph.instagram.com/{c.var('INSTAGRAM_GRAPH_VERSION', 'v24.0')}"
    token = c.var("INSTAGRAM_ACCESS_TOKEN")
    for r in rows:
        resp = c.transport.send(Request("GET", f"{api}/{r['remote_id']}/insights", params={
            "metric": "views,reach,likes,comments,shares,saved,ig_reels_avg_watch_time", "access_token": token}))
        if resp.status == 400 and re.search(r"does not exist|cannot be loaded|unsupported get", resp.text(), re.I):
            col.removed.append(("instagram", r["remote_id"], redact_text(resp.text())[:200]))
            continue
        if not resp.ok:
            col.notes.append(f"instagram {r['remote_id']}: insights answered {resp.status}")
            continue
        vals = {d.get("name"): ((d.get("values") or [{}])[0]).get("value") for d in resp.json().get("data") or []}
        avg = vals.get("ig_reels_avg_watch_time")
        col.posts.append(PostNumbers("instagram", r["remote_id"], r["url"], r["id"], r["format"], r["posted_at"],
                                     views=_int(vals.get("views")), likes=_int(vals.get("likes")),
                                     comments=_int(vals.get("comments")), shares=_int(vals.get("shares")),
                                     saves=_int(vals.get("saved")), avg_view_s=(avg / 1000 if avg else None),
                                     source="instagram-insights"))
    me = c.send(Request("GET", f"{api}/me", params={"fields": "followers_count,media_count", "access_token": token})).json()
    col.accounts.append({"network": "instagram", "followers": _int(me.get("followers_count")), "source": "instagram-api"})


def reconcile_buffer(c: _Client, log: PostLog, now: datetime, col: Collection) -> None:
    """AntiSearch logged 194 TikToks as published with only Buffer's id. Here a handed-off post stays `handed` until
    Buffer says it went out, and then gets the network's own link."""
    for (item, net), r in list(log.latest().items()):
        if r["status"] != HANDED or not r.get("remote_id"):
            continue
        body = c.send(Request("POST", BUFFER_API, headers={"Authorization": f"Bearer {c.var('BUFFER_API_KEY')}"},
                              json={"query": BUFFER_STATUS, "variables": {"id": r["remote_id"]}})).json()
        post = (body.get("data") or {}).get("post") or {}
        fields = {k: r.get(k, "") for k in PostRow.__dataclass_fields__}
        if post.get("status") == "sent":
            log.append(PostRow(**{**fields, "status": PUBLISHED, "at": iso(now), "url": post.get("externalLink") or r["url"], "detail": "sent, confirmed by Buffer"}))
        elif post.get("status") == "error":
            why = ((post.get("error") or {}).get("message") or "Buffer reported an error")[:300]
            log.append(PostRow(**{**fields, "status": FAILED, "at": iso(now), "detail": why}))
            col.removed.append((net, r["remote_id"], f"Buffer could not send it: {why}"))
        elif (parse_iso(r["at"]) or now) < now - timedelta(hours=24):
            col.problems.append(f"{net}: {item} is still with Buffer after 24 h (status {post.get('status')})")


def buffer_nodes(c: _Client) -> list[dict]:
    def gql(query: str, variables: dict) -> dict:
        body = c.send(Request("POST", BUFFER_API, headers={"Authorization": f"Bearer {c.var('BUFFER_API_KEY')}"},
                              json={"query": query, "variables": variables})).json()
        if body.get("errors"):
            raise RuntimeError(f"buffer: {body['errors'][0].get('message')}")
        return body["data"]

    org = c.var("BUFFER_ORG_ID") or gql(BUFFER_ORGS, {})["account"]["organizations"][0]["id"]
    out, after = [], None
    for _ in range(4):  # no date filter: startDate drops API-created shareNow posts; stop at 200
        page = gql(BUFFER_POSTS, {"input": {"organizationId": org, "filter": {"status": ["sent"]}},
                                  "first": 50, "after": after})["posts"]
        out += [e["node"] for e in page.get("edges") or []]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    return out


def collect_buffer(nodes: list[dict], rows_by_net: dict[str, list[dict]], col: Collection) -> dict[str, str]:
    """Buffer fills unreported metrics with 0, so a set is used only once Buffer refreshed it and it has a non-zero
    value. Returns {buffer id: network URL} for the joins."""
    links = {}
    ours = {r["remote_id"]: r for rows in rows_by_net.values() for r in rows}
    for n in nodes:
        net = BUFFER_SERVICE.get(n.get("channelService", ""))
        if not net:
            continue
        if n.get("externalLink"):
            links[n["id"]] = n["externalLink"]
        m = n.get("metrics") or []
        r = ours.get(n["id"])
        if not r or not n.get("metricsUpdatedAt") or not m or all(not _int(x.get("value")) for x in m):
            continue
        val = {x.get("type"): x.get("value") for x in m}
        pick = lambda *ks: next((_int(val[k]) for k in ks if k in val), None)  # noqa: E731
        col.posts.append(PostNumbers(net, n["id"], n.get("externalLink") or r["url"], r["id"], r["format"],
                                     r["posted_at"], views=pick("views", "impressions"), likes=pick("reactions", "likes"),
                                     comments=pick("comments"), shares=pick("shares", "reposts"), saves=pick("saves"),
                                     source=f"buffer@{n['metricsUpdatedAt']}"))
    return links


def _script_json(page: str, script_id: str) -> dict | None:
    m = re.search(rf'<script id="{script_id}"[^>]*>(.*?)</script>', page, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def tiktok_profile(page: str, handle: str) -> int | None:
    data = _script_json(page, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    info = ((data or {}).get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo") or {})
    stats = info.get("statsV2") or info.get("stats")
    if not stats or (info.get("user") or {}).get("uniqueId", "").lower() != handle.lower():
        return None
    return _int(stats.get("followerCount"))


def tiktok_video(page: str, video_id: str) -> dict | None:
    data = _script_json(page, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
    item = ((data or {}).get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {}).get("itemInfo", {})
            .get("itemStruct") or {})
    if str(item.get("id", "")) != video_id:
        return None
    s = item.get("statsV2") or item.get("stats") or {}
    return {"views": _int(s.get("playCount")), "likes": _int(s.get("diggCount")), "comments": _int(s.get("commentCount")),
            "shares": _int(s.get("shareCount")), "saves": _int(s.get("collectCount"))}


def collect_tiktok_public(transport: Transport, handle: str, rows: list[dict], links: dict[str, str],
                          col: Collection, sleep=None) -> None:
    get = lambda url: transport.send(Request("GET", url, headers={"User-Agent": BROWSER_UA,  # noqa: E731
                                                                   "Accept-Language": "en-US,en;q=0.9"}), timeout=20)
    resp = get(f"https://www.tiktok.com/@{handle}")
    followers = tiktok_profile(resp.text(), handle) if resp.ok else None
    if followers is not None:
        col.accounts.append({"network": "tiktok", "followers": followers, "source": "tiktok-profile"})
    have = {(p.network, p.remote_id) for p in col.posts}
    for r in rows[-20:]:
        url = links.get(r["remote_id"]) or r["url"]
        m = re.search(r"/video/(\d+)", url or "")
        if not m or ("tiktok", r["remote_id"]) in have:
            continue
        if sleep:
            sleep(3.0)
        page = get(url)
        stats = tiktok_video(page.text(), m.group(1)) if page.ok else None
        if stats:
            col.posts.append(PostNumbers("tiktok", r["remote_id"], url, r["id"], r["format"], r["posted_at"], **stats,
                                         source="tiktok-page"))


def collect(state_dir: Path, cfg: Config, env: Mapping[str, str], *, now: datetime, live: bool,
            transport: Transport | None = None, sleep=None) -> Collection:
    log = PostLog(state_dir / "posts.csv")
    st = GuardState.load(state_dir / "state.json")
    col = Collection()
    if not live:
        col.notes.append("dry run: no network was asked")
        return col
    t = transport or UrllibTransport()
    c = _Client(t, env)
    rows = posted(log)
    nets = {n.name: n for n in cfg.enabled()}
    for name in ("youtube", "instagram"):
        n = nets.get(name)
        if not n or n.route != "direct" or not rows.get(name):
            continue
        try:
            collect_youtube(c, rows[name], now, col) if name == "youtube" else collect_instagram(c, rows[name], col)
        except (HttpError, RuntimeError, KeyError, ValueError) as e:
            col.problems.append(f"{name}: {redact_text(str(e))[:300]}")
    links: dict[str, str] = {}
    if (env.get("BUFFER_API_KEY") or "").strip():
        try:
            reconcile_buffer(c, log, now, col)
            rows = posted(log)
        except (HttpError, RuntimeError, KeyError) as e:
            col.notes.append(f"buffer reconcile: {redact_text(str(e))[:200]}")
    buffered = {k: v for k, v in rows.items() if nets.get(k) and nets[k].route == "buffer"}
    last = parse_iso(st.alarms.get("_metrics_buffer", {}).get("sent_at", ""))
    if buffered and (env.get("BUFFER_API_KEY") or "").strip() and (not last or now - last >= BUFFER_EVERY):
        try:
            links = collect_buffer(buffer_nodes(c), buffered, col)
            st.alarms["_metrics_buffer"] = {"sent_at": iso(now), "cleared": True}
        except (HttpError, RuntimeError, KeyError) as e:
            col.notes.append(f"buffer: {redact_text(str(e))[:200]}")
    handle = str((cfg.raw.get("networks", {}).get("tiktok") or {}).get("handle") or "").lstrip("@")
    if "tiktok" in buffered and handle:
        collect_tiktok_public(t, handle, buffered["tiktok"], links, col, sleep)
    for name in nets:
        if cfg.is_live(name) and rows.get(name) and not any(p.network == name for p in col.posts) \
                and not any(a["network"] == name for a in col.accounts):
            col.problems.append(f"{name}: live, but no numbers came back")
    st.save()
    return col


def append_csv(path: Path, fields_: tuple[str, ...], rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields_, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def latest_posts(rows: list[dict]) -> list[dict]:
    last: dict[tuple[str, str], dict] = {}
    for r in rows:
        last[(r["network"], r["remote_id"])] = r
    return sorted(last.values(), key=lambda r: r.get("posted_at") or "")


def scoreboard(rows: list[dict]) -> dict:
    """Per network: the floor (median views of the last 20 posts) and the formats against it, with the method's
    rules: kill a format with no post above 2x the floor after 10 posts; a post above 5x the floor is a breakout."""
    out: dict = {"networks": {}}
    posts = latest_posts(rows)
    for net in sorted({p["network"] for p in posts}):
        mine = [p for p in posts if p["network"] == net and _int(p.get("views")) is not None]
        views = [_int(p["views"]) for p in mine]
        floor = statistics.median(views[-20:]) if views else None
        fmts: dict[str, dict] = {}
        for p in mine:
            f = fmts.setdefault(p.get("format") or "?", {"posts": 0, "views": []})
            f["posts"] += 1
            f["views"].append(_int(p["views"]))
        for name, f in fmts.items():
            best = max(f["views"])
            ratio = round(best / floor, 2) if floor else None
            state = "trial"
            if floor and f["posts"] >= 10 and best <= 2 * floor:
                state = "kill"
            elif floor and best > 5 * floor and len(views) >= 5:
                state = "double"
            elif f["posts"] >= 10:
                state = "live"
            fmts[name] = {"posts": f["posts"], "median_views": statistics.median(f["views"]), "best_views": best,
                          "best_vs_floor": ratio, "state": state}
        out["networks"][net] = {"posts_with_numbers": len(mine), "floor": floor, "formats": fmts}
    return out


def follows_per_1000(accounts: list[dict], posts: list[dict], net: str, now: datetime, days: int = 7) -> float | None:
    acc = [a for a in accounts if a["network"] == net and _int(a.get("followers")) is not None]
    if len(acc) < 2:
        return None
    cutoff = now - timedelta(days=days)
    old = [a for a in acc if (parse_iso(a["ts_utc"]) or now) <= cutoff] or acc[:1]
    gained = _int(acc[-1]["followers"]) - _int(old[-1]["followers"])
    views = sum(_int(p.get("views")) or 0 for p in latest_posts(posts)
                if p["network"] == net and (parse_iso(p.get("posted_at") or "") or now) >= cutoff)
    return round(gained / views * 1000, 2) if views else None


def summary(state_dir: Path, now: datetime) -> str:
    posts = read_csv(state_dir / "metrics" / "posts.csv")
    accounts = read_csv(state_dir / "metrics" / "accounts.csv")
    board = scoreboard(posts)
    lines = [f"# Social numbers ({iso(now)})", "", "A blank is a number the network did not give, never a zero.", ""]
    for net, b in board["networks"].items():
        fol = [a for a in accounts if a["network"] == net]
        lines += [f"## {net}", "",
                  f"- posts with numbers: {b['posts_with_numbers']}; floor (median of the last 20): {b['floor']}",
                  f"- followers: {fol[-1]['followers'] if fol else ''}; follows per 1,000 views (7 days): "
                  f"{follows_per_1000(accounts, posts, net, now) or ''}", "",
                  "| Format | Posts | Median views | Best | Best ÷ floor | State |", "|---|---|---|---|---|---|"]
        for name, f in sorted(b["formats"].items()):
            lines.append(f"| {name} | {f['posts']} | {f['median_views']} | {f['best_views']} | {f['best_vs_floor'] or ''} "
                         f"| {f['state']} |")
        lines.append("")
    return "\n".join(lines)


def write(state_dir: Path, col: Collection, now: datetime) -> dict:
    ts = iso(now)
    append_csv(state_dir / "metrics" / "posts.csv", POST_FIELDS, [{"ts_utc": ts, **asdict(p)} for p in col.posts])
    append_csv(state_dir / "metrics" / "accounts.csv", ACCOUNT_FIELDS, [{"ts_utc": ts, **a} for a in col.accounts])
    board = scoreboard(read_csv(state_dir / "metrics" / "posts.csv"))
    board["at"] = ts
    (state_dir / "metrics" / "scoreboard.json").parent.mkdir(parents=True, exist_ok=True)
    (state_dir / "metrics" / "scoreboard.json").write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    (state_dir / "metrics" / "summary.md").write_text(summary(state_dir, now), encoding="utf-8")
    return board


def react(state_dir: Path, cfg: Config, col: Collection, alarms: Alarms, now: datetime) -> None:
    """Removed posts, the reach brake and breakouts, from what the numbers say."""
    log = PostLog(state_dir / "posts.csv")
    st = alarms.state
    final = {(r["remote_id"], r["network"]): r for r in log.latest().values() if r.get("remote_id")}
    for net, rid, why in col.removed:
        r = final.get((rid, net))
        if r and r["status"] != REMOVED:
            log.append(PostRow(**{k: r.get(k, "") for k in PostRow.__dataclass_fields__} | {
                "status": REMOVED, "at": iso(now), "detail": f"gone from {net}: {why}"[:300]}))
            alarms.raise_(f"removed-{net}-{rid}", f"{net}: a post was removed",
                          f"{r['url']} ({r['id']}, format {r['format']}) is gone: {why}. Read the network's notice; "
                          f"repeated removals mean the format breaks a rule.", "high")
    board = scoreboard(read_csv(state_dir / "metrics" / "posts.csv"))
    posts = latest_posts(read_csv(state_dir / "metrics" / "posts.csv"))
    for net, b in board["networks"].items():
        views = [_int(p["views"]) for p in posts if p["network"] == net and _int(p.get("views")) is not None]
        if len(views) >= 25:
            recent, before = statistics.median(views[-5:]), statistics.median(views[-25:-5])
            if before >= cfg.reach_floor and recent <= cfg.reach_ratio * before:
                if st.trip(net, now, f"reach fell from a median of {before} to {recent}", cfg.brake_hours):
                    alarms.raise_(f"brake-{net}", f"{net}: reach halved; the brake tripped",
                                  f"The last 5 posts' median is {recent}, against {before} before. Posts are halved for "
                                  f"{cfg.brake_hours} h.")
        floor = b["floor"]
        if not floor or len(views) < 5:
            continue
        for p in posts:
            v = _int(p.get("views"))
            key = p["remote_id"]
            if p["network"] == net and v and v > 5 * floor and not st.seen.get(key, {}).get("breakout"):
                st.seen.setdefault(key, {})["breakout"] = iso(now)
                alarms.event("metric-social-breakout", {"network": net, "url": p["url"], "format": p["format"],
                                                        "views": v, "floor": floor})
