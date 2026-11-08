"""What a format's `make()` works with: models for stills, motion and voice, type drawn by code, and the final cut.

Code holds the layout, the type and the brand; the models add images, motion and voice. Every output of `finish()` is
1080x1920, H.264, yuv420p, 30 fps, AAC: what the intake checks.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from comfy import Comfy, still_graph, video_graph
from mark import tool

W, H = 1080, 1920


class Kit:
    def __init__(self, folder: Path, work: Path, seed: int = 7):
        self.folder = Path(folder)
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.comfy = Comfy(self.folder)
        self.ffmpeg = tool(self.folder, "ffmpeg")
        self.ffprobe = tool(self.folder, "ffprobe")
        self._tts: dict[str, object] = {}
        self._last = ""

    def font(self, size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
        for hit in (self.folder / "tools" / "inter").rglob(f"Inter-{weight}.ttf"):
            return ImageFont.truetype(str(hit), size)
        raise RuntimeError("the Inter font is not installed (setup_pc.py)")

    def _model(self, name: str) -> None:
        if self._last and self._last != name:
            self.comfy.free()
        self._last = name

    def image(self, prompt: str, out: Path, *, seed: int | None = None, steps: int = 9) -> Path:
        self.comfy.start()
        self._model("still")
        return self.comfy.run(still_graph(prompt, 1088, 1920, self.seed if seed is None else seed, steps), Path(out))

    def video(self, prompt: str, out: Path, *, frames: int = 121, seed: int | None = None, steps: int = 20, width: int = 704,
              height: int = 1280) -> Path:
        self.comfy.start()
        self._model("video")
        return self.comfy.run(video_graph(prompt, width, height, frames, self.seed if seed is None else seed, steps=steps), Path(out))

    def done_with_models(self) -> None:
        if self.comfy.up():
            self.comfy.free()
        self.comfy.stop()

    def run(self, *args: str) -> None:
        r = subprocess.run([self.ffmpeg, "-v", "error", "-y", *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg: {r.stderr[:800]}")

    def seconds(self, path: Path) -> float:
        out = subprocess.run([self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, check=True).stdout
        return float(out.strip())

    def voice(self, text: str, out: Path, *, voice: str = "af_heart", lang: str = "a", speed: float = 1.0) -> tuple[Path, float]:
        """Kokoro-82M. Only the model's own stock voices: never a clone of a real person without a written release."""
        import numpy as np
        from kokoro import KPipeline

        pipe = self._tts.setdefault(lang, KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M"))
        audio = np.concatenate([np.asarray(a) for _, _, a in pipe(text, voice=voice, speed=speed)])
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return out, len(audio) / 24000

    def bed(self, seconds: float, out: Path, *, root_hz: float = 110.0, volume: float = 0.09) -> Path:
        """A soft pad made by code: no sample, nothing to license."""
        f = root_hz
        expr = (f"{volume}*(sin(2*PI*{f}*t)+0.6*sin(2*PI*{f * 1.5}*t)+0.45*sin(2*PI*{f * 2}*t))"
                f"*(0.75+0.25*sin(2*PI*0.25*t))")
        self.run("-f", "lavfi", "-i", f"aevalsrc={expr}:s=44100:d={seconds:.3f}", "-af",
                 f"afade=in:st=0:d=0.6,afade=out:st={max(0, seconds - 0.8):.3f}:d=0.8", "-c:a", "pcm_s16le", str(out))
        return Path(out)

    def wrap(self, draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
        lines: list[str] = []
        cur = ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        return lines + ([cur] if cur else [])

    def caption_png(self, text: str, out: Path, *, y: int = 1380, size: int = 72, weight: str = "Bold", band: bool = True,
                    align: str = "center", color: tuple = (255, 255, 255, 255)) -> Path:
        """Type by code: a line of text on a soft band, inside the area no app covers."""
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        font = self.font(size, weight)
        lines = self.wrap(d, text, font, W - 180)
        step = int(size * 1.25)
        top = y - (len(lines) * step) // 2
        if band:
            d.rounded_rectangle((60, top - 36, W - 60, top + len(lines) * step + 30), radius=36, fill=(10, 10, 16, 165))
        for i, line in enumerate(lines):
            x = (W - d.textlength(line, font=font)) / 2 if align == "center" else 90
            d.text((x, top + i * step), line, font=font, fill=color)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return out

    def kenburns(self, still: Path, seconds: float, out: Path, *, zoom_to: float = 1.12, direction: int = 1) -> Path:
        """A slow push in or pull out on a still, 30 fps, 1080x1920."""
        frames = max(2, int(round(seconds * 30)))
        step = (zoom_to - 1.0) / frames
        z = f"min(1+{step:.6f}*on,{zoom_to})" if direction > 0 else f"max({zoom_to}-{step:.6f}*on,1)"
        self.run("-loop", "1", "-i", str(still), "-t", f"{seconds:.3f}", "-vf",
                 f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
                 f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps=30,format=yuv420p",
                 "-frames:v", str(frames), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-an", str(out))
        return Path(out)

    def overlay(self, video: Path, png: Path, out: Path, *, start: float = 0.0, end: float | None = None) -> Path:
        enable = f":enable='between(t,{start:.3f},{end:.3f})'" if end is not None else ""
        self.run("-i", str(video), "-i", str(png), "-filter_complex", f"[0:v][1:v]overlay=0:0{enable},format=yuv420p",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-an", str(out))
        return Path(out)

    def concat(self, parts: list[Path], out: Path) -> Path:
        lst = Path(out).with_suffix(".txt")
        lst.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        self.run("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))
        return Path(out)

    def concat_audio(self, parts: list[Path], out: Path, *, gap: float = 0.25) -> Path:
        silence = self.work / "gap.wav"
        self.run("-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono", "-t", f"{gap}", "-c:a", "pcm_s16le", str(silence))
        lst = Path(out).with_suffix(".txt")
        lines = []
        for p in parts:
            lines += [f"file '{Path(p).resolve().as_posix()}'\n", f"file '{silence.resolve().as_posix()}'\n"]
        lst.write_text("".join(lines), encoding="utf-8")
        self.run("-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "pcm_s16le", str(out))
        return Path(out)

    def finish(self, video: Path, out: Path, *, voice: Path | None = None, bed: Path | None = None, trim: float | None = None,
               loudness: float = -15.0) -> Path:
        """The master: 1080x1920, H.264 yuv420p, 30 fps, AAC 44.1 kHz, faststart, levelled to `loudness` LUFS. A clip whose
        only sound is a pad passes a lower target: the platforms play everything near -14, and a drone at that level grates."""
        level = f"loudnorm=I={loudness}:TP=-1.5:LRA=11"
        args = ["-i", str(video)]
        inputs = 0
        if voice:
            args += ["-i", str(voice)]
            inputs += 1
        if bed:
            args += ["-i", str(bed)]
            inputs += 1
        if inputs == 0:
            args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
            amap = "1:a"
            fa = ["-map", amap, "-shortest"]
        elif inputs == 1:
            args += ["-filter_complex", f"[1:a]aresample=44100,{level}[a]"]
            fa = ["-map", "[a]", "-shortest"]
        else:
            args += ["-filter_complex", f"[1:a]aresample=44100[v];[2:a]aresample=44100[b];[v][b]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{level}[a]"]
            fa = ["-map", "[a]"]
        v = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,format=yuv420p"
        self.run(*args, "-map", "0:v", *fa, "-vf", v, *(["-t", f"{trim:.3f}"] if trim else []), "-c:v", "libx264", "-preset", "medium",
                 "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", str(out))
        return Path(out)
