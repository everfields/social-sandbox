"""A tiny HTTP seam, so adapters run the same code live, in a dry run and in tests.

A dry run swaps the real transport for `RecordingTransport`: every request an adapter would send is recorded (secrets
redacted, bodies summarised) and answered by that adapter's responder, which mimics the real API. The record is the
publish plan. Ported from AntiSearch's `publish/http.py`, on urllib instead of requests.
"""

from __future__ import annotations

import json as _json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

REDACT_HEADERS = {"authorization", "cookie", "x-goog-api-key"}
REDACT_FIELDS = {"access_token", "client_secret", "password", "refresh_token", "code", "code_verifier", "token"}
SECRET_TEXT = re.compile(
    r"(sk-ant-[\w-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|ya29\.[\w-]{20,}|1//[\w-]{20,}"
    r"|IG[A-Za-z0-9]{40,}|EAA[A-Za-z0-9]{40,}|eyJ[\w-]{20,}\.[\w-]{10,}\.[\w-]{10,})"
)


def redact_text(text: str) -> str:
    return SECRET_TEXT.sub("[redacted]", str(text or ""))


def _redact(mapping: Any) -> Any:
    if not isinstance(mapping, dict):
        return mapping
    return {k: ("<redacted>" if str(k).lower() in REDACT_FIELDS else _redact(v)) for k, v in mapping.items()}


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        return _json.loads(self.body.decode("utf-8")) if self.body else {}

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str) -> str:
        lname = name.lower()
        for k, v in self.headers.items():
            if k.lower() == lname:
                return v
        return ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @classmethod
    def of_json(cls, status: int, payload: Any, headers: dict[str, str] | None = None) -> "HttpResponse":
        return cls(status, {"Content-Type": "application/json", **(headers or {})}, _json.dumps(payload).encode())


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] | None = None
    json: Any = None
    data: bytes | None = None
    form: dict[str, str] | None = None  # application/x-www-form-urlencoded
    files: dict[str, tuple[str, bytes, str]] | None = None  # multipart: name -> (filename, bytes, type)

    def full_url(self) -> str:
        if not self.params:
            return self.url
        sep = "&" if urllib.parse.urlsplit(self.url).query else "?"
        return f"{self.url}{sep}{urllib.parse.urlencode(self.params)}"


class Transport(Protocol):
    def send(self, req: Request, timeout: float = 60.0) -> HttpResponse: ...


class HttpError(RuntimeError):
    def __init__(self, req: Request, resp: HttpResponse):
        snippet = redact_text(resp.body[:300].decode("utf-8", "replace"))
        super().__init__(f"{req.method} {urllib.parse.urlsplit(req.url).path} -> {resp.status}: {snippet}")
        self.status = resp.status
        self.response = resp


def encode(req: Request) -> tuple[bytes | None, dict[str, str]]:
    headers = dict(req.headers)
    if req.files:
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for k, v in (req.form or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, blob, ctype) in req.files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f"Content-Type: {ctype}\r\n\r\n".encode() + blob + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        return b"".join(parts), headers
    if req.form is not None:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return urllib.parse.urlencode(req.form).encode(), headers
    if req.json is not None:
        headers.setdefault("Content-Type", "application/json")
        return _json.dumps(req.json).encode(), headers
    return req.data, headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a resumable upload answers 308; never follow it
        return None


class UrllibTransport:
    """The live transport. Redirects are followed except 308 (resumable uploads use it as a status)."""

    def __init__(self, user_agent: str = "social-engine/1.0 (+https://github.com)"):
        self.user_agent = user_agent

    def send(self, req: Request, timeout: float = 60.0) -> HttpResponse:
        body, headers = encode(req)
        headers.setdefault("User-Agent", self.user_agent)
        r = urllib.request.Request(req.full_url(), data=body, headers=headers, method=req.method)
        opener = urllib.request.build_opener(_NoRedirect) if req.method == "PUT" else urllib.request.build_opener()
        try:
            with opener.open(r, timeout=timeout) as resp:
                return HttpResponse(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            return HttpResponse(e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b"")


def summarize(req: Request) -> dict:
    out: dict[str, Any] = {
        "method": req.method,
        "url": req.url,
        "headers": {k: ("<redacted>" if k.lower() in REDACT_HEADERS else v) for k, v in req.headers.items()},
    }
    if req.params:
        out["params"] = _redact(req.params)
    if req.json is not None:
        out["json"] = _redact(req.json)
    if req.form:
        out["form"] = _redact(req.form)
    if req.data is not None:
        out["body_bytes"] = len(req.data)
    if req.files:
        out["files"] = {k: {"filename": v[0], "bytes": len(v[1]), "type": v[2]} for k, v in req.files.items()}
    return out


class RecordingTransport:
    """The dry-run transport: records each request and answers it with `responder`."""

    def __init__(self, responder: Callable[[Request], HttpResponse]):
        self.responder = responder
        self.requests: list[Request] = []
        self.responses: list[HttpResponse] = []

    def send(self, req: Request, timeout: float = 60.0) -> HttpResponse:
        self.requests.append(req)
        resp = self.responder(req)
        self.responses.append(resp)
        return resp

    def plan(self) -> list[dict]:
        return [{**summarize(q), "simulated_response": {"status": p.status}} for q, p in zip(self.requests, self.responses)]
