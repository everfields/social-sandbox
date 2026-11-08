"""Instagram Reels through the Instagram Login API, directly (create container -> poll -> publish -> permalink).

Ported from AntiSearch's `publisher/meta.py`, where it was built and tested but never carried live posts. Free on a
Business or Creator account; accounts with a role on the app get Standard Access with no App Review. Meta fetches the
clip from a public URL (the Pages runway). New here: the `is_ai_generated` flag (2026-06-22) and trial reels, which
only non-followers see (2025-12-03). Meta says a Development-mode app shows its posts only to role users, so the
first post is checked logged out before this route goes live (`social check-public`).
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from .base import VIDEO, Adapter, Media, PostResult, PostSpec, PublisherError
from .http import HttpResponse, Request

CONTAINER_POLLS = 30
CONTAINER_POLL_S = 10.0
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"


class InstagramDirect(Adapter):
    name = "instagram"
    route = "direct"
    kinds = frozenset({VIDEO})
    needs_public_url = True
    env_sets = (("INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN"),)
    optional_env = ("INSTAGRAM_GRAPH_VERSION",)
    cannot = ("Reels only, from a Business or Creator account. Meta fetches the clip from a public URL. The token lasts "
              "60 days and is refreshed weekly. 100 API posts per 24 hours (some pages still say 50).")

    @property
    def api(self) -> str:
        return f"https://graph.instagram.com/{self.var('INSTAGRAM_GRAPH_VERSION', 'v24.0')}"

    def params(self, media: Media, spec: PostSpec) -> dict[str, str]:
        p = {"media_type": "REELS", "video_url": media.public_url, "share_to_feed": "false" if spec.trial else "true"}
        if media.cover_s is not None:
            p["thumb_offset"] = str(max(0, int(media.cover_s * 1000)))
        if spec.caption:
            p["caption"] = spec.caption
        if spec.ai_generated and spec.ai_realistic:
            p["is_ai_generated"] = "true"
        if spec.trial:
            p["trial_params"] = json.dumps({"graduation_strategy": spec.options.get("graduation", "SS_PERFORMANCE")})
        return p

    def _post(self, media: Media, spec: PostSpec) -> PostResult:
        uid, token = self.var("INSTAGRAM_USER_ID"), self.var("INSTAGRAM_ACCESS_TOKEN")
        container = self.send(Request("POST", f"{self.api}/{uid}/media",
                                      params={**self.params(media, spec), "access_token": token})).json()["id"]

        def fetch() -> str | None:
            st = self.send(Request("GET", f"{self.api}/{container}",
                                   params={"fields": "status_code,status", "access_token": token})).json()
            code = st.get("status_code", "")
            if code == "FINISHED":
                return code
            if code in ("ERROR", "EXPIRED"):
                raise PublisherError(f"instagram: container {code}: {st.get('status', '')}")
            return None

        self.poll(fetch, attempts=CONTAINER_POLLS, interval=CONTAINER_POLL_S, what="media container")
        media_id = self.send(Request("POST", f"{self.api}/{uid}/media_publish",
                                     params={"creation_id": container, "access_token": token})).json()["id"]
        link = self.send(Request("GET", f"{self.api}/{media_id}",
                                 params={"fields": "permalink", "access_token": token})).json().get("permalink", "")
        return PostResult(link or f"instagram:{media_id}", str(media_id), note="trial reel" if spec.trial else "")

    def dry_run_responder(self, req: Request) -> HttpResponse:
        path = urlsplit(req.url).path
        if path.endswith("/media"):
            return HttpResponse.of_json(200, {"id": "17890000000000001"})
        if path.endswith("/media_publish"):
            return HttpResponse.of_json(200, {"id": "17890000000000002"})
        fields = (req.params or {}).get("fields", "")
        if req.method == "GET" and fields.startswith("status_code"):
            return HttpResponse.of_json(200, {"status_code": "FINISHED", "id": "17890000000000001"})
        if req.method == "GET":
            return HttpResponse.of_json(200, {"permalink": "https://www.instagram.com/reel/DRYRUN/"})
        return HttpResponse.of_json(404, {"error": {"message": f"dry-run responder: unexpected {req.method} {path}"}})


def refresh_token(adapter: Adapter) -> dict:
    """Extend the long-lived token by 60 days (it must be at least 24 h old and not expired). Returns the body:
    `access_token`, `expires_in`. The caller stores it as a secret and never prints it."""
    body = adapter.send(Request("GET", REFRESH_URL, params={
        "grant_type": "ig_refresh_token", "access_token": adapter.var("INSTAGRAM_ACCESS_TOKEN")})).json()
    if "access_token" not in body:
        raise PublisherError("instagram: token refresh returned no token")
    return body
