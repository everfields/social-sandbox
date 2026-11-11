"""The social engine's pipe: queue, publisher, guards, media host, metrics and alarms.

Shared by every company, vendored from the launch-company skill (engine/social/) into each `<slug>-social` repo,
and upgraded like `cos.mjs`. Deterministic, no AI, standard library only (Python >= 3.11). The creative engine (the
formats) lives in each company's social repo, never here. Method: knowledge/social-native.md; design:
references/social-engine.md.
"""

VERSION = "1.1.1"
NETWORKS = ("tiktok", "instagram", "youtube", "x")
