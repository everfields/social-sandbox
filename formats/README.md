# Formats: the creative engine, one per company, never shared

A format is a hypothesis: who it is for, the hook in the first second, the payoff, and why someone would follow. A new caption is not a new format. Each one lives in its own folder:

- `brief.md`: the approved direction: audience, promise, hook, look, voice, sound, and what never appears. Carlos approves it with 3 sample renders, in a strategy gate.
- `make.py`: the generator, run on the PC by `engine/pc/render.py`. It defines `make(post, out_dir, kit)` and returns `{"file": <path of the 9:16 mp4>, "ai": {"generated": bool, "realistic": bool}}`. `kit` gives it the image and video models, the voice, type and the final cut.
- `samples/`: the renders the founder approved.

Code holds the layout, the type and the brand; open models add images, motion, voice and music; Claude writes the scripts. Every post carries something new: new pixels around the same content are not new content.
