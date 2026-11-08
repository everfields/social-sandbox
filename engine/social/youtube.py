"""YouTube Shorts through the YouTube Data API, directly: refresh token -> resumable upload -> status.

New in the engine. Since 2026-11-02, uploads from unaudited API projects are no longer forced private, and the API
allows 100 uploads a day. `status.containsSyntheticMedia` carries the AI disclosure. The OAuth client must be
"In production": a "Testing" client's refresh token dies after 7 days. A vertical clip of 3 minutes or less is a Short.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .base import PROCESSING, PUBLISHED, VIDEO, Adapter, Media, PostResult, PostSpec, PublisherError
from .http import HttpResponse, Request

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
CATEGORY = "22"  # People & Blogs; a format may set its own
STATUS_POLLS = 6
STATUS_POLL_S = 10.0
SCOPES = ("https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
          "https://www.googleapis.com/auth/yt-analytics.readonly")


def access_token(adapter: Adapter) -> str:
    body = adapter.send(Request("POST", TOKEN_URL, form={
        "client_id": adapter.var("YOUTUBE_CLIENT_ID"),
        "client_secret": adapter.var("YOUTUBE_CLIENT_SECRET"),
        "refresh_token": adapter.var("YOUTUBE_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    })).json()
    if "access_token" not in body:
        raise PublisherError(f"youtube: token refresh failed: {body.get('error')}")
    return body["access_token"]


class YouTubeDirect(Adapter):
    name = "youtube"
    route = "direct"
    kinds = frozenset({VIDEO})
    env_sets = (("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"),)
    cannot = ("Shorts up to 3 minutes, vertical. 100 uploads a day per project (10,000 quota units, 100 each since "
              "2026). A Testing-mode OAuth client's refresh token dies in 7 days: keep the client In production.")

    def _post(self, media: Media, spec: PostSpec) -> PostResult:
        if media.duration_s and media.duration_s > 180:
            raise PublisherError(f"youtube: {media.duration_s:.0f}s is longer than a Short (180s)")
        title = (spec.title or spec.caption or "").strip()
        if not title:
            raise PublisherError("youtube: a Short needs a title")
        token = access_token(self)
        auth = {"Authorization": f"Bearer {token}"}
        body = {
            "snippet": {"title": title[:100], "description": spec.caption[:4900],
                        "tags": list(spec.tags)[:15], "categoryId": spec.options.get("category", CATEGORY),
                        "defaultLanguage": spec.language, "defaultAudioLanguage": spec.language},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": bool(spec.ai_generated and spec.ai_realistic)},
        }
        init = self.send(Request("POST", UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                                 headers={**auth, "X-Upload-Content-Length": str(media.size),
                                          "X-Upload-Content-Type": media.mime}, json=body))
        session = init.header("Location")
        if not session:
            raise PublisherError("youtube: no upload session in the response")
        video = self.send(Request("PUT", session, headers={**auth, "Content-Type": media.mime},
                                  data=media.read())).json()
        vid = video.get("id")
        if not vid:
            raise PublisherError("youtube: the upload returned no video id")

        def fetch() -> dict | None:
            items = self.send(Request("GET", f"{API}/videos", headers=auth,
                                      params={"part": "status", "id": vid})).json().get("items") or []
            st = (items[0].get("status") if items else {}) or {}
            if st.get("uploadStatus") in ("rejected", "failed", "deleted"):
                raise PublisherError(f"youtube: upload {st.get('uploadStatus')}: "
                                     f"{st.get('rejectionReason') or st.get('failureReason') or ''}")
            return st if st.get("uploadStatus") == "processed" else None

        try:
            st = self.poll(fetch, attempts=STATUS_POLLS, interval=STATUS_POLL_S, what="processing")
            state = PUBLISHED if st.get("privacyStatus") == "public" else PROCESSING
            note = "" if state == PUBLISHED else f"privacy {st.get('privacyStatus')}"
        except PublisherError as e:
            if "not ready" not in str(e):
                raise
            state, note = PROCESSING, "uploaded; YouTube is still processing it"
        return PostResult(f"https://www.youtube.com/shorts/{vid}", vid, state, note)

    def dry_run_responder(self, req: Request) -> HttpResponse:
        parts = urlsplit(req.url)
        if req.url == TOKEN_URL:
            return HttpResponse.of_json(200, {"access_token": "ya29.dry", "expires_in": 3599})
        if parts.path.startswith("/upload/youtube/v3/videos") and req.method == "POST":
            return HttpResponse(200, {"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=dry"})
        if req.method == "PUT":
            return HttpResponse.of_json(200, {"id": "dryRunVid01", "status": {"uploadStatus": "uploaded"}})
        if parts.path.endswith("/videos") and req.method == "GET":
            return HttpResponse.of_json(200, {"items": [{"id": "dryRunVid01", "status": {
                "uploadStatus": "processed", "privacyStatus": "public"}}]})
        return HttpResponse.of_json(404, {"error": {"message": f"dry-run responder: unexpected {req.method} {req.url}"}})
