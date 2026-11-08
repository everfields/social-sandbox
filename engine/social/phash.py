"""Perceptual dedupe: catches a near-duplicate that an id check misses (a re-render, a new border, a new caption).

A video's hash is the difference hash (dHash, 64 bits) of five frames at 10, 30, 50, 70 and 90 % of its length; an
image is one frame. ffmpeg scales each frame to 9x8 grey pixels, so no imaging library is needed. Two posts are
near-duplicates when the mean Hamming distance over the matching frames is at most the threshold (10 of 64 bits by
default). Platforms treat "new pixels around the same content" as unoriginal (knowledge/social-native.md).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

POSITIONS = (0.1, 0.3, 0.5, 0.7, 0.9)


def tool(name: str) -> str | None:
    """ffmpeg / ffprobe / c2patool: from SOCIAL_<NAME> (a path), else PATH."""
    explicit = os.environ.get(f"SOCIAL_{name.upper()}")
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which(name)


def dhash(pixels: bytes) -> int:
    """64-bit difference hash from 9x8 grey pixels (row-major): one bit per left>right comparison."""
    if len(pixels) != 72:
        raise ValueError(f"dhash needs 72 grey pixels, got {len(pixels)}")
    bits = 0
    for y in range(8):
        row = pixels[y * 9:(y + 1) * 9]
        for x in range(8):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def frame_pixels(path: Path, at_s: float | None, ffmpeg: str) -> bytes:
    cmd = [ffmpeg, "-v", "error"]
    if at_s is not None:
        cmd += ["-ss", f"{at_s:.3f}"]
    cmd += ["-i", str(path), "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
    return out[:72]


def media_hash(path: Path, duration_s: float, kind: str = "video", ffmpeg: str | None = None) -> list[str]:
    ffmpeg = ffmpeg or tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is needed for the perceptual hash")
    if kind == "image" or duration_s <= 0:
        return [f"{dhash(frame_pixels(path, None, ffmpeg)):016x}"]
    return [f"{dhash(frame_pixels(path, max(0.0, duration_s * p), ffmpeg)):016x}" for p in POSITIONS]


def distance(a: list[str], b: list[str]) -> float:
    """Mean Hamming distance over the frames both hashes have (64 when they share none)."""
    pairs = list(zip(a, b))
    if not pairs:
        return 64.0
    return sum(bin(int(x, 16) ^ int(y, 16)).count("1") for x, y in pairs) / len(pairs)


def nearest(h: list[str], others: dict[str, list[str]]) -> tuple[str, float] | None:
    best = None
    for key, other in others.items():
        d = distance(h, other)
        if best is None or d < best[1]:
            best = (key, d)
    return best
