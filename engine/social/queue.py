"""The queue: ready posts, one JSON file each, in `queue/<network>/`, written by the PC and checked at intake.

An item: id, network, format, batch, media {file, sha256, kind}, caption, title, language, tags, ai {generated,
realistic}, trial, earliest, and the `intake` block the intake adds (ok, problems, probe, phash). Media are never in
git: they are release assets (tag `media`) and the Pages runway serves them. Posted items move to `archive/` after a
week (their hashes stay for the dedupe); failing items move to `quarantine/` with the reasons.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import NETWORKS
from .state import parse_iso

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,120}$")
MEDIA_TAG = "media"


@dataclass
class Item:
    path: Path
    data: dict

    @property
    def id(self) -> str:
        return str(self.data.get("id", ""))

    @property
    def network(self) -> str:
        return str(self.data.get("network", ""))

    @property
    def file(self) -> str:
        return str((self.data.get("media") or {}).get("file", ""))

    @property
    def earliest(self) -> datetime | None:
        return parse_iso(str(self.data.get("earliest", "")))

    @property
    def checked(self) -> bool:
        return bool((self.data.get("intake") or {}).get("ok"))

    @property
    def ai(self) -> dict:
        return self.data.get("ai") or {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def shape_problems(data: dict, folder_network: str = "") -> list[str]:
    p = []
    if not ID.match(str(data.get("id", ""))):
        p.append("id: letters, digits, dot, dash or underscore, 3-121 characters")
    if data.get("network") not in NETWORKS:
        p.append(f"network: one of {', '.join(NETWORKS)}")
    elif folder_network and data["network"] != folder_network:
        p.append(f"network {data['network']} is in the folder queue/{folder_network}")
    media = data.get("media") or {}
    if not media.get("file") or "/" in str(media.get("file")) or "\\" in str(media.get("file")):
        p.append("media.file: a bare file name")
    if not re.fullmatch(r"[0-9a-f]{64}", str(media.get("sha256", ""))):
        p.append("media.sha256: the file's SHA-256")
    if not isinstance(data.get("caption", ""), str):
        p.append("caption: text")
    if not data.get("format"):
        p.append("format: the format that made it")
    if data.get("earliest") and not parse_iso(str(data["earliest"])):
        p.append("earliest: an ISO date-time")
    if not isinstance(data.get("ai"), dict) or "generated" not in data["ai"]:
        p.append('ai: {"generated": true|false, "realistic": true|false}')
    return p


def items(root: Path, folder: str = "queue", network: str | None = None) -> list[Item]:
    out = []
    base = root / folder
    for net in ([network] if network else NETWORKS):
        d = base / net
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))
            except ValueError:
                data = {"id": f.stem, "network": net, "_unreadable": True}
            out.append(Item(f, data))
    return out


def ready(root: Path, network: str, used: set[str], now: datetime) -> list[Item]:
    """Checked, unposted items whose earliest time has come, oldest first."""
    out = [i for i in items(root, network=network)
           if i.checked and i.id not in used and (i.earliest is None or i.earliest <= now)]
    return sorted(out, key=lambda i: (i.earliest or datetime.min.replace(tzinfo=now.tzinfo), i.id))


def move(item: Item, root: Path, folder: str, note: dict | None = None) -> Path:
    dest = root / folder / item.network / item.path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if note:
        item.data.update(note)
    item.path = Path(shutil.move(str(item.path), str(dest)))
    item.save()
    return item.path


def media_url(base: str, file: str) -> str:
    return f"{base.rstrip('/')}/m/{file}"


def release_url(repo: str, file: str) -> str:
    return f"https://github.com/{repo}/releases/download/{MEDIA_TAG}/{file}"
