"""TikTok's own Content Posting API: the fallback only, as drafts a person posts from the app.

Ported from AntiSearch's `publisher/tiktok.py`. Until TikTok audits the app, direct posts are private, and the audit
rejects "a utility tool to help upload contents to the account(s) you or your team manages". So public TikToks go
through Buffer; this route uploads an inbox draft (scope video.upload), at most 5 waiting per day, and a person taps
Post on the phone. Refresh tokens last 365 days and may rotate: a rotation raises a warning to update the secret.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .base import DRAFTED, VIDEO, Adapter, Media, PostResult, PostSpec, PublisherError
from .http import HttpResponse, Request

API = "https://open.tiktokapis.com/v2"
SINGLE_CHUNK_MAX = 64 * 1024 * 1024
STATUS_POLLS = 40


class TikTokInbox(Adapter):
    name = "tiktok"
    route = "inbox"
    kinds = frozenset({VIDEO})
    env_sets = (("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN"),)
    cannot = ("Drafts only: a person must open TikTok and tap Post, at most 5 drafts waiting per 24 hours. Public "
              "direct posting needs TikTok's audit, which it refuses for tools that post to their owners' accounts.")

    def api(self, req: Request) -> dict:
        body = self.send(req).json()
        err = body.get("error") or {}
        if err.get("code") not in (None, "", "ok"):
            raise PublisherError(f"tiktok: {err.get('code')}: {err.get('message', '')}")
        return body.get("data") or {}

    def token(self) -> str:
        refresh = self.var("TIKTOK_REFRESH_TOKEN")
        body = self.send(Request("POST", f"{API}/oauth/token/", form={
            "client_key": self.var("TIKTOK_CLIENT_KEY"), "client_secret": self.var("TIKTOK_CLIENT_SECRET"),
            "grant_type": "refresh_token", "refresh_token": refresh,
        })).json()
        if "access_token" not in body:
            raise PublisherError(f"tiktok: token refresh failed: {body.get('error')}")
        if body.get("refresh_token") and body["refresh_token"] != refresh:
            self.warnings.append("tiktok rotated the refresh token: update the TIKTOK_REFRESH_TOKEN secret")
        return body["access_token"]

    def _post(self, media: Media, spec: PostSpec) -> PostResult:
        size = media.size
        if size > SINGLE_CHUNK_MAX:
            raise PublisherError(f"tiktok: {size} bytes needs a chunked upload; keep posts under 64 MB")
        headers = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json; charset=UTF-8"}
        init = self.api(Request("POST", f"{API}/post/publish/inbox/video/init/", headers=headers, json={
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}}))
        publish_id = init["publish_id"]
        self.send(Request("PUT", init["upload_url"], headers={
            "Content-Type": media.mime, "Content-Range": f"bytes 0-{size - 1}/{size}"}, data=media.read()))

        def fetch() -> dict | None:
            st = self.api(Request("POST", f"{API}/post/publish/status/fetch/", headers=headers,
                                  json={"publish_id": publish_id}))
            if st.get("status") == "FAILED":
                raise PublisherError(f"tiktok: upload failed: {st.get('fail_reason', '')}")
            return st if st.get("status") in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE") else None

        self.poll(fetch, attempts=STATUS_POLLS, interval=3.0, what="upload")
        return PostResult(f"tiktok-inbox:{publish_id}", publish_id, DRAFTED,
                          "draft in the TikTok inbox: open TikTok on the phone and tap Post")

    def dry_run_responder(self, req: Request) -> HttpResponse:
        parts = urlsplit(req.url)
        ok = {"error": {"code": "ok", "message": ""}}
        if parts.path == "/v2/oauth/token/":
            return HttpResponse.of_json(200, {"access_token": "act.dry", "expires_in": 86400,
                                              "refresh_token": "DRY_RUN_TIKTOK_REFRESH_TOKEN"})
        if parts.path.endswith("/video/init/"):
            return HttpResponse.of_json(200, {**ok, "data": {
                "publish_id": "v_inbox_file~v2.dry", "upload_url": "https://open-upload.tiktokapis.com/video/?upload_id=dry"}})
        if req.method == "PUT":
            return HttpResponse(201)
        if parts.path.endswith("/status/fetch/"):
            return HttpResponse.of_json(200, {**ok, "data": {"status": "SEND_TO_USER_INBOX"}})
        return HttpResponse.of_json(404, {"error": {"code": "not_found", "message": f"unexpected {req.method} {req.url}"}})
