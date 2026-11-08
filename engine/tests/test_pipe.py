"""The pipe's tests. Offline: every network answer is a stub. Run from engine/social: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from social import alarms as alarms_mod, intake, metrics, pages, phash, publish, schedule, watch  # noqa: E402
from social.base import Media, PostSpec, Retryable  # noqa: E402
from social.buffer import BufferTikTok, BufferX  # noqa: E402
from social.config import ConfigError, load  # noqa: E402
from social.http import HttpResponse, RecordingTransport, Request, redact_text  # noqa: E402
from social.instagram import InstagramDirect  # noqa: E402
from social.queue import Item, items, ready, shape_problems  # noqa: E402
from social.state import FAILED, PENDING, PUBLISHED, RETRY, GuardState, PostLog, PostRow, iso  # noqa: E402
from social.tiktok import TikTokInbox  # noqa: E402
from social.x import XDirect, oauth1_header  # noqa: E402
from social.youtube import YouTubeDirect  # noqa: E402

UTC = timezone.utc
SHA = "a" * 64
BASE = {
    "company": "Test Co",
    "company_repo": "owner/test",
    "timezone": "America/New_York",
    "public_base_url": "https://owner.github.io/test-social",
    "networks": {
        "tiktok": {"route": "buffer", "slots": ["10:07", "13:07", "16:07", "19:07", "22:07"], "handle": "testco"},
        "instagram": {"route": "direct", "slots": ["12:07", "18:07"]},
        "youtube": {"route": "direct", "slots": ["11:07", "20:07"]},
    },
}


def et(y, m, d, h, mi=0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/New_York"))


class Repo:
    """A throwaway social repo."""

    def __init__(self, cfg: dict | None = None, env: dict | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="social-test-"))
        self.raw = json.loads(json.dumps(cfg or BASE))
        (self.dir / "social.json").write_text(json.dumps(self.raw), encoding="utf-8")
        self.env = {"SOCIAL_CRON": "live", "SOCIAL_LIVE_NETWORKS": "tiktok,instagram,youtube", **(env or {})}
        self.state = self.dir / "state"
        self.state.mkdir()
        self.out = self.dir / "out"

    @property
    def cfg(self):
        return load(self.dir, self.env)

    @property
    def log(self):
        return PostLog(self.state / "posts.csv")

    def add(self, network="tiktok", id_="post-001", **over) -> Item:
        data = {
            "id": id_, "network": network, "format": "hotlap", "batch": "2026-W46",
            "media": {"file": f"{id_}.mp4", "sha256": SHA, "kind": "video"},
            "caption": f"caption of {id_}", "title": f"title of {id_}", "language": "en",
            "ai": {"generated": False, "realistic": False},
            "intake": {"ok": True, "probe": {"width": 1080, "height": 1920, "duration_s": 15.0, "bytes": 2048}, "phash": []},
        }
        data.update(over)
        p = self.dir / "queue" / network / f"{id_}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
        return Item(p, data)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Stub:
    """A transport answering from a function; records every request."""

    def __init__(self, fn):
        self.fn = fn
        self.requests: list[Request] = []

    def send(self, req: Request, timeout: float = 60.0) -> HttpResponse:
        self.requests.append(req)
        return self.fn(req)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.addCleanup(self.r.close)


class TestConfig(TmpCase):
    def bad(self, **net):
        raw = json.loads(json.dumps(BASE))
        raw["networks"]["tiktok"].update(net)
        r = Repo(raw)
        self.addCleanup(r.close)
        with self.assertRaises(ConfigError) as c:
            r.cfg
        return str(c.exception)

    def test_loads(self):
        cfg = self.r.cfg
        self.assertEqual(cfg.networks["tiktok"].route, "buffer")
        self.assertTrue(cfg.is_live("tiktok"))

    def test_company_cannot_go_above_the_limits(self):
        self.assertIn("above the engine's limit", self.bad(max_24h=6))
        self.assertIn("below the engine's limit", self.bad(min_gap_minutes=60))
        self.assertIn("slots, the limit", self.bad(slots=["09:07", "11:07", "13:07", "15:07", "17:07", "19:07"]))

    def test_slots_off_the_hour_and_never_overnight(self):
        self.assertIn(":00 or :30", self.bad(slots=["10:00"]))
        self.assertIn("overnight", self.bad(slots=["03:07"]))

    def test_slots_keep_the_gap(self):
        self.assertIn("closer than", self.bad(slots=["10:07", "11:07"]))

    def test_tiktok_cannot_use_a_route_that_posts_privately(self):
        self.assertIn("route", self.bad(route="direct"))

    def test_the_warmup_cannot_be_shortened(self):
        raw = json.loads(json.dumps(BASE))
        raw["warmup"] = {"days": 1}
        r = Repo(raw)
        self.addCleanup(r.close)
        self.assertEqual(r.cfg.warmup_days, 14)

    def test_variables_override(self):
        r = Repo(env={"SOCIAL_ROUTE_INSTAGRAM": "buffer", "SOCIAL_SLOTS_TIKTOK": "off", "SOCIAL_CRON": "dry-run"})
        self.addCleanup(r.close)
        cfg = r.cfg
        self.assertEqual(cfg.networks["instagram"].route, "buffer")
        self.assertEqual(cfg.networks["tiktok"].slots, [])
        self.assertFalse(cfg.is_live("tiktok"))


class TestSchedule(TmpCase):
    def test_ramp(self):
        self.assertEqual(schedule.ramp(5, 0, 14, 1), 1)
        self.assertEqual(schedule.ramp(5, 7, 14, 1), 3)
        self.assertEqual(schedule.ramp(5, 14, 14, 1), 5)
        self.assertEqual(schedule.ramp(0, 3, 14, 1), 0)

    def test_posting_day_starts_when_the_quiet_hours_end(self):
        cfg = self.r.cfg
        self.assertEqual(schedule.posting_day(cfg, et(2026, 11, 13, 0, 30)).isoformat(), "2026-11-12")
        self.assertEqual(schedule.posting_day(cfg, et(2026, 11, 13, 9)).isoformat(), "2026-11-13")

    def test_jitter_is_seeded_and_off_the_round_minutes(self):
        cfg = self.r.cfg
        day = et(2026, 11, 12, 12).date()
        st = GuardState(self.r.state / "state.json", first_post={"tiktok": "2026-01-01"})
        a = schedule.day_slots(cfg, cfg.networks["tiktok"], day, st, et(2026, 11, 12, 12))
        b = schedule.day_slots(cfg, cfg.networks["tiktok"], day, st, et(2026, 11, 12, 12))
        self.assertEqual([s.opens for s in a], [s.opens for s in b])
        self.assertEqual(len(a), 5)
        for s in a:
            self.assertNotIn(s.opens.minute, (0, 30))
            self.assertLessEqual(s.opens - s.nominal, timedelta(minutes=40))

    def test_warmup_starts_at_one_post_a_day(self):
        cfg = self.r.cfg
        st = GuardState(self.r.state / "state.json")
        day = et(2026, 11, 12, 12).date()
        self.assertEqual(len(schedule.day_slots(cfg, cfg.networks["tiktok"], day, st, et(2026, 11, 12, 12))), 1)

    def test_brake_halves_the_posts(self):
        cfg = self.r.cfg
        now = et(2026, 11, 12, 12)
        st = GuardState(self.r.state / "state.json", first_post={"tiktok": "2026-01-01"})
        st.trip("tiktok", now, "limit", 72)
        self.assertEqual(len(schedule.day_slots(cfg, cfg.networks["tiktok"], now.date(), st, now)), 2)

    def seeded(self):
        return GuardState(self.r.state / "state.json", first_post={"tiktok": "2026-01-01", "instagram": "2026-01-01",
                                                                   "youtube": "2026-01-01"})

    def test_due_at_a_slot_and_not_in_quiet_hours(self):
        cfg, st = self.r.cfg, self.seeded()
        v = schedule.due(cfg, cfg.networks["tiktok"], et(2026, 11, 12, 11), self.r.log, st)
        self.assertTrue(v.due, v.reason)
        v = schedule.due(cfg, cfg.networks["tiktok"], et(2026, 11, 12, 3), self.r.log, st)
        self.assertEqual(v.reason, "quiet hours")

    def test_rolling_cap_and_gap(self):
        cfg, st = self.r.cfg, self.seeded()
        now = et(2026, 11, 12, 22, 50)
        for i in range(5):
            at = iso(now - timedelta(hours=3 * (i + 1)))
            self.r.log.append(PostRow(id=f"p{i}", network="tiktok", status=PENDING, at=at, slot=f"old{i}"))
            self.r.log.append(PostRow(id=f"p{i}", network="tiktok", status=PUBLISHED, at=at, slot=f"old{i}"))
        self.assertIn("in the last 24 h", schedule.due(cfg, cfg.networks["tiktok"], now, self.r.log, st).reason)

    def test_min_gap_holds_even_with_a_slot_open(self):
        cfg, st = self.r.cfg, self.seeded()
        now = et(2026, 11, 12, 13, 55)
        self.r.log.append(PostRow(id="p", network="tiktok", status=PENDING, at=iso(now - timedelta(minutes=30)), slot="x"))
        self.r.log.append(PostRow(id="p", network="tiktok", status=PUBLISHED, at=iso(now - timedelta(minutes=30)), slot="x"))
        self.assertIn("ago", schedule.due(cfg, cfg.networks["tiktok"], now, self.r.log, st).reason)

    def test_a_slot_fills_once(self):
        cfg, st = self.r.cfg, self.seeded()
        now = et(2026, 11, 12, 11)
        v = schedule.due(cfg, cfg.networks["tiktok"], now, self.r.log, st)
        self.r.log.append(PostRow(id="p", network="tiktok", status=PENDING, at=iso(et(2026, 11, 12, 8, 30)), slot=v.slot.key))
        self.r.log.append(PostRow(id="p", network="tiktok", status=PUBLISHED, at=iso(et(2026, 11, 12, 8, 30)), slot=v.slot.key))
        self.assertEqual(schedule.due(cfg, cfg.networks["tiktok"], now, self.r.log, st).reason, "no open slot")

    def test_a_post_without_an_outcome_blocks_the_network(self):
        cfg, st = self.r.cfg, self.seeded()
        self.r.log.append(PostRow(id="p", network="tiktok", status=PENDING, at=iso(et(2026, 11, 11, 10)), slot="x"))
        self.assertIn("without an outcome", schedule.due(cfg, cfg.networks["tiktok"], et(2026, 11, 12, 11), self.r.log, st).reason)

    def test_stopped_network_stays_stopped(self):
        cfg, st = self.r.cfg, self.seeded()
        st.stopped["tiktok"] = "account restricted"
        self.assertIn("stopped", schedule.due(cfg, cfg.networks["tiktok"], et(2026, 11, 12, 11), self.r.log, st).reason)

    def test_a_missed_slot_is_caught_within_its_window_only(self):
        cfg, st = self.r.cfg, self.seeded()
        self.assertTrue(schedule.due(cfg, cfg.networks["instagram"], et(2026, 11, 12, 14, 30), self.r.log, st).due)
        self.assertEqual(schedule.due(cfg, cfg.networks["instagram"], et(2026, 11, 12, 23, 45), self.r.log, st).reason,
                         "no open slot")


class TestLog(TmpCase):
    def test_retry_keeps_the_item_and_the_slot(self):
        log = self.r.log
        log.append(PostRow(id="a", network="tiktok", status=PENDING, slot="s1"))
        log.append(PostRow(id="a", network="tiktok", status=RETRY, slot="s1"))
        self.assertNotIn("a", log.used_ids())
        self.assertNotIn("s1", log.filled_slots("tiktok"))

    def test_failed_consumes_the_slot_and_the_item(self):
        log = self.r.log
        log.append(PostRow(id="a", network="tiktok", status=PENDING, slot="s1"))
        log.append(PostRow(id="a", network="tiktok", status=FAILED, slot="s1"))
        self.assertIn("a", log.used_ids())
        self.assertIn("s1", log.filled_slots("tiktok"))
        self.assertEqual(log.sent_times("tiktok"), [])


class TestAdapters(unittest.TestCase):
    def media(self, tmp: Path, url="https://x/m/a.mp4") -> Media:
        f = tmp / "a.mp4"
        f.write_bytes(b"\0" * 1000)
        return Media(f, duration_s=15.0, public_url=url, cover_s=2.0)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def recording(self, cls, env=None):
        a = cls(None, {**cls.placeholder_env(), **(env or {})}, sleep=lambda s: None)
        a.transport = RecordingTransport(a.dry_run_responder)
        return a

    def test_buffer_tiktok_shares_now_by_url(self):
        a = self.recording(BufferTikTok, {"BUFFER_CHANNEL_TIKTOK": "ch1"})
        res = a.post(self.media(self.tmp), PostSpec(caption="hello"))
        body = a.transport.requests[0].json["variables"]["input"]
        self.assertEqual(body["mode"], "shareNow")
        self.assertEqual(body["assets"][0]["video"]["url"], "https://x/m/a.mp4")
        self.assertTrue(res.url.startswith("https://tiktok.dry.run"))

    def test_buffer_still_sending_is_handed_not_published(self):
        cls = BufferTikTok
        a = cls(None, {**cls.placeholder_env(), "BUFFER_CHANNEL_TIKTOK": "c"}, sleep=lambda s: None)

        def slow(q):
            if "createPost" in q.json["query"]:
                return HttpResponse.of_json(200, {"data": {"createPost": {"__typename": "PostActionSuccess", "post": {"id": "b9", "status": "sending"}}}})
            return HttpResponse.of_json(200, {"data": {"post": {"id": "b9", "status": "sending", "externalLink": None}}})

        a.transport = Stub(slow)
        self.assertEqual(a.post(self.media(self.tmp), PostSpec(caption="c")).state, "handed")

    def test_buffer_needs_a_public_url(self):
        a = self.recording(BufferTikTok, {"BUFFER_CHANNEL_TIKTOK": "ch1"})
        with self.assertRaises(Exception):
            a.post(self.media(self.tmp, url=""), PostSpec(caption="x"))

    def test_instagram_flags_realistic_ai_only(self):
        a = self.recording(InstagramDirect)
        a.post(self.media(self.tmp), PostSpec(caption="c", ai_generated=True, ai_realistic=True))
        self.assertEqual(a.transport.requests[0].params["is_ai_generated"], "true")
        b = self.recording(InstagramDirect)
        b.post(self.media(self.tmp), PostSpec(caption="c", ai_generated=True, ai_realistic=False))
        self.assertNotIn("is_ai_generated", b.transport.requests[0].params)

    def test_instagram_trial_reel(self):
        a = self.recording(InstagramDirect)
        a.post(self.media(self.tmp), PostSpec(caption="c", trial=True))
        p = a.transport.requests[0].params
        self.assertEqual(p["share_to_feed"], "false")
        self.assertIn("graduation_strategy", p["trial_params"])

    def test_plan_redacts_the_token(self):
        a = self.recording(InstagramDirect)
        a.post(self.media(self.tmp), PostSpec(caption="c"))
        self.assertNotIn("DRY_RUN_INSTAGRAM_ACCESS_TOKEN", json.dumps(a.transport.plan()))

    def test_youtube_uploads_with_the_disclosure(self):
        a = self.recording(YouTubeDirect)
        res = a.post(self.media(self.tmp), PostSpec(caption="c", title="A title", ai_generated=True, ai_realistic=True))
        init = next(r for r in a.transport.requests if r.params and r.params.get("uploadType") == "resumable")
        self.assertTrue(init.json["status"]["containsSyntheticMedia"])
        self.assertEqual(init.json["status"]["privacyStatus"], "public")
        self.assertEqual(res.url, "https://www.youtube.com/shorts/dryRunVid01")

    def test_youtube_needs_a_title(self):
        a = self.recording(YouTubeDirect)
        with self.assertRaises(Exception):
            a.post(self.media(self.tmp), PostSpec())

    def test_tiktok_inbox_is_a_draft(self):
        a = self.recording(TikTokInbox)
        self.assertEqual(a.post(self.media(self.tmp), PostSpec(caption="c")).state, "drafted")

    def test_x_signature_is_stable(self):
        keys = {"X_API_KEY": "k", "X_API_KEY_SECRET": "s", "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts"}
        h = oauth1_header("POST", "https://api.x.com/2/tweets", keys, nonce="n", timestamp="1")
        self.assertIn('oauth_signature="', h)
        self.assertEqual(h, oauth1_header("POST", "https://api.x.com/2/tweets", keys, nonce="n", timestamp="1"))

    def test_x_text_needs_text(self):
        a = self.recording(XDirect)
        with self.assertRaises(Exception):
            a.post(Media(Path(""), kind="text"), PostSpec())

    def test_buffer_x_posts_text(self):
        a = self.recording(BufferX, {"BUFFER_CHANNEL_X": "c"})
        a.post(Media(Path(""), kind="text"), PostSpec(caption="hello"))
        self.assertEqual(a.transport.requests[0].json["variables"]["input"]["assets"], [])

    def test_redact_text(self):
        self.assertEqual(redact_text("token ya29.abcdefghijklmnopqrstuvwxyz"), "token [redacted]")


class TestIntakeRules(TmpCase):
    GOOD = {"width": 1080, "height": 1920, "duration_s": 20.0, "video_codec": "h264", "pix_fmt": "yuv420p", "fps": 30.0,
            "audio_codec": "aac", "bytes": 5_000_000}

    def rules(self):
        return self.r.cfg.intake

    def test_good_media(self):
        self.assertEqual(intake.media_problems(self.GOOD, "tiktok", self.rules()), ([], []))

    def test_bad_media(self):
        for change, needle in (({"width": 1920, "height": 1080}, "aspect"), ({"duration_s": 200.0}, "length"),
                               ({"video_codec": "vp9"}, "codec"), ({"pix_fmt": "yuv444p"}, "pixel"),
                               ({"bytes": 200_000_000}, "MB"), ({"audio_codec": "mp3"}, "audio")):
            e, _ = intake.media_problems({**self.GOOD, **change}, "tiktok", self.rules())
            self.assertTrue(any(needle in x for x in e), (change, e))

    def test_silent_is_a_warning(self):
        e, w = intake.media_problems({**self.GOOD, "audio_codec": ""}, "tiktok", self.rules())
        self.assertEqual(e, [])
        self.assertTrue(w)

    def item(self, **over):
        return Item(Path("x.json"), {"id": "a", "network": over.pop("network", "tiktok"), **over})

    def test_captions(self):
        rules = self.rules()
        self.assertEqual(intake.caption_problems(self.item(caption="a fine caption"), [], rules), [])
        self.assertTrue(intake.caption_problems(self.item(caption="part 3 of the series"), [], rules))
        self.assertTrue(intake.caption_problems(self.item(caption="watch #12 now"), [], rules))
        self.assertTrue(intake.caption_problems(self.item(caption="3/10 in the list"), [], rules))
        self.assertTrue(intake.caption_problems(self.item(caption="#a #b #c #d"), [], rules))
        self.assertTrue(intake.caption_problems(self.item(caption="the same words"), ["The same, words!"], rules))

    def test_youtube_needs_a_title(self):
        self.assertTrue(intake.caption_problems(self.item(network="youtube", caption="c"), [], self.rules()))
        self.assertEqual(intake.caption_problems(self.item(network="youtube", caption="c", title="A title"), [], self.rules()), [])

    def test_shape(self):
        self.assertTrue(shape_problems({"id": "x"}))
        good = {"id": "abc", "network": "tiktok", "media": {"file": "a.mp4", "sha256": SHA}, "format": "f", "caption": "",
                "ai": {"generated": False}}
        self.assertEqual(shape_problems(good, "tiktok"), [])
        self.assertTrue(shape_problems(good, "youtube"))


class TestPhash(unittest.TestCase):
    def test_dhash_detects_order(self):
        rising = bytes(range(72))
        self.assertEqual(phash.dhash(rising), 0)
        falling = bytes(range(72, 0, -1))
        self.assertEqual(phash.dhash(falling), 2 ** 64 - 1)

    def test_distance(self):
        self.assertEqual(phash.distance(["0" * 16], ["0" * 16]), 0)
        self.assertEqual(phash.distance(["f" * 16], ["0" * 16]), 64)
        self.assertEqual(phash.distance([], ["0" * 16]), 64)
        self.assertEqual(phash.nearest(["0" * 16], {"a": ["f" * 16], "b": ["0" * 16]}), ("b", 0.0))


def answers(req: Request, *, buffer_status="sent", ig=None, pages_ok=True) -> HttpResponse:
    if req.method == "HEAD":
        return HttpResponse(200 if pages_ok else 404)
    if "api.buffer.com" in req.url:
        q = req.json["query"]
        if "createPost" in q:
            return HttpResponse.of_json(200, {"data": {"createPost": {
                "__typename": "PostActionSuccess", "post": {"id": "b1", "status": buffer_status, "externalLink": "https://www.tiktok.com/@x/video/1"}}}})
        return HttpResponse.of_json(200, {"data": {}})
    return HttpResponse.of_json(404, {})


class TestPublishRun(TmpCase):
    def seed(self):
        st = GuardState(self.r.state / "state.json", first_post={n: "2026-01-01" for n in ("tiktok", "instagram", "youtube")})
        st.save()

    def go(self, transport=None, live=True, now=None, env=None):
        return publish.run(self.r.dir, self.r.state, self.r.cfg, {**self.r.env, "BUFFER_API_KEY": "k",
                                                                   "BUFFER_CHANNEL_TIKTOK": "c", **(env or {})},
                           now=now or et(2026, 11, 12, 11), live=live, out=self.r.out, only="tiktok", transport=transport,
                           wait=False, sleep=lambda s: None)

    def test_dry_run_sends_nothing_and_keeps_the_real_log_clean(self):
        self.seed()
        self.r.add()
        res = self.go(live=False)
        self.assertEqual(res[0].action, "dry-run")
        self.assertFalse(self.r.log.path.exists())
        self.assertTrue((self.r.out / "plan-tiktok.json").is_file())
        self.assertTrue((self.r.out / "dry-posts.csv").is_file())

    def test_a_network_not_approved_stays_a_dry_run(self):
        self.seed()
        self.r.add()
        self.r.env["SOCIAL_LIVE_NETWORKS"] = "youtube"
        stub = Stub(lambda q: answers(q))
        res = self.go(stub)
        self.assertEqual(res[0].action, "dry-run")
        self.assertEqual(stub.requests, [])

    def test_live_post_writes_pending_first(self):
        self.seed()
        self.r.add()
        statuses = []
        real = PostLog.append

        def spy(log, row):
            statuses.append(row.status)
            real(log, row)

        PostLog.append = spy
        self.addCleanup(lambda: setattr(PostLog, "append", real))
        res = self.go(Stub(lambda q: answers(q)))
        self.assertEqual(res[0].action, "posted")
        self.assertEqual(statuses, [PENDING, PUBLISHED])
        self.assertEqual(GuardState.load(self.r.state / "state.json").first_post["tiktok"], "2026-01-01")

    def test_pages_not_serving_is_a_retry(self):
        self.seed()
        self.r.add()
        res = self.go(Stub(lambda q: answers(q, pages_ok=False)))
        self.assertEqual(res[0].action, "retry")
        self.assertNotIn("post-001", self.r.log.used_ids())

    def failing(self, message: str):
        self.seed()
        self.r.add()

        def fn(q):
            if q.method == "HEAD":
                return HttpResponse(200)
            if "api.github.com" in q.url:
                return HttpResponse(204)
            return HttpResponse.of_json(200, {"errors": [{"message": message}]})

        self.r.env["COS_EVENT_TOKEN"] = "tok"
        stub = Stub(fn)
        return self.go(stub, env={"COS_EVENT_TOKEN": "tok"}), stub

    def test_a_limit_error_trips_the_brake(self):
        res, stub = self.failing("429 too many requests")
        self.assertEqual(res[0].action, "failed")
        st = GuardState.load(self.r.state / "state.json")
        self.assertTrue(st.braked("tiktok", et(2026, 11, 12, 12)))
        self.assertTrue(any("api.github.com" in q.url for q in stub.requests))

    def test_a_restricted_account_stops_the_network(self):
        self.failing("this account has been suspended")
        st = GuardState.load(self.r.state / "state.json")
        self.assertIn("tiktok", st.stopped)

    def test_a_failure_uses_one_item_and_one_slot_only(self):
        res, _ = self.failing("something odd")
        self.r.add(id_="post-002")
        again = self.go(Stub(lambda q: answers(q)))
        self.assertEqual(again[0].action, "skipped")
        self.assertNotIn("post-002", self.r.log.used_ids())

    def test_secrets_missing_never_posts(self):
        self.seed()
        self.r.add(network="youtube")
        res = publish.run(self.r.dir, self.r.state, self.r.cfg, {**self.r.env, "COS_EVENT_TOKEN": "t"},
                          now=et(2026, 11, 12, 11, 50), live=True, out=self.r.out, only="youtube",
                          transport=Stub(lambda q: HttpResponse(204)), wait=False)
        self.assertEqual(res[0].action, "skipped")
        self.assertIn("missing", res[0].detail)

    def test_empty_queue_goes_quiet(self):
        self.seed()
        self.assertIn("empty", self.go(Stub(lambda q: answers(q)))[0].detail)

    def test_a_crash_mid_post_blocks_a_repost(self):
        self.seed()
        self.r.add()

        def boom(q):
            if q.method == "HEAD":
                return HttpResponse(200)
            raise SystemExit("the runner died")

        with self.assertRaises(SystemExit):
            self.go(Stub(boom))
        self.assertEqual([r["status"] for r in self.r.log.rows()], [PENDING])
        self.r.add(id_="post-002")
        v = self.go(Stub(lambda q: answers(q)))
        self.assertIn("without an outcome", v[0].detail)

    def test_earliest_is_respected(self):
        self.seed()
        self.r.add(earliest="2026-12-01T00:00:00Z")
        self.assertIn("empty", self.go(Stub(lambda q: answers(q)))[0].detail)
        self.assertEqual(ready(self.r.dir, "tiktok", set(), et(2026, 12, 2, 12))[0].id, "post-001")

    def test_unchecked_item_is_never_posted(self):
        self.seed()
        self.r.add(intake={"ok": False})
        self.assertIn("empty", self.go(Stub(lambda q: answers(q)))[0].detail)


class TestAlarms(TmpCase):
    def make(self, live=True, now=None):
        st = GuardState(self.r.state / "state.json")
        stub = Stub(lambda q: HttpResponse(204))
        return alarms_mod.Alarms(st, {"COS_EVENT_TOKEN": "t", "GITHUB_REPOSITORY": "o/s"}, live=live, company_repo="o/c",
                                 transport=stub, now=now or et(2026, 11, 12, 12)), stub, st

    def test_dispatch_payload(self):
        a, stub, _ = self.make()
        a.raise_("brake-tiktok", "title", "body", "high")
        q = stub.requests[0]
        self.assertTrue(q.url.endswith("/repos/o/c/dispatches"))
        self.assertEqual(q.json["event_type"], "cos-event")
        self.assertEqual(q.json["client_payload"]["type"], "social-alarm")
        self.assertEqual(q.json["client_payload"]["key"], "brake-tiktok")

    def test_not_resent_within_a_day(self):
        a, stub, st = self.make()
        a.raise_("k", "t", "b")
        a.raise_("k", "t", "b")
        self.assertEqual(len(stub.requests), 1)
        b, stub2, _ = self.make(now=et(2026, 11, 13, 13))
        b.state = st
        b.raise_("k", "t", "b")
        self.assertEqual(len(stub2.requests), 1)

    def test_clear_only_when_open(self):
        a, stub, _ = self.make()
        a.clear("k", "n")
        self.assertEqual(stub.requests, [])
        a.raise_("k", "t", "b")
        a.clear("k", "n")
        self.assertEqual(stub.requests[-1].json["client_payload"]["type"], "social-clear")

    def test_a_dry_run_only_records(self):
        a, stub, st = self.make(live=False)
        a.raise_("k", "t", "b")
        self.assertEqual(stub.requests, [])
        self.assertTrue(a.sent[0]["dry_run"])
        self.assertEqual(st.alarms, {})


class TestWatch(TmpCase):
    def test_runway_and_heartbeat(self):
        st = GuardState(self.r.state / "state.json")
        a = alarms_mod.Alarms(st, {"COS_EVENT_TOKEN": "t"}, live=True, company_repo="o/c",
                              transport=Stub(lambda q: HttpResponse(204)), now=et(2026, 11, 12, 12))
        rep = watch.check(self.r.dir, self.r.state, self.r.cfg, a, et(2026, 11, 12, 12))
        keys = {x["key"] for x in a.sent}
        self.assertIn("runway-tiktok", keys)
        self.assertIn("heartbeat", keys)
        self.assertEqual(rep["runway"]["tiktok"]["days"], 0)

    def test_a_full_runway_and_fresh_heartbeat_clear(self):
        for i in range(30):
            self.r.add(id_=f"t-{i:03d}")
            self.r.add(network="instagram", id_=f"i-{i:03d}")
            self.r.add(network="youtube", id_=f"y-{i:03d}")
        hb = self.r.dir / "pc"
        hb.mkdir()
        (hb / "heartbeat.json").write_text(json.dumps({"at": iso(et(2026, 11, 12, 3))}), encoding="utf-8")
        st = GuardState(self.r.state / "state.json")
        st.alarms["runway-tiktok"] = {"title": "x", "sent_at": iso(et(2026, 11, 10, 3)), "cleared": False}
        a = alarms_mod.Alarms(st, {"COS_EVENT_TOKEN": "t"}, live=True, company_repo="o/c",
                              transport=Stub(lambda q: HttpResponse(204)), now=et(2026, 11, 12, 12))
        watch.check(self.r.dir, self.r.state, self.r.cfg, a, et(2026, 11, 12, 12))
        self.assertEqual([x["type"] for x in a.sent], ["social-clear"])

    def test_dry_run_raises_no_runway_alarm(self):
        r = Repo(env={"SOCIAL_CRON": "dry-run"})
        self.addCleanup(r.close)
        a = alarms_mod.Alarms(GuardState(r.state / "state.json"), {}, live=False, company_repo="o/c", now=et(2026, 11, 12, 12))
        watch.check(r.dir, r.state, r.cfg, a, et(2026, 11, 12, 12))
        self.assertEqual(a.sent, [])


class TestMetrics(TmpCase):
    def posted(self, network, rid, id_, fmt="hotlap", at=None):
        at = iso(at or et(2026, 11, 1, 12))
        self.r.log.append(PostRow(id=id_, network=network, status=PENDING, at=at, format=fmt, route="direct"))
        self.r.log.append(PostRow(id=id_, network=network, status=PUBLISHED, at=at, format=fmt, route="direct", remote_id=rid,
                                  url=f"https://{network}/{rid}"))

    def test_youtube_numbers_and_a_blank_for_a_hidden_like_count(self):
        self.posted("youtube", "v1", "a")

        def fn(q):
            if "oauth2" in q.url:
                return HttpResponse.of_json(200, {"access_token": "t"})
            if q.url.endswith("/videos"):
                return HttpResponse.of_json(200, {"items": [{"id": "v1", "statistics": {"viewCount": "120"}, "status": {"uploadStatus": "processed"}}]})
            if "youtubeanalytics" in q.url:
                return HttpResponse.of_json(200, {"columnHeaders": [{"name": n} for n in ("video", "views", "averageViewDuration", "shares", "subscribersGained")],
                                                  "rows": [["v1", 120, 9.5, 3, 2]]})
            return HttpResponse.of_json(200, {"items": [{"statistics": {"subscriberCount": "40"}}]})

        env = {"YOUTUBE_CLIENT_ID": "i", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r"}
        col = metrics.collect(self.r.state, self.r.cfg, env, now=et(2026, 11, 12, 12), live=True, transport=Stub(fn))
        p = col.posts[0]
        self.assertEqual((p.views, p.likes, p.avg_view_s, p.follows), (120, None, 9.5, 2))
        metrics.write(self.r.state, col, et(2026, 11, 12, 12))
        row = metrics.read_csv(self.r.state / "metrics" / "posts.csv")[0]
        self.assertEqual(row["likes"], "")
        self.assertEqual(row["views"], "120")

    def test_a_removed_video_is_caught(self):
        self.posted("youtube", "v1", "a")

        def fn(q):
            if "oauth2" in q.url:
                return HttpResponse.of_json(200, {"access_token": "t"})
            if q.url.endswith("/videos"):
                return HttpResponse.of_json(200, {"items": []})
            if "youtubeanalytics" in q.url:
                return HttpResponse.of_json(200, {})
            return HttpResponse.of_json(200, {"items": [{"statistics": {"subscriberCount": "40"}}]})

        env = {"YOUTUBE_CLIENT_ID": "i", "YOUTUBE_CLIENT_SECRET": "s", "YOUTUBE_REFRESH_TOKEN": "r", "COS_EVENT_TOKEN": "t"}
        now = et(2026, 11, 12, 12)
        col = metrics.collect(self.r.state, self.r.cfg, env, now=now, live=True, transport=Stub(fn))
        self.assertEqual(col.removed[0][:2], ("youtube", "v1"))
        st = GuardState.load(self.r.state / "state.json")
        a = alarms_mod.Alarms(st, env, live=True, company_repo="o/c", transport=Stub(lambda q: HttpResponse(204)), now=now)
        metrics.react(self.r.state, self.r.cfg, col, a, now)
        self.assertEqual(self.r.log.latest()[("a", "youtube")]["status"], "removed")
        self.assertEqual(a.sent[0]["key"], "removed-youtube-v1")

    def test_instagram_insights(self):
        self.posted("instagram", "m1", "a")

        def fn(q):
            if q.url.endswith("/insights"):
                return HttpResponse.of_json(200, {"data": [{"name": "views", "values": [{"value": 50}]}, {"name": "saved", "values": [{"value": 4}]}]})
            return HttpResponse.of_json(200, {"followers_count": 7})

        col = metrics.collect(self.r.state, self.r.cfg, {"INSTAGRAM_ACCESS_TOKEN": "t"}, now=et(2026, 11, 12, 12), live=True, transport=Stub(fn))
        self.assertEqual((col.posts[0].views, col.posts[0].saves, col.posts[0].likes), (50, 4, None))
        self.assertEqual(col.accounts[0]["followers"], 7)

    def test_a_live_network_with_no_numbers_turns_red(self):
        self.posted("instagram", "m1", "a")

        def fn(q):
            return HttpResponse.of_json(500, {})

        col = metrics.collect(self.r.state, self.r.cfg, {"INSTAGRAM_ACCESS_TOKEN": "t"}, now=et(2026, 11, 12, 12), live=True, transport=Stub(fn))
        self.assertTrue(col.problems)

    def test_buffer_zeroes_are_not_numbers(self):
        rows = {"tiktok": [{"remote_id": "b1", "id": "a", "format": "f", "posted_at": "x", "url": ""},
                           {"remote_id": "b2", "id": "b", "format": "f", "posted_at": "x", "url": ""}]}
        col = metrics.Collection()
        nodes = [{"id": "b1", "channelService": "tiktok", "externalLink": "u1", "metricsUpdatedAt": "t",
                  "metrics": [{"type": "views", "value": 0}, {"type": "likes", "value": 0}]},
                 {"id": "b2", "channelService": "tiktok", "externalLink": "u2", "metricsUpdatedAt": "t",
                  "metrics": [{"type": "views", "value": 40}]}]
        links = metrics.collect_buffer(nodes, rows, col)
        self.assertEqual([p.remote_id for p in col.posts], ["b2"])
        self.assertEqual(links, {"b1": "u1", "b2": "u2"})

    def test_tiktok_page_parsers(self):
        page = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">' + json.dumps(
            {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": {"id": "9", "stats": {"playCount": 7, "diggCount": 1}}}}}}) + "</script>"
        self.assertEqual(metrics.tiktok_video(page, "9")["views"], 7)
        self.assertIsNone(metrics.tiktok_video(page, "8"))

    def board(self, per_format):
        rows = []
        for fmt, views in per_format.items():
            for i, v in enumerate(views):
                rows.append({"network": "tiktok", "remote_id": f"{fmt}{i}", "views": str(v), "format": fmt, "posted_at": f"2026-11-{i + 1:02d}"})
        return metrics.scoreboard(rows)["networks"]["tiktok"]

    def test_handed_posts_are_reconciled_with_the_network_link(self):
        for rid, id_ in (("b1", "a"), ("b2", "b"), ("b3", "c")):
            at = iso(et(2026, 11, 12, 10))
            self.r.log.append(PostRow(id=id_, network="tiktok", status=PENDING, at=at, route="buffer"))
            self.r.log.append(PostRow(id=id_, network="tiktok", status="handed", at=at, route="buffer", remote_id=rid, url=f"buffer:{rid}"))
        answers_ = {"b1": {"status": "sent", "externalLink": "https://www.tiktok.com/@x/video/5"},
                    "b2": {"status": "error", "error": {"message": "video too short"}}, "b3": {"status": "sending"}}

        def fn(q):
            if "api.buffer.com" in q.url and "PostStatus" in q.json["query"]:
                return HttpResponse.of_json(200, {"data": {"post": {"id": q.json["variables"]["id"], **answers_[q.json["variables"]["id"]]}}})
            return HttpResponse.of_json(200, {"data": {"account": {"organizations": [{"id": "o"}]}, "posts": {"edges": [], "pageInfo": {"hasNextPage": False}}}})

        col = metrics.collect(self.r.state, self.r.cfg, {"BUFFER_API_KEY": "k"}, now=et(2026, 11, 13, 12), live=True, transport=Stub(fn))
        latest = self.r.log.latest()
        self.assertEqual(latest[("a", "tiktok")]["status"], "published")
        self.assertEqual(latest[("a", "tiktok")]["url"], "https://www.tiktok.com/@x/video/5")
        self.assertEqual(latest[("b", "tiktok")]["status"], "failed")
        self.assertEqual(latest[("c", "tiktok")]["status"], "handed")
        self.assertTrue(any("still with Buffer" in p for p in col.problems))

    def test_scoreboard_kills_a_flat_format_after_ten(self):
        b = self.board({"flat": [100] * 10, "good": [100, 120, 150, 130, 110, 100, 90, 160, 170, 900]})
        self.assertEqual(b["formats"]["flat"]["state"], "kill")
        self.assertEqual(b["formats"]["good"]["state"], "double")

    def test_scoreboard_stays_trial_before_ten(self):
        self.assertEqual(self.board({"new": [100, 110]})["formats"]["new"]["state"], "trial")

    def test_blank_views_do_not_count(self):
        rows = [{"network": "tiktok", "remote_id": "a", "views": "", "format": "f", "posted_at": "1"},
                {"network": "tiktok", "remote_id": "b", "views": "10", "format": "f", "posted_at": "2"}]
        b = metrics.scoreboard(rows)["networks"]["tiktok"]
        self.assertEqual((b["posts_with_numbers"], b["floor"]), (1, 10))

    def test_reach_halving_trips_the_brake_and_a_breakout_wakes_the_loops(self):
        now = et(2026, 11, 30, 12)
        col = metrics.Collection()
        for i in range(20):
            col.posts.append(metrics.PostNumbers("tiktok", f"r{i}", f"u{i}", f"i{i}", "f", f"2026-11-{i + 1:02d}", views=1000))
        for i in range(5):
            col.posts.append(metrics.PostNumbers("tiktok", f"s{i}", f"u{i}", f"j{i}", "f", f"2026-11-{21 + i:02d}", views=200))
        metrics.write(self.r.state, col, now)
        st = GuardState.load(self.r.state / "state.json")
        a = alarms_mod.Alarms(st, {"COS_EVENT_TOKEN": "t"}, live=True, company_repo="o/c", transport=Stub(lambda q: HttpResponse(204)), now=now)
        metrics.react(self.r.state, self.r.cfg, metrics.Collection(), a, now)
        self.assertTrue(st.braked("tiktok", now))
        rows = [metrics.asdict(metrics.PostNumbers("tiktok", "z", "u", "k", "f", "2026-11-29", views=90000))]
        metrics.append_csv(self.r.state / "metrics" / "posts.csv", metrics.POST_FIELDS, [{"ts_utc": iso(now), **r} for r in rows])
        metrics.react(self.r.state, self.r.cfg, metrics.Collection(), a, now)
        self.assertTrue(any(x["type"] == "metric-social-breakout" for x in a.sent))
        before = len(a.sent)
        metrics.react(self.r.state, self.r.cfg, metrics.Collection(), a, now)
        self.assertEqual(len([x for x in a.sent[before:] if x["type"] == "metric-social-breakout"]), 0)


class TestPages(TmpCase):
    def test_runway_keeps_unposted_and_recent(self):
        now = et(2026, 11, 12, 12)
        src = self.r.dir / "src"
        src.mkdir()
        for name in ("new", "recent", "old"):
            self.r.add(id_=name)
            (src / f"{name}.mp4").write_bytes(b"x" * 10)
        for name, at in (("recent", now - timedelta(hours=5)), ("old", now - timedelta(days=4))):
            self.r.log.append(PostRow(id=name, network="tiktok", status=PENDING, at=iso(at)))
            self.r.log.append(PostRow(id=name, network="tiktok", status=PUBLISHED, at=iso(at)))
        rep = pages.build(self.r.dir, self.r.state, self.r.dir / "site", lambda f: src / f, now)
        self.assertEqual(rep["files"], 2)
        self.assertFalse((self.r.dir / "site" / "m" / "old.mp4").exists())


FFMPEG = phash.tool("ffmpeg")
FFPROBE = phash.tool("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "needs ffmpeg and ffprobe (SOCIAL_FFMPEG / SOCIAL_FFPROBE or on PATH)")
class TestIntakeEndToEnd(TmpCase):
    def clip(self, name: str, source: str = "testsrc2", size="1080x1920", extra=()) -> Path:
        out = self.r.dir / "media" / name
        out.parent.mkdir(exist_ok=True)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"{source}=size={size}:rate=30:duration=4",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "4", *extra, "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)], check=True, capture_output=True)
        return out

    def enqueue(self, path: Path, id_: str, **over):
        self.r.add(id_=id_, media={"file": path.name, "sha256": intake.sha256(path), "kind": "video"}, intake=None, **over)

    def fetch(self, name: str) -> Path:
        return self.r.dir / "media" / name

    def run_intake(self):
        return intake.check(self.r.dir, self.r.cfg, self.fetch, et(2026, 11, 12, 12))

    def test_accepts_a_good_clip_and_records_the_hash(self):
        self.enqueue(self.clip("a.mp4"), "a-001", caption="first caption")
        rep = self.run_intake()
        self.assertEqual(rep.accepted, ["tiktok/a-001"])
        item = items(self.r.dir)[0]
        self.assertTrue(item.checked)
        self.assertEqual(len(item.data["intake"]["phash"]), 5)

    def test_quarantines_a_landscape_clip(self):
        self.enqueue(self.clip("wide.mp4", size="1920x1080"), "w-001", caption="wide")
        rep = self.run_intake()
        self.assertIn("tiktok/w-001", rep.quarantined)
        self.assertTrue((self.r.dir / "quarantine" / "tiktok" / "w-001.json").is_file())
        self.assertEqual(items(self.r.dir), [])

    def test_catches_a_near_duplicate_with_a_new_border(self):
        self.enqueue(self.clip("a.mp4"), "a-001", caption="first caption")
        self.assertTrue(self.run_intake().accepted)
        self.enqueue(self.clip("b.mp4", extra=("-vf", "drawbox=x=0:y=0:w=iw:h=20:color=black@0.2:t=fill")), "b-002", caption="another caption")
        rep = self.run_intake()
        self.assertIn("tiktok/b-002", rep.quarantined)
        self.assertIn("near-duplicate", rep.quarantined["tiktok/b-002"][0])

    def test_passes_different_content(self):
        self.enqueue(self.clip("a.mp4"), "a-001", caption="first caption")
        self.run_intake()
        self.enqueue(self.clip("c.mp4", source="smptebars"), "c-003", caption="another caption")
        self.assertEqual(self.run_intake().accepted, ["tiktok/c-003"])

    def test_same_clip_may_go_to_another_network(self):
        p = self.clip("a.mp4")
        self.enqueue(p, "a-001", caption="caption one")
        self.r.add(network="youtube", id_="a-yt", media={"file": p.name, "sha256": intake.sha256(p), "kind": "video"},
                   intake=None, caption="caption two", title="a title")
        self.assertEqual(sorted(self.run_intake().accepted), ["tiktok/a-001", "youtube/a-yt"])

    def test_refuses_an_unmarked_ai_file(self):
        self.enqueue(self.clip("a.mp4"), "a-001", caption="c", ai={"generated": True, "realistic": False})
        rep = self.run_intake()
        self.assertIn("C2PA", rep.quarantined["tiktok/a-001"][0])

    def test_refuses_a_file_that_is_not_the_one_rendered(self):
        p = self.clip("a.mp4")
        self.r.add(id_="a-001", media={"file": p.name, "sha256": SHA, "kind": "video"}, intake=None)
        self.assertIn("sha256", self.run_intake().quarantined["tiktok/a-001"][0])


class TestCli(TmpCase):
    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "social", "--root", str(self.r.dir), *args], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parents[1], env={**os.environ, **self.r.env, **(env or {})})

    def test_validate(self):
        r = self.cli("validate")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_validate_catches_a_bad_row(self):
        self.r.add(id_="x")
        r = self.cli("validate")
        self.assertEqual(r.returncode, 1)

    def test_resolve_settles_a_post_with_no_outcome(self):
        self.r.log.append(PostRow(id="p", network="tiktok", status=PENDING, at=iso(et(2026, 11, 12, 10)), slot="s"))
        r = self.cli("resolve", "tiktok", "p", "--url", "https://www.tiktok.com/@x/video/1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.r.log.latest()[("p", "tiktok")]["status"], PUBLISHED)
        self.assertEqual(self.r.log.unresolved(), [])

    def test_resume(self):
        st = GuardState(self.r.state / "state.json", stopped={"tiktok": "suspended"})
        st.save()
        self.assertEqual(self.cli("resume", "tiktok", "--reason", "Carlos checked").returncode, 0)
        self.assertEqual(GuardState.load(self.r.state / "state.json").stopped, {})


if __name__ == "__main__":
    unittest.main()
