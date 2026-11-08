"""The adapter contract: one adapter per network and route, behind `post(media, spec) -> PostResult`.

Ported from AntiSearch's publisher (`publisher/base.py`) and widened: a post carries its AI flags, a title where the
network needs one (YouTube), and network options (Instagram trial reels). Credentials only ever come from the
environment, and a dry run answers every call from the adapter's own responder.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, TypeVar

from .http import HttpError, HttpResponse, Request, Transport

VIDEO = "video"
IMAGE = "image"
TEXT = "text"
PUBLISHED = "published"
DRAFTED = "drafted"  # waits for a person to tap Post inside the network's app (TikTok inbox)
PROCESSING = "processing"  # handed over; the network is still processing it
HANDED = "handed"  # Buffer accepted it; whether it went out is reconciled later

T = TypeVar("T")


class PublisherError(RuntimeError):
    retryable = False


class Retryable(PublisherError):
    """A transient failure (the media host was slow): the post stays in the queue for the next run."""

    retryable = True


@dataclass(frozen=True)
class Media:
    path: Path
    kind: str = VIDEO
    mime: str = "video/mp4"
    width: int = 1080
    height: int = 1920
    duration_s: float = 0.0
    public_url: str = ""
    cover_s: float | None = None

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    def read(self) -> bytes:
        return self.path.read_bytes()


@dataclass(frozen=True)
class PostSpec:
    caption: str = ""
    title: str = ""  # YouTube needs one; elsewhere unused
    ai_generated: bool = False  # made with generative models (C2PA marked at the PC)
    ai_realistic: bool = False  # realistic people, places or events: the platform's AI label is required
    trial: bool = False  # Instagram trial reel: shown to non-followers first
    language: str = "en"
    tags: tuple[str, ...] = ()
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PostResult:
    url: str
    remote_id: str
    state: str = PUBLISHED
    note: str = ""


class Adapter(ABC):
    name: str = ""  # the network
    route: str = "direct"
    env_sets: tuple[tuple[str, ...], ...] = ()  # any one complete set is enough
    optional_env: tuple[str, ...] = ()
    kinds: frozenset[str] = frozenset({VIDEO})
    needs_public_url: bool = False  # the network fetches the media from a URL (Pages)
    cannot: str = ""  # what this route cannot do, said plainly

    def __init__(self, transport: Transport | None = None, env: Mapping[str, str] | None = None, *,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time):
        self.transport = transport
        self.env = dict(os.environ if env is None else env)
        self.sleep = sleep
        self.now = now
        self.warnings: list[str] = []

    @classmethod
    def missing(cls, env: Mapping[str, str]) -> list[str]:
        for names in cls.env_sets:
            if all((env.get(n) or "").strip() for n in names):
                return []
        return [n for n in cls.env_sets[0] if not (env.get(n) or "").strip()] if cls.env_sets else []

    @classmethod
    def placeholder_env(cls) -> dict[str, str]:
        return {n: f"DRY_RUN_{n}" for n in (cls.env_sets[0] if cls.env_sets else ())}

    def var(self, name: str, default: str = "") -> str:
        return (self.env.get(name) or default).strip()

    def send(self, req: Request) -> HttpResponse:
        assert self.transport is not None, "adapter has no transport"
        resp = self.transport.send(req)
        if not resp.ok:
            raise HttpError(req, resp)
        return resp

    def poll(self, fetch: Callable[[], T | None], *, attempts: int, interval: float, what: str) -> T:
        for i in range(attempts):
            got = fetch()
            if got is not None:
                return got
            if i < attempts - 1:
                self.sleep(interval)
        raise PublisherError(f"{self.name}: {what} not ready after {attempts} checks")

    def post(self, media: Media, spec: PostSpec) -> PostResult:
        if media.kind not in self.kinds:
            raise PublisherError(f"{self.name} via {self.route} cannot post {media.kind}")
        if self.needs_public_url and media.kind != TEXT and not media.public_url:
            raise PublisherError(f"{self.name} via {self.route}: the media needs a public URL (the Pages runway)")
        return self._post(media, spec)

    @abstractmethod
    def _post(self, media: Media, spec: PostSpec) -> PostResult: ...

    @abstractmethod
    def dry_run_responder(self, req: Request) -> HttpResponse: ...
