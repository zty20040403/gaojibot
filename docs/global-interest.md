# Global interest candidates

This slice produces public headlines only; it does not match groups, configure or send messages, or invoke a model. It never reads chat, group/member data, author profiles, article pages, images or scripts. Requests are fixed GETs with no query, credentials, cookies, proxy environment, or redirects. The sources explicitly advertise RSS subscriptions:

- [NASA Technology RSS](https://www.nasa.gov/technology/feed/), listed on [NASA's RSS page](https://www.nasa.gov/rss-feeds/): science/space technology.
- [Library of Congress Timeless RSS](https://blogs.loc.gov/loc/feed/), linked under “Share & Subscribe” on [the blog](https://blogs.loc.gov/loc/): culture/history.

Only RSS item title, article link and publication date are retained. Topic tags are a small fixed vocabulary derived from source and headline; authors, descriptions and arbitrary feed categories are not stored. Titles may mention public figures but no person profile is fetched or extracted. Links must be HTTPS on the corresponding publisher host; Library of Congress links must have the blog's dated article path and NASA profile/author paths are rejected. Query strings and fragments are removed. Links are attribution, not instructions to automatically fetch them. Source titles, links, and derived records are **untrusted external data**, not instructions; consumers must keep this boundary when rendering or passing data to a model.

On NixOS with `services.gaoji.enable` and `runBot`, `gaoji-interest.timer` runs the independent `gaoji-interest.service` oneshot at 05:00 Asia/Shanghai. `Persistent=true` catches missed runs when a prior timer stamp exists; a first installation waits for the next scheduled time. The dedicated `gaoji-interest` account has no bot environment file or credentials, cannot access bot state or PostgreSQL sockets, and runs with filesystem and IP sandboxing. DNS uses the public resolver 1.1.1.1 because private/loopback IPs are denied; the host must allow DNS to that resolver and public HTTPS. The bot's supplementary interest group is read-only. No bot maintenance loop runs collection.

The attempt is recorded before the first request; `flock` and the persisted Shanghai date prevent overlapping or repeated attempts across processes and restarts. A crash, timeout or source failure consumes the day: no same-day retry. Clock rollback does not repeat an earlier day. This is at-most-once, not guaranteed success. All producers must share the same persistent interest directory on a filesystem supporting cross-process `flock` and atomic rename; separate hosts or directories are not coordinated.

At most two fixed feed requests per day, one per source, with an 8-second total deadline per source, no retries, no redirects, no decompression, and at most 256 KiB of response bytes per source. At most 50 RSS items are inspected and five valid entries retained per source. Invalid XML, DTDs, unsupported encodings, oversized responses, HTTP errors and timeouts fail that source and are logged without including remote content. On source failure, its earlier unexpired entries remain; a successful empty feed clears that source. Dates more than 14 days old or in the future are excluded. Each candidate expires at the earlier of three days after collection or 14 days after publication.

## Consumer interface

```python
import os
from pathlib import Path
from src.plugins.ai_chat.global_interest import read_candidates

state_dir = Path(os.environ["AI_GLOBAL_INTEREST_STATE_DIR"])
candidates = read_candidates(state_dir)  # no network; filters expired entries
```

Each record has `source`, `title`, `url`, `tags` (Chinese topic labels), `published_at`, `collected_at`, and `expires_at` (Unix seconds). NixOS sets `AI_GLOBAL_INTEREST_STATE_DIR` to `/var/lib/<services.gaoji.stateDirectory>-interest` (default `/var/lib/gaoji-interest`); `global-interest/state.json` and `lock` live below it. The dedicated account owns the directory and files (0750/0640), while the bot can only read. Writes are atomic and fsynced. A missing state returns no candidates; corrupt state or storage failure raises rather than resetting the daily marker. Preserve this directory across restarts and monitor `systemctl status gaoji-interest.timer gaoji-interest.service` and `journalctl -u gaoji-interest.service` for failures. The collector does not load `EnvironmentFile`.

The later slice owns group relevance, per-group switches, rate limits, review and delivery. Before enabling delivery, the owner should inspect candidate quality and sources and conduct a real QQ acceptance test; this slice itself cannot send to users.
