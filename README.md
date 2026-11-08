# Social sandbox

The test bench of the social engine. **It posts nothing**: `SOCIAL_CRON` is `dry-run` and no network secret exists here. The media are sample renders.

The publisher of Social sandbox's own social accounts. Public on purpose: standard Actions runners are free on public repos, GitHub Pages gives the media the plain URLs that Buffer and Meta fetch, and the media are public once posted anyway. Secrets stay secret.

Claude writes in the company repo (`company/social/batches/`), the PC renders at night, and this repo checks and publishes.

- `engine/`: the pipe, vendored from the launch-company skill (`install-social.mjs`). Improve it there, never here.
- `formats/<name>/`: the creative engine: `brief.md`, `make.py`, `samples/`.
- `queue/<network>/`: ready posts. `quarantine/`: what failed intake, with the reasons. `archive/`: posted a week ago or more.
- Branch `social-state`: `posts.csv` (a row before every API call), `state.json`, `metrics/`.
- `social.json`: time zone, slots, routes. Limits are the engine's: a company may go below them, never above.

Repository variables: `SOCIAL_CRON` (`off`, `dry-run`, `live`), `SOCIAL_LIVE_NETWORKS`, `SOCIAL_PUBLIC_BASE_URL`, `SOCIAL_ROUTE_<NETWORK>`, `SOCIAL_SLOTS_<NETWORK>`.

Run by hand from the repo root, with `PYTHONPATH=engine`: `python -m social status`, `validate`, `due`, `publish`, `intake`, `metrics`, `watch`. A post with no outcome: `python -m social resolve <network> <id> --url <link>` or `--failed`. A stopped network: `python -m social resume <network> --reason "..."`.
