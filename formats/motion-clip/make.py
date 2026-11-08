"""motion-clip: one short scene from a video model, with a hook line drawn by code.

Wan 2.2 TI2V-5B at 704x1280, 121 frames (5 s at 24 fps), scaled to the master by ffmpeg. Photoreal, so `realistic` is
true: the post carries the platform's AI label as well as C2PA.
"""

from pathlib import Path


def make(post, out_dir, kit):
    out_dir = Path(out_dir)
    p = post["prompts"]
    clip = kit.video(p["video"], out_dir / "raw.mp4", frames=int(p.get("frames", 121)), steps=int(p.get("steps", 20)))
    scaled = out_dir / "scaled.mp4"
    kit.run("-i", str(clip), "-vf", "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
            "-crf", "18", "-an", str(scaled))
    hook = kit.caption_png(p["hook"], out_dir / "hook.png", y=420, size=84)
    video = kit.overlay(scaled, hook, out_dir / "video.mp4", start=0.15, end=kit.seconds(scaled) - 0.1)
    bed = kit.bed(kit.seconds(video), out_dir / "bed.wav", root_hz=110.0, volume=0.09)
    final = kit.finish(video, out_dir / "final.mp4", voice=bed)
    return {"file": final, "ai": {"generated": True, "realistic": True}}
