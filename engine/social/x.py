"""X through its own API, only after a spend gate (pay per use: about $0.015 a post, $0.20 with a link).

Text posts, signed with OAuth 1.0a user context (the account's own keys). Video on X stays on Buffer, which also
carries X at €0 until the gate. Nothing here runs unless the route for x is `direct` and the network is live.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from .base import TEXT, Adapter, Media, PostResult, PostSpec, PublisherError
from .http import HttpResponse, Request

API = "https://api.x.com/2/tweets"


def _pct(s: str) -> str:
    return quote(str(s), safe="~-._")


def oauth1_header(method: str, url: str, keys: dict[str, str], *, nonce: str = "", timestamp: str = "") -> str:
    """The OAuth 1.0a Authorization header (HMAC-SHA1). A JSON body is not part of the signature."""
    oauth = {
        "oauth_consumer_key": keys["X_API_KEY"],
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": keys["X_ACCESS_TOKEN"],
        "oauth_version": "1.0",
    }
    params = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(oauth.items()))
    base = "&".join((method.upper(), _pct(url), _pct(params)))
    key = f"{_pct(keys['X_API_KEY_SECRET'])}&{_pct(keys['X_ACCESS_TOKEN_SECRET'])}"
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


class XDirect(Adapter):
    name = "x"
    route = "direct"
    kinds = frozenset({TEXT})
    env_sets = (("X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"),)
    cannot = "Text only on the paid API; video goes through Buffer. Needs an approved spend gate first."

    def _post(self, media: Media, spec: PostSpec) -> PostResult:
        text = spec.caption.strip()
        if not text:
            raise PublisherError("x: a text post needs text")
        keys = {k: self.var(k) for k in self.env_sets[0]}
        body = self.send(Request("POST", API, headers={"Authorization": oauth1_header("POST", API, keys)},
                                 json={"text": text})).json()
        tid = (body.get("data") or {}).get("id")
        if not tid:
            raise PublisherError("x: no post id in the response")
        return PostResult(f"https://x.com/i/web/status/{tid}", tid)

    def dry_run_responder(self, req: Request) -> HttpResponse:
        if req.url == API and req.method == "POST":
            return HttpResponse.of_json(201, {"data": {"id": "1900000000000000001", "text": (req.json or {}).get("text")}})
        return HttpResponse.of_json(404, {"title": f"dry-run responder: unexpected {req.method} {req.url}"})
