"""The pipe's command line: `PYTHONPATH=engine python -m social <command>`, from the social repo's root.

    validate                        check social.json and every queue row's shape
    due                             exit 0 when some network has a slot due (the gate that ends idle wakes)
    publish [--live] [--network N] [--no-wait]
    intake [--recheck]              check new posts, quarantine the failures, report the runway
    pages --out DIR                 build the Pages runway
    metrics                         collect the numbers, rebuild the summary and the scoreboard
    watch                           runway, heartbeat, posts with no outcome
    archive                         move items posted over a week ago to archive/
    resolve NETWORK ID (--url URL | --failed)   a person settles a post with no outcome
    resume NETWORK --reason TEXT    a person restarts a stopped network
    check-public URL                does this post show up for a logged-out viewer?
    token-refresh --out FILE        extend Instagram's token; the new one goes to FILE, never to the log
    youtube-auth url | exchange --code CODE --out FILE     the founder's one-time consent
    buffer-channels                 the channel ids to pin as BUFFER_CHANNEL_<NETWORK>
    status                          one view of slots, queue and guards

Every command takes --root (default: here) and --state (default: state/, a checkout of the social-state branch).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from . import VERSION
from .alarms import Alarms
from .base import Retryable
from .buffer import CHANNELS, ORGS, BufferTikTok
from .config import Config, ConfigError, load
from .http import Request, UrllibTransport, redact_text
from .instagram import InstagramDirect, refresh_token
from .intake import check as intake_check, fetcher
from .metrics import collect, react, write
from .pages import build
from .publish import LOOKAHEAD, adapter_for, run
from .queue import items, move, shape_problems
from .schedule import due
from .state import PENDING, PUBLISHED, FAILED, GuardState, PostLog, PostRow, iso, now_utc, parse_iso
from .watch import check as watch_check
from .youtube import SCOPES, TOKEN_URL

REDIRECT = "http://localhost"


def out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def any_live(cfg: Config) -> bool:
    return any(cfg.is_live(n.name) for n in cfg.enabled())


def alarms_for(st: GuardState, cfg: Config) -> Alarms:
    return Alarms.from_config(cfg, st, os.environ, live=any_live(cfg), now=now_utc())


def cmd_validate(a, cfg: Config, root: Path, state: Path) -> int:
    problems = []
    if not cfg.company_repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", cfg.company_repo):
        problems.append("social.json: company_repo must be owner/name")
    if not cfg.public_base_url:
        problems.append("social.json: public_base_url is empty (the Pages URL)")
    for it in items(root):
        for p in (["not valid JSON"] if it.data.get("_unreadable") else shape_problems(it.data, it.path.parent.name)):
            problems.append(f"{it.path.relative_to(root).as_posix()}: {p}")
    for n in cfg.enabled():
        adapter_for(n.name, n.route)
    for p in problems:
        print(f"x {p}", file=sys.stderr)
    if not problems:
        print(f"ok: {cfg.company}, networks {', '.join(n.name for n in cfg.enabled())}, engine {VERSION}")
    return 1 if problems else 0


def cmd_due(a, cfg: Config, root: Path, state: Path) -> int:
    log = PostLog(state / "posts.csv")
    st = GuardState.load(state / "state.json")
    verdicts = {n.name: due(cfg, n, now_utc(), log, st, lookahead=LOOKAHEAD) for n in cfg.enabled()}
    for name, v in verdicts.items():
        print(f"{name}: {'due' if v.due else v.reason}")
    return 0 if any(v.due for v in verdicts.values()) else 1


def alarm_status(alarms: Alarms) -> int:
    """An alarm that could not be sent turns the run red, after everything was saved."""
    for e in alarms.errors:
        print(f"x alarm not sent: {e}", file=sys.stderr)
    return 1 if alarms.errors else 0


def git_checkpoint(state: Path):
    """Push the pending row before the API call. If it cannot be saved, nothing goes out."""

    def save() -> None:
        git = ["git", "-C", str(state)]
        who = ["-c", "user.name=social-publisher", "-c", "user.email=actions@users.noreply.github.com"]
        try:
            subprocess.run([*git, "add", "-A", "."], check=True, capture_output=True, timeout=60)
            if subprocess.run([*git, "diff", "--cached", "--quiet"], timeout=60).returncode == 0:
                return
            subprocess.run([*git, *who, "commit", "--quiet", "-m", f"pending {iso(now_utc())}"], check=True, capture_output=True, timeout=60)
            subprocess.run([*git, "push", "--quiet", "origin", "HEAD:social-state"], check=True, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as e:
            raise Retryable("could not save the pending row, so nothing was sent") from e

    return save


def cmd_publish(a, cfg: Config, root: Path, state: Path) -> int:
    results = run(root, state, cfg, os.environ, now=now_utc(), live=a.live, out=Path(a.out), only=a.network,
                  wait=not a.no_wait, checkpoint=git_checkpoint(state) if a.live and (state / ".git").exists() else None)
    out([r.__dict__ for r in results])
    st = GuardState.load(state / "state.json")
    alarms = alarms_for(st, cfg)
    watch_check(root, state, cfg, alarms, now_utc())
    if any_live(cfg):
        st.save()
    return alarm_status(alarms)


def cmd_intake(a, cfg: Config, root: Path, state: Path) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    rep = intake_check(root, cfg, fetcher(repo, root / ".media"), now_utc(), recheck=a.recheck)
    st = GuardState.load(state / "state.json")
    alarms = alarms_for(st, cfg)
    report = watch_check(root, state, cfg, alarms, now_utc())
    rep.runway = report["runway"]
    if rep.quarantined:
        alarms.raise_("quarantine", f"intake quarantined {len(rep.quarantined)} post(s)",
                      "\n".join(f"- {k}: {'; '.join(v)}" for k, v in rep.quarantined.items())[:6000])
    else:
        alarms.clear("quarantine", "the last batch passed intake")
    if any_live(cfg):
        st.save()
    out(rep.as_dict())
    return alarm_status(alarms)


def cmd_pages(a, cfg: Config, root: Path, state: Path) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    out(build(root, state, Path(a.out), fetcher(repo, root / ".media"), now_utc()))
    return 0


def cmd_metrics(a, cfg: Config, root: Path, state: Path) -> int:
    now = now_utc()
    live = any_live(cfg)
    col = collect(state, cfg, os.environ, now=now, live=live)
    write(state, col, now)
    st = GuardState.load(state / "state.json")
    alarms = alarms_for(st, cfg)
    react(state, cfg, col, alarms, now)
    watch_check(root, state, cfg, alarms, now)
    if live:
        st.save()
    out({"posts": len(col.posts), "accounts": len(col.accounts), "removed": col.removed, "notes": col.notes,
         "problems": col.problems})
    for p in col.problems:
        print(f"x {p}", file=sys.stderr)
    return 1 if col.problems or alarm_status(alarms) else 0


def cmd_watch(a, cfg: Config, root: Path, state: Path) -> int:
    st = GuardState.load(state / "state.json")
    alarms = alarms_for(st, cfg)
    out({**watch_check(root, state, cfg, alarms, now_utc()), "alarms": alarms.sent})
    if any_live(cfg):
        st.save()
    return alarm_status(alarms)


def cmd_archive(a, cfg: Config, root: Path, state: Path) -> int:
    log = PostLog(state / "posts.csv")
    final, moved = log.latest(), 0
    now = now_utc()
    for it in items(root):
        last = final.get((it.id, it.network))
        when = parse_iso(last["at"]) if last else None
        if last and last["status"] not in (PENDING, "retry") and when and now - when > timedelta(days=7):
            move(it, root, "archive")
            moved += 1
    print(f"archived {moved}")
    return 0


def cmd_resolve(a, cfg: Config, root: Path, state: Path) -> int:
    log = PostLog(state / "posts.csv")
    last = log.latest().get((a.id, a.network))
    if not last or last["status"] != PENDING:
        print(f"{a.network}/{a.id} has no post without an outcome", file=sys.stderr)
        return 1
    fields = {k: last.get(k, "") for k in PostRow.__dataclass_fields__}
    fields.update(at=iso(now_utc()), detail="settled by a person")
    if a.failed:
        fields.update(status=FAILED)
    else:
        fields.update(status=PUBLISHED, url=a.url, remote_id=a.remote_id or a.url.rstrip("/").rsplit("/", 1)[-1])
    log.append(PostRow(**fields))
    print(f"{a.network}/{a.id}: {fields['status']}")
    return 0


def cmd_resume(a, cfg: Config, root: Path, state: Path) -> int:
    st = GuardState.load(state / "state.json")
    if a.network not in st.stopped:
        print(f"{a.network} is not stopped")
        return 0
    del st.stopped[a.network]
    st.alarms.pop(f"restricted-{a.network}", None)
    st.save()
    print(f"{a.network} resumed: {a.reason}")
    return 0


def cmd_check_public(a, cfg: Config, root: Path, state: Path) -> int:
    """A post from a Development-mode app shows only to role users, so the first post is read logged out."""
    resp = UrllibTransport("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0").send(Request("GET", a.url), timeout=30)
    text = resp.text()
    gone = not resp.ok or re.search(r"Page Not Found|isn.t available|may have been removed|Sorry, this page", text, re.I)
    print(json.dumps({"url": a.url, "status": resp.status, "visible_logged_out": not gone}))
    return 1 if gone else 0


def cmd_token_refresh(a, cfg: Config, root: Path, state: Path) -> int:
    adapter = InstagramDirect(UrllibTransport(), os.environ)
    body = refresh_token(adapter)
    target = Path(a.out)
    target.write_text(body["access_token"], encoding="utf-8")
    print(f"instagram token extended: {int(body.get('expires_in', 0)) // 86400} days")
    return 0


def cmd_youtube_auth(a, cfg: Config, root: Path, state: Path) -> int:
    cid = (os.environ.get("YOUTUBE_CLIENT_ID") or "").strip()
    if not cid:
        print("YOUTUBE_CLIENT_ID is not set", file=sys.stderr)
        return 1
    if a.action == "url":
        print("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "consent"}))
        return 0
    code = a.code
    if "://" in code:
        m = re.search(r"[?&]code=([^&]+)", code)
        code = m.group(1) if m else ""
    from urllib.parse import unquote

    resp = UrllibTransport().send(Request("POST", TOKEN_URL, form={
        "client_id": cid, "client_secret": os.environ.get("YOUTUBE_CLIENT_SECRET", ""), "code": unquote(code),
        "grant_type": "authorization_code", "redirect_uri": REDIRECT}))
    body = resp.json() if resp.body else {}
    if "refresh_token" not in body:
        print(f"no refresh token ({resp.status}): {redact_text(str(body.get('error_description') or body.get('error')))}",
              file=sys.stderr)
        return 1
    Path(a.out).write_text(body["refresh_token"], encoding="utf-8")
    print(f"refresh token written to {a.out}")
    return 0


def cmd_buffer_channels(a, cfg: Config, root: Path, state: Path) -> int:
    """The ids to pin as BUFFER_CHANNEL_<NETWORK>, which saves two requests per post."""
    b = BufferTikTok(UrllibTransport(), os.environ)
    org = b.var("BUFFER_ORG_ID") or b.gql(ORGS)["account"]["organizations"][0]["id"]
    out([{"service": c["service"], "id": c["id"], "name": c["name"]} for c in b.gql(CHANNELS, {"org": org})["channels"]])
    return 0


def cmd_status(a, cfg: Config, root: Path, state: Path) -> int:
    log = PostLog(state / "posts.csv")
    st = GuardState.load(state / "state.json")
    now = now_utc()
    rows = {}
    for n in cfg.enabled():
        v = due(cfg, n, now, log, st, lookahead=LOOKAHEAD)
        waiting = [i for i in items(root, network=n.name) if i.id not in log.used_ids()]
        rows[n.name] = {"route": n.route, "live": cfg.is_live(n.name), "slots": [f"{t:%H:%M}" for t in n.slots],
                        "due": v.due, "why": v.reason, "waiting": len(waiting),
                        "checked": sum(1 for i in waiting if i.checked), "braked": st.braked(n.name, now),
                        "stopped": st.stopped.get(n.name, "")}
    out({"company": cfg.company, "engine": VERSION, "cron": cfg.cron, "networks": rows})
    return 0


COMMANDS = {
    "validate": cmd_validate, "due": cmd_due, "publish": cmd_publish, "intake": cmd_intake, "pages": cmd_pages,
    "metrics": cmd_metrics, "watch": cmd_watch, "archive": cmd_archive, "resolve": cmd_resolve, "resume": cmd_resume,
    "check-public": cmd_check_public, "token-refresh": cmd_token_refresh, "youtube-auth": cmd_youtube_auth,
    "status": cmd_status, "buffer-channels": cmd_buffer_channels,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="social")
    ap.add_argument("--root", default=".")
    ap.add_argument("--state", default="state")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "publish":
            p.add_argument("--live", action="store_true")
            p.add_argument("--network")
            p.add_argument("--no-wait", action="store_true")
            p.add_argument("--out", default="out")
        if name == "intake":
            p.add_argument("--recheck", action="store_true")
        if name == "pages":
            p.add_argument("--out", default="site")
        if name == "resolve":
            p.add_argument("network")
            p.add_argument("id")
            p.add_argument("--url", default="")
            p.add_argument("--remote-id", default="")
            p.add_argument("--failed", action="store_true")
        if name == "resume":
            p.add_argument("network")
            p.add_argument("--reason", required=True)
        if name == "check-public":
            p.add_argument("url")
        if name == "token-refresh":
            p.add_argument("--out", required=True)
        if name == "youtube-auth":
            p.add_argument("action", choices=["url", "exchange"])
            p.add_argument("--code", default="")
            p.add_argument("--out", default="")
    a = ap.parse_args(argv)
    root, state = Path(a.root).resolve(), Path(a.state)
    if not state.is_absolute():
        state = root / state
    try:
        cfg = load(root, os.environ)
    except ConfigError as e:
        print(f"x {e}", file=sys.stderr)
        return 2
    return COMMANDS[a.cmd](a, cfg, root, state)


if __name__ == "__main__":
    sys.exit(main())
