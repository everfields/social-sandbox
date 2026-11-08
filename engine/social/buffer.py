"""TikTok, X, and the fallbacks for Instagram and YouTube, through Buffer's GraphQL API (free plan).

Ported from AntiSearch's `publisher/buffer.py`, which posted about 250 times at €0. The engine keeps everything that
matters (slots, captions, the post log); Buffer is only the authorised pipe. Each post is created at its slot with
`mode: shareNow`, so the free plan's 10-post queue never fills. Media go by public URL (the Pages runway).
"""

from __future__ import annotations

from .base import HANDED, TEXT, VIDEO, Adapter, Media, PostResult, PostSpec, PublisherError
from .http import HttpResponse, Request

API = "https://api.buffer.com"
STATUS_POLLS = 3  # create + 2 status reads per post keeps far under 1,000 requests per channel per 30 days
STATUS_POLL_S = 10.0

CREATE = """mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status externalLink } }
    ... on MutationError { message }
  }
}"""
STATUS = "query PostStatus($id: PostId!) { post(input: { id: $id }) { id status externalLink error { message } } }"
ORGS = "query Orgs { account { organizations { id } } }"
CHANNELS = "query Channels($org: OrganizationId!) { channels(input: { organizationId: $org }) { id name service } }"


class BufferRoute(Adapter):
    route = "buffer"
    service = ""
    kinds = frozenset({VIDEO})
    needs_public_url = True
    env_sets = (("BUFFER_API_KEY",),)

    @classmethod
    def channel_var(cls) -> str:
        return f"BUFFER_CHANNEL_{cls.name.upper()}"

    def gql(self, query: str, variables: dict | None = None) -> dict:
        body = self.send(Request("POST", API, headers={"Authorization": f"Bearer {self.var('BUFFER_API_KEY')}"},
                                 json={"query": query, "variables": variables or {}})).json()
        if body.get("errors"):
            raise PublisherError(f"buffer: {body['errors'][0].get('message', body['errors'])}")
        return body.get("data") or {}

    def channel(self) -> str:
        pinned = self.var(self.channel_var())
        if pinned:
            return pinned
        org = self.var("BUFFER_ORG_ID") or self.gql(ORGS)["account"]["organizations"][0]["id"]
        found = [c for c in self.gql(CHANNELS, {"org": org})["channels"] if c["service"] == self.service]
        if len(found) != 1:
            raise PublisherError(f"buffer: {len(found)} {self.service} channels connected; set {self.channel_var()}")
        return found[0]["id"]

    def metadata(self, spec: PostSpec) -> dict:
        return {}

    def asset(self, media: Media) -> dict:
        out: dict = {"url": media.public_url}
        if media.cover_s is not None:
            out["metadata"] = {"thumbnailOffset": max(0, int(media.cover_s * 1000))}
        return out

    def _post(self, media: Media, spec: PostSpec) -> PostResult:
        payload: dict = {
            "channelId": self.channel(),
            "text": spec.caption,
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": [] if media.kind == TEXT else [{"video": self.asset(media)}],
        }
        meta = self.metadata(spec)
        if meta:
            payload["metadata"] = {self.service: meta}
        result = self.gql(CREATE, {"input": payload})["createPost"]
        if result.get("__typename") != "PostActionSuccess":
            raise PublisherError(f"buffer: {result.get('message', result)}")
        post = result["post"]
        for i in range(STATUS_POLLS):
            if post.get("status") == "sent":
                return PostResult(post.get("externalLink") or f"buffer:{post['id']}", post["id"])
            if post.get("status") == "error":
                raise PublisherError(f"buffer: {self.service} rejected it: {(post.get('error') or {}).get('message')}")
            if i < STATUS_POLLS - 1:
                self.sleep(STATUS_POLL_S)
                post = self.gql(STATUS, {"id": post["id"]})["post"]
        return PostResult(f"buffer:{post['id']}", post["id"], HANDED,
                          note=f"handed to Buffer, status {post.get('status')}; the metrics run reconciles it")

    def dry_run_responder(self, req: Request) -> HttpResponse:
        query = (req.json or {}).get("query", "")
        if "createPost" in query:
            return HttpResponse.of_json(200, {"data": {"createPost": {
                "__typename": "PostActionSuccess", "post": {"id": "dry-post", "status": "sending", "externalLink": None}}}})
        if "PostStatus" in query:
            return HttpResponse.of_json(200, {"data": {"post": {
                "id": "dry-post", "status": "sent", "externalLink": f"https://{self.service}.dry.run/p/dry", "error": None}}})
        if "Orgs" in query:
            return HttpResponse.of_json(200, {"data": {"account": {"organizations": [{"id": "org-dry"}]}}})
        if "Channels" in query:
            return HttpResponse.of_json(200, {"data": {"channels": [
                {"id": f"ch-{s}", "name": s, "service": s} for s in ("tiktok", "twitter", "instagram", "youtube")]}})
        return HttpResponse.of_json(400, {"errors": [{"message": "dry-run responder: unexpected query"}]})


class BufferTikTok(BufferRoute):
    name = "tiktok"
    service = "tiktok"
    cannot = ("Public posts through Buffer's audited TikTok app; trending sounds and polls need the phone. If Buffer "
              "offers only reminders, switch the TikTok account to Business (free, in the app).")


class BufferInstagram(BufferRoute):
    """The fallback until a direct test post shows up for a logged-out viewer. No AI flag on this route."""

    name = "instagram"
    service = "instagram"
    cannot = "Reels through Buffer: no is_ai_generated flag, no trial reels, no insights back. Music or stickers turn it into a reminder."

    def metadata(self, spec: PostSpec) -> dict:
        return {"type": "reel", "shouldShareToFeed": True}


class BufferYouTube(BufferRoute):
    name = "youtube"
    service = "youtube"
    cannot = "Shorts through Buffer: 10 a day until the channel is verified, no containsSyntheticMedia flag."

    def metadata(self, spec: PostSpec) -> dict:
        return {"title": (spec.title or spec.caption)[:100], "privacy": "public", "madeForKids": False}


class BufferX(BufferRoute):
    name = "x"
    service = "twitter"
    kinds = frozenset({VIDEO, TEXT})
    cannot = "X through Buffer at €0: Buffer pays X's fees. 50 posts a day on an unverified account; we post at most 2."

    def _post(self, media: Media, spec: PostSpec) -> PostResult:
        if media.kind == TEXT and not spec.caption.strip():
            raise PublisherError("buffer: an X text post needs text")
        return super()._post(media, spec)


BUFFER_ROUTES: dict[str, type[BufferRoute]] = {c.name: c for c in (BufferTikTok, BufferInstagram, BufferYouTube, BufferX)}
