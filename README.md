# Release Tracker Bot

A Telegram bot that watches things and messages you when they change. v1 tracks
**GitHub releases**. The tracker abstraction is built so other sources (Docker Hub tags,
RSS feeds, web pages, …) can be added without touching the schema or the
scheduler.

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Show usage |
| `/track owner/repo [--tag X] [--pre]` | Start tracking. Accepts `owner/repo`, `https://github.com/owner/repo[/...]`, `git@github.com:owner/repo.git`. The current latest release is stored as a baseline, so you only get notified about **new** releases. Running it again with different flags updates the options and resets the baseline. |
| `/untrack owner/repo` | Stop tracking |
| `/list` | Tracked items with their last known version |
| `/check` | Run a check right now (for all tracked items), then report a summary |

**Which release the bot follows** is decided automatically for each repo, using the most specific signal available:

1. **The release GitHub labels "Latest".** Maintainers choose this label, so repos that publish patch releases for older lines in parallel (e.g. `paritytech/polkadot-sdk`) only notify you when the main line moves.
2. If no release has that label: **the newest non-pre-release**.
3. If the repo has no releases at all: **the newest git tag** (turn this off with `GITHUB_FALLBACK_TO_TAGS=false`).

Add `--pre` to follow the newest release instead, pre-releases and RCs included.

**Choosing manually with `--tag`** (case-insensitive; `--tag x` and `--tag=x` both work):

| You type | Notifies only for |
|---|---|
| `--tag latest` | the release with GitHub's **Latest** badge |
| `--tag release` | normal releases (no pre-releases), newest first |
| `--tag pre-release` (or `pre`) | pre-releases only |
| `--tag <text>` | releases (or tags, for repos without releases) whose tag or release name **contains** the text, e.g. `--tag nightly` |
| `--tag <glob>` | names matching a wildcard pattern, e.g. `--tag polkadot-stable*` or `--tag v3.*` |

Examples:

```
/track https://github.com/paritytech/polkadot-sdk/releases/ --tag latest
/track offchainlabs/nitro --tag release
/track paritytech/polkadot-sdk --tag polkadot-stable2603*
```

`--tag` and `--pre` can't be combined (use `--tag pre-release` instead). Telegram
apps often autocorrect `--` into `—`, and the bot accepts that too.

You can put the tracker kind first, e.g. `/track github owner/repo`. It defaults to `github`.

## Setup

### 1. Create the bot (BotFather)

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Pick a display name and a username ending in `bot`.
3. Copy the token it gives you (`123456789:AA...`). This is `TELEGRAM_BOT_TOKEN`.
4. Optional: if you'll add the bot to a group and want it to see only commands,
   leave privacy mode on (the default).

The bot registers its command menu (`/track`, `/list`, …) on startup.

**Finding your chat ID** for `ALLOWED_CHAT_IDS`: start the bot without the
allowlist, send it any command, and look for `chat_id` in the logs. You can also
message [@userinfobot](https://t.me/userinfobot). Group IDs are negative.

### 2. Configure

```bash
cp .env.example .env
$EDITOR .env
```

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **required** | From BotFather |
| `GITHUB_TOKEN` | – | Optional. A fine-grained PAT with no permissions works for public repos. Raises the limit from 60 to 5000 req/h. |
| `CHECK_INTERVAL_SECONDS` | `900` | Poll interval, minimum 60 |
| `DB_PATH` | `data/tracker.db` | SQLite file. In Docker it is `/data/tracker.db`, which lives on a volume. |
| `ALLOWED_CHAT_IDS` | – | Comma-separated chat IDs. If set, updates from every other chat are ignored and logged. |
| `GITHUB_FALLBACK_TO_TAGS` | `true` | For repos with no releases, track git tags instead |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs each GitHub response and the remaining rate limit |
| `LOG_FORMAT` | `json` | `json` (one object per line) or `text` (human-readable key=value) |

### 3a. Run locally

Requires Python 3.12+.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m bot.main          # reads .env automatically
.venv/bin/python -m pytest            # tests, no network needed
```

With uv: `uv venv -p 3.12 && uv pip install -r requirements-dev.txt`.

### 3b. Run with Docker (e.g. on a Debian server)

```bash
# Debian: apt install docker.io docker-compose-plugin   (or Docker's official repo)
git clone <this repo> && cd tracker
cp .env.example .env && $EDITOR .env
docker compose up -d --build
docker compose logs -f
```

The SQLite DB lives on the named volume `tracker-data` at `/data/tracker.db`, so
it survives rebuilds and `docker compose down`. **`docker compose down -v` deletes it.**

Backup: `docker compose exec bot python -c "import sqlite3; sqlite3.connect('/data/tracker.db').backup(sqlite3.connect('/data/backup.db'))"`
then `docker compose cp bot:/data/backup.db ./`.

If you'd rather use a host directory, replace the volume with `./data:/data` and
`chown 10001 ./data` (the container runs as uid 10001).

Upgrading: `git pull && docker compose up -d --build`. Schema migrations run automatically on start.

## How it works

```
job_queue (every CHECK_INTERVAL_SECONDS) or /check
  └─ Checker.run
       ├─ group tracked_items by (kind, target)
       ├─ tracker.fetch(target)            # once per unique target, max 4 in parallel
       └─ per item:
            latest = tracker.select_latest(versions, item.options)
            if latest.id != item.last_version → send → on success, store latest.id
```

* **One fetch per target.** `fetch()` returns every candidate version and
  `select_latest()` applies per-item options. A repo tracked by five chats, some
  with `--pre` and some without, costs one request.
* **Conditional requests.** GitHub responses are cached by ETag in memory and
  re-requested with `If-None-Match`. A `304 Not Modified` doesn't count against the
  rate limit. The cache is cleared on restart, so the first cycle after a restart is
  unconditional.
* **Rate limits.** A 429, or a 403 that shows rate-limit signs (`x-ratelimit-remaining: 0`,
  `retry-after`, or "rate limit" in the body), puts the tracker into backoff until
  `retry-after` or `x-ratelimit-reset` (default 60s). No requests are sent during
  backoff. A warning is logged and the affected targets are retried on the next cycle.
* **Failures never crash the job.** Network errors, 5xx responses and bad JSON are
  logged per target, and other targets still get processed. A missing repo (404) is
  logged as a warning.
* **Delivery.** A version is only marked as seen after the message is accepted, so
  transient Telegram errors (network, flood control) are retried on the next cycle.
  If a user blocked the bot (`Forbidden`) or the chat is gone, that's logged and the
  version is marked as seen so it isn't retried forever.
* **Drafts** are always skipped. **Pre-releases** are skipped unless the item was
  tracked with `--pre`.
* Finding the **"Latest" label** costs one extra request to `/releases/latest`,
  which is also ETag-cached. It's skipped when every item tracking the repo uses `--pre`.

## Project layout

```
bot/
  main.py                 wiring, handlers, access gate, job scheduling, Telegram sender
  config.py               env var parsing
  log.py                  JSON / key=value structured logging
  db.py                   SQLite store + migrations (PRAGMA user_version)
  checker.py              change detection engine (no Telegram dependency)
  trackers/
    __init__.py           TrackerRegistry + "[kind] <target> [--flags]" parsing
    base.py               Tracker ABC, Version, errors, excerpt()
    github_release.py     GitHubReleaseTracker
tests/
```

## Adding a new tracker

The DB stores `kind` + `target` + `options` (JSON) + `last_version`, so a new
tracker needs **no schema change**. Example: Docker Hub tags.

1. Create `bot/trackers/dockerhub.py`:

   ```python
   import html, re
   import httpx
   from .base import FetchError, InvalidTarget, TargetNotFound, Tracker, Version

   class DockerHubTagTracker(Tracker):
       kind = "docker"                                   # stored in DB, used as /track docker ...
       description = "Docker Hub image tags"
       flags = {}                                        # e.g. {"--all": "include non-semver tags"}

       def __init__(self, client: httpx.AsyncClient):
           self._client = client

       def normalize(self, arg: str) -> str:
           arg = arg.strip().lower().removeprefix("docker.io/")
           if not re.fullmatch(r"[a-z0-9._-]+(/[a-z0-9._-]+)?", arg):
               raise InvalidTarget(f"Not a Docker Hub image: {arg!r}")
           return arg if "/" in arg else f"library/{arg}"

       async def fetch(self, target: str, requested=()) -> list[Version]:
           try:
               r = await self._client.get(
                   f"https://hub.docker.com/v2/repositories/{target}/tags",
                   params={"page_size": 25, "ordering": "last_updated"},
               )
           except httpx.HTTPError as exc:
               raise FetchError(str(exc)) from exc
           if r.status_code == 404:
               raise TargetNotFound(target)
           if r.status_code >= 400:
               raise FetchError(f"HTTP {r.status_code}")
           return [Version(id=t["name"], name=t["name"], label="tag",
                           url=f"https://hub.docker.com/r/{target}/tags")
                   for t in r.json()["results"]]

       # select_latest: the default returns versions[0]. Override it to filter
       # (semver only, skip "latest", honour per-item options, ...).

       def format_message(self, target: str, version: Version) -> str:
           return f"🐳 New tag for <b>{html.escape(target)}</b>: <code>{html.escape(version.id)}</code>"

       def target_url(self, target: str) -> str:
           return f"https://hub.docker.com/r/{target}"
   ```

2. Register it in `build_registry()` in `bot/main.py`:

   ```python
   GitHubReleaseTracker(http, ...),
   DockerHubTagTracker(http),
   ```

3. Add tests under `tests/` using `httpx.MockTransport` (see `tests/test_github_tracker.py`).

Things to keep in mind:

* `Version.id` is what gets compared and stored, so it has to be stable. Use a tag
  name, a feed GUID or a content hash. **Webpage-change** trackers should hash the
  normalised content and use the hash as `id`. **RSS** trackers can use the newest
  entry's GUID.
* Raise `TargetNotFound`, `RateLimited(msg, retry_at)` or `FetchError` from
  `fetch()`. Anything else still won't crash the job, but it gets logged with a traceback.
* `format_message()` returns Telegram **HTML**, so escape everything with `html.escape`.
* Use `parse_options()` for per-item settings. Boolean flags go in `flags`;
  flags that take a value (`--tag latest`) go in `value_flags`.
  They're stored as JSON in `options` and passed to `select_latest()`.
* `fetch(target, requested)` gets the options of **every** item tracking that
  target. Use it to fetch extra data only when some item needs it. The GitHub
  tracker does this: it skips the `/releases/latest` call when every item uses `--pre`.
* `/track` calls `fetch()` + `select_latest()` to store the baseline, so the
  user isn't notified about something that already existed.
