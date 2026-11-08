"""still-story: a short spoken explanation over model-made illustrations.

Stills from Z-Image-Turbo, a slow push on each, the sentence on screen, Kokoro's stock voice, a pad made by code.
post["scenes"]: [{"say": "...", "image": "..."}]. Illustrated, not photoreal: `realistic` stays false.
"""

from pathlib import Path


def make(post, out_dir, kit):
    out_dir = Path(out_dir)
    clips, voices = [], []
    style = post["prompts"].get("style", "")
    for i, s in enumerate(post["prompts"]["scenes"]):
        still = kit.image(f"{s['image']}. {style}", out_dir / f"still{i}.png", seed=kit.seed + i)
        wav, seconds = kit.voice(s["say"], out_dir / f"say{i}.wav", voice=post["prompts"].get("voice", "af_heart"))
        hold = seconds + 0.25
        push = kit.kenburns(still, hold, out_dir / f"push{i}.mp4", zoom_to=1.12, direction=1 if i % 2 == 0 else -1)
        text = kit.caption_png(s["say"], out_dir / f"cap{i}.png", y=1420, size=64)
        clips.append(kit.overlay(push, text, out_dir / f"clip{i}.mp4"))
        voices.append(wav)
    video = kit.concat(clips, out_dir / "video.mp4")
    voice = kit.concat_audio(voices, out_dir / "voice.wav", gap=0.25)
    bed = kit.bed(kit.seconds(video), out_dir / "bed.wav", root_hz=146.8, volume=0.05)
    final = kit.finish(video, out_dir / "final.mp4", voice=voice, bed=bed)
    return {"file": final, "ai": {"generated": True, "realistic": False}}
