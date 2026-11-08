"""code-card: a number that surprises, animated entirely by code. No model touches it, so there is nothing to mark.

Each frame is drawn with Pillow and piped to ffmpeg. The sound is a pad made by code. post["prompts"]: hook, rows
[{"left": "1", "right": "31"}], left_label, right_label, close, source.
"""

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

W, H, FPS = 1080, 1920, 30
BG, INK, ACCENT, MUTED = (16, 18, 24), (244, 241, 233), (255, 196, 88), (150, 154, 168)


def ease(t):
    return 1 - (1 - min(max(t, 0.0), 1.0)) ** 3


def make(post, out_dir, kit):
    out_dir = Path(out_dir)
    p = post["prompts"]
    rows = p["rows"]
    hook_s, row_s, close_s = 2.6, 1.7, 3.2
    total = hook_s + row_s * len(rows) + close_s
    big, mid, small, tiny = kit.font(150), kit.font(82), kit.font(46, "Medium"), kit.font(32, "Medium")
    silent = out_dir / "silent.mp4"
    ff = subprocess.Popen([kit.ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", str(silent)], stdin=subprocess.PIPE)

    def centered(d, y, text, font, fill):
        d.text(((W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    def paragraph(d, y, text, font, fill, step):
        for i, line in enumerate(kit.wrap(d, text, font, W - 170)):
            centered(d, y + i * step, line, font, fill)

    for f in range(int(total * FPS)):
        t = f / FPS
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        if t < hook_s:
            k = ease(t / 0.5)
            paragraph(d, int(760 - 40 * (1 - k)), p["hook"], mid, tuple(int(c * k) for c in INK), 104)
        elif t < hook_s + row_s * len(rows):
            n = int((t - hook_s) // row_s)
            local = (t - hook_s - n * row_s) / row_s
            d.text((90, 200), p["left_label"], font=small, fill=MUTED)
            rx = W - 90 - d.textlength(p["right_label"], font=small)
            d.text((rx, 200), p["right_label"], font=small, fill=MUTED)
            for i in range(n + 1):
                y = 330 + i * 250
                done = 1.0 if i < n else ease(local / 0.55)
                d.text((90, y), rows[i]["left"], font=big, fill=INK)
                value = str(int(round(int(rows[i]["right"]) * done)))
                d.text((W - 90 - d.textlength(value, font=big), y), value, font=big, fill=ACCENT)
                d.line((90, y + 195, W - 90, y + 195), fill=(50, 54, 66), width=2)
        else:
            k = ease((t - hook_s - row_s * len(rows)) / 0.5)
            paragraph(d, 700, p["close"], mid, tuple(int(c * k) for c in ACCENT), 104)
            paragraph(d, 1560, p["source"], tiny, tuple(int(c * k) for c in MUTED), 44)
        ff.stdin.write(img.tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        raise RuntimeError("ffmpeg failed drawing the frames")
    bed = kit.bed(total, out_dir / "bed.wav", root_hz=98.0, volume=0.1)
    final = kit.finish(silent, out_dir / "final.mp4", voice=bed, loudness=-20.0)
    return {"file": final, "ai": {"generated": False}}
