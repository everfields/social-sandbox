"""Intake: every post the PC makes is checked before it can be posted. A failing post is quarantined, never posted.

Checks, per item:
- shape: the queue row has what the publisher needs;
- media: the file matches its SHA-256; 9:16, tall enough, the length, the size and the codecs each network takes;
- dedupe: its perceptual hash against everything already queued or posted on that network;
- caption: no repeat on the network, no serial numbers ("part 3", "#12", "3/10"), few hashtags, the length limits,
  a title for YouTube;
- AI marking: a file made with generative models carries a C2PA manifest with the IPTC digital source type.
Then the runway: days of checked, unposted posts per network.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from . import phash
from .config import Config
from .queue import Item, items, move, release_url, shape_problems
from .state import iso

SERIAL = re.compile(r"(^|\s)(day|part|episode|ep|vol\.?|no\.?)\s*\d+\b|(^|\s)#\d+\b|\b\d+\s*/\s*\d+\b", re.I)
HASHTAG = re.compile(r"(^|\s)#[^\s#]+")
AI_TYPES = ("trainedalgorithmicmedia", "compositewithtrainedalgorithmicmedia")
CAPTION_LIMIT = {"x": 280, "youtube": 4900, "instagram": 2200, "tiktok": 2200}
MAX_S = {"youtube": 180, "instagram": 180, "tiktok": 600, "x": 140}


@dataclass
class Report:
    accepted: list[str] = field(default_factory=list)
    quarantined: dict[str, list[str]] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)
    runway: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "quarantined": self.quarantined, "warnings": self.warnings, "runway": self.runway}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    ffprobe = phash.tool("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is needed at intake")
    out = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, check=True, timeout=120).stdout
    j = json.loads(out)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    num, _, den = str(v.get("avg_frame_rate", "0/1")).partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return {
        "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
        "duration_s": round(float((j.get("format") or {}).get("duration") or v.get("duration") or 0), 3),
        "video_codec": v.get("codec_name", ""), "pix_fmt": v.get("pix_fmt", ""), "fps": round(fps, 3),
        "audio_codec": (a or {}).get("codec_name", ""), "bytes": path.stat().st_size,
    }


def media_problems(p: dict, network: str, rules: dict, kind: str = "video") -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    w, h = p["width"], p["height"]
    aw, ah = rules["aspect"]
    if not w or not h:
        return ["no video stream"], warnings
    if abs(w / h - aw / ah) > 0.01:
        errors.append(f"aspect {w}x{h}, not {aw}:{ah}")
    if h < rules["min_height"]:
        errors.append(f"height {h} under {rules['min_height']}")
    if kind == "video":
        top = min(rules["max_s"], MAX_S.get(network, rules["max_s"]))
        if not rules["min_s"] <= p["duration_s"] <= top:
            errors.append(f"length {p['duration_s']}s outside {rules['min_s']}-{top}s")
        if p["video_codec"] not in rules["video_codecs"]:
            errors.append(f"video codec {p['video_codec']} (want {', '.join(rules['video_codecs'])})")
        if p["pix_fmt"] not in ("yuv420p", "yuvj420p"):
            errors.append(f"pixel format {p['pix_fmt']} (want yuv420p)")
        if p["fps"] > 60.5:
            errors.append(f"{p['fps']} fps (60 at most)")
        if not p["audio_codec"]:
            warnings.append("silent: no audio track")
        elif p["audio_codec"] not in rules["audio_codecs"]:
            errors.append(f"audio codec {p['audio_codec']} (want {', '.join(rules['audio_codecs'])})")
    if p["bytes"] > rules["max_mb"] * 1024 * 1024:
        errors.append(f"{p['bytes'] / 1048576:.1f} MB, over {rules['max_mb']} MB")
    return errors, warnings


def _norm(text: str) -> str:
    text = re.sub(r"https?://\S+|\bwww\.\S+", " ", (text or "").lower())
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def near_duplicate(a: str, b: str, ratio: float = 0.9) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return len(na) >= 30 and len(nb) >= 30 and SequenceMatcher(None, na, nb).ratio() >= ratio


def caption_problems(item: Item, previous: list[str], rules: dict) -> list[str]:
    d, net = item.data, item.network
    cap = str(d.get("caption") or "")
    p = []
    if len(cap) > min(rules["caption_max"], CAPTION_LIMIT.get(net, 2200)):
        p.append(f"caption is {len(cap)} characters (limit {min(rules['caption_max'], CAPTION_LIMIT.get(net, 2200))})")
    tags = len(HASHTAG.findall(cap))
    if tags > rules["max_hashtags"]:
        p.append(f"{tags} hashtags (at most {rules['max_hashtags']})")
    if SERIAL.search(cap) or SERIAL.search(str(d.get("title") or "")):
        p.append("a serial number in the caption or title (part 3, #12, 3/10): every post stands alone")
    if any(near_duplicate(cap, old) for old in previous):
        p.append("the caption repeats an earlier one on this network")
    if net == "youtube":
        title = str(d.get("title") or "").strip()
        if not title:
            p.append("YouTube needs a title")
        elif len(title) > 100:
            p.append(f"title is {len(title)} characters (100 at most)")
        elif any(near_duplicate(title, old) for old in previous):
            p.append("the title repeats an earlier one")
    return p


def ai_mark_problems(path: Path, item: Item) -> list[str]:
    """A generative file must carry C2PA with the IPTC digital source type. c2patool when present, else the bytes."""
    if not item.ai.get("generated"):
        return []
    c2pa = phash.tool("c2patool")
    if c2pa:
        r = subprocess.run([c2pa, str(path)], capture_output=True, text=True, timeout=120)
        report = (r.stdout or "").lower()
        if r.returncode != 0 or "c2pa.actions" not in report:
            return ["AI-made, but no C2PA manifest (mark it at the PC: pc/mark.py)"]
        if not any(t in report for t in AI_TYPES):
            return ["AI-made, but the C2PA manifest has no trainedAlgorithmicMedia source type"]
        return []
    blob = path.read_bytes()
    low = blob.lower()
    if b"c2pa" not in low:
        return ["AI-made, but no C2PA manifest (mark it at the PC: pc/mark.py)"]
    if not any(t.encode() in low for t in AI_TYPES):
        return ["AI-made, but the C2PA manifest has no trainedAlgorithmicMedia source type"]
    return []


def fetcher(repo: str, cache: Path) -> Callable[[str], Path]:
    """Download a release asset (tag `media`) once, into the cache."""

    def get(file: str) -> Path:
        dest = cache / file
        if dest.is_file():
            return dest
        cache.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(release_url(repo, file), timeout=300) as r, tmp.open("wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
        return dest

    return get


def history(root: Path, network: str, skip: str) -> tuple[dict[str, list[str]], list[str]]:
    """Perceptual hashes and captions already accepted or posted on `network` (queue and archive)."""
    hashes, captions = {}, []
    for folder in ("archive", "queue"):
        for it in items(root, folder, network):
            if it.id == skip or not it.checked:
                continue
            h = (it.data.get("intake") or {}).get("phash")
            if h:
                hashes[it.id] = h
            for k in ("caption", "title"):
                if it.data.get(k):
                    captions.append(str(it.data[k]))
    return hashes, captions


def check(root: Path, cfg: Config, fetch: Callable[[str], Path], now: datetime, *, recheck: bool = False) -> Report:
    rep = Report()
    rules = cfg.intake
    for it in items(root):
        if it.checked and not recheck:
            continue
        errors: list[str] = []
        warnings: list[str] = []
        if it.data.get("_unreadable"):
            errors.append("not valid JSON")
        else:
            errors += shape_problems(it.data, it.path.parent.name)
        probe_info: dict = {}
        h: list[str] = []
        if not errors:
            try:
                path = fetch(it.file)
                if sha256(path) != it.data["media"]["sha256"]:
                    errors.append("the media file does not match its sha256")
                else:
                    probe_info = probe(path)
                    kind = (it.data.get("media") or {}).get("kind", "video")
                    e, w = media_problems(probe_info, it.network, rules, kind)
                    errors += e
                    warnings += w
                    errors += ai_mark_problems(path, it)
                    h = phash.media_hash(path, probe_info["duration_s"], kind)
                    hashes, captions = history(root, it.network, it.id)
                    near = phash.nearest(h, hashes)
                    if near and near[1] <= rules["phash_distance"]:
                        errors.append(f"near-duplicate of {near[0]} (distance {near[1]:.1f})")
                    errors += caption_problems(it, captions, rules)
            except Exception as e:  # noqa: BLE001 - any failure quarantines the item, with the reason
                errors.append(f"could not check the media: {e}")
        block = {"ok": not errors, "at": iso(now), "problems": errors, "warnings": warnings, "probe": probe_info,
                 "phash": h}
        if errors:
            move(it, root, "quarantine", {"intake": block})
            rep.quarantined[f"{it.network}/{it.id}"] = errors
        else:
            it.data["intake"] = block
            it.save()
            rep.accepted.append(f"{it.network}/{it.id}")
        if warnings:
            rep.warnings[f"{it.network}/{it.id}"] = warnings
    return rep


def runway(root: Path, cfg: Config, used: set[str], now: datetime) -> dict[str, dict]:
    """Days of checked, unposted posts per enabled network, at its full cadence."""
    out = {}
    for net in cfg.enabled():
        n = sum(1 for it in items(root, network=net.name) if it.checked and it.id not in used)
        per_day = max(1, len(net.slots))
        out[net.name] = {"posts": n, "per_day": per_day, "days": round(n / per_day, 1)}
    return out
