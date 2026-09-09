# RallyBot — Agent Context

Discord bot for the **chicago-anime-hangouts** Meetup group that mirrors Meetup events into Discord Scheduled Events and runs a user-report / moderation workflow. Single-tenant and deliberately hardcoded: one guild (`1219601473948614737`), one Meetup group slug, one DynamoDB table. Changing tenant = editing constants in the source (locations noted below).

## Design principles

- **DynamoDB is the source of truth for the mapping.** Every Meetup event (sort key = Meetup guid, partition `id="event"`) stores the Discord snowflake it was published as. Sync reconciles both directions: Meetup changes edit the Discord event; Meetup cancellations/404s delete from both DDB and Discord; Discord events deleted out-of-band are detected and recreated.
- **Single-table design** via PynamoDB (`aws.py`): `MeetupEvent`, `Report`, `Intro`, `Welcome`, and `RulesMessage` share the `RallyBot` table, partitioned by the `id` string (`"event"` / `"report"` / `"intro"` / `"welcome"` / `"rules"`). `RulesMessage` (sort 0) points at the canonical rules-acceptance button message in #rules and is what makes `onboarding.ensure_rules_message` idempotent across restarts — plain GetItem, no GSI needed.
- **Scrape-first, no Meetup API key.** Events come from the RSS feed, then each event page's embedded `script#__NEXT_DATA__` JSON (`_meetup_url_to_json` in `events.py`). This is intentionally fragile-but-accepted; it's a known trade-off, not something to "fix" casually.
- **Dual-write consistency**: every mutation touches both sides (DDB row + Discord event) with per-side error handling — a Discord-side failure must not abort the DDB write and vice versa.
- **Announcements are deduped and silencable**: `shared.message_channel` keeps a 5-message deque to prevent duplicate pings; `QUIET_RALLY` env var turns every send into a log-only dry run.
- **Keep blocking I/O off the event loop.** All `requests` calls (Meetup scrape, AI categorizer) must run via `asyncio.to_thread` from async call sites — direct blocking calls stall discord.py heartbeats.

## Tech stack

- Python 3.10+ syntax; Docker image runs `python:3.13-slim`. **Tabs for indentation**, not spaces.
- discord.py ~2.7.1 (app commands, scheduled events, modals/views incl. UserSelect-in-modal, requires 2.6+ for selects in modals), PynamoDB ~6.1 (ORM) over boto3, APScheduler ~3.11 (AsyncIOScheduler), requests + BeautifulSoup/lxml for scraping.
- No web framework, no queue — one long-running process.

## Commands

```bash
pip install -r requirements.txt   # or: uv venv .venv && uv pip install -r requirements.txt --python .venv/bin/python
python main.py                    # runs the bot; needs DISCORD_TOKEN + AWS creds
python events.py                  # debug harness: rechecks all tracked events against Meetup (sleeps 5s per event)
python tests/tests_import.py && python tests/tests_forum_pilot.py && python tests/tests_intro_guard.py   # offline test scripts (run from repo root; they bootstrap sys.path)
docker build -t rallybot .        # non-root user; copies the 5 .py files explicitly — add new modules to the Dockerfile COPY line
docker run -d -e DISCORD_TOKEN=... -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... --name rallybot rallybot
```

No CI. Verification = the offline test scripts in `tests/` (above) plus `python -m py_compile *.py`.

## Configuration

Read via `os.getenv` only — no dotenv loading, no config file.

- Required: `DISCORD_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (`AWS_SESSION_TOKEN` for temp creds)
- `DO_AI_ENDPOINT` / `DO_AI_SECRET` — DigitalOcean inference endpoint for event categorization; unset ⇒ everything lands in category `other` (logged, not fatal)
- `MOD_CHANNEL` — channel name for report notifications (default `moderator-only`)
- `QUIET_RALLY` — any non-empty value ⇒ silent/dry-run mode

## Architecture

```
main.py       entrypoint: client, intents (incl. members), cron jobs (update_events daily 12:30 CT, notify_events hourly, weekly digest sundays 3pm CT when FORUM_PILOT), create/update/announce flow, onboarding event hooks
events.py     MeetupEvent model + RSS scrape → __NEXT_DATA__ JSON → upsert; cancellation checks; AI categorizer
forum.py      forum pilot (gated by FORUM_PILOT): event posts in #event-chat forum (create/update/delete + tags), weekly announcements digest builder/sender, backfill for pre-pilot events
report.py     Report model + "Report…" context menu, mod-channel action buttons (ignore/warn/timeout/ban), escalating timeouts, /rb warn slash command
onboarding.py New-user flow: #intro welcome on join, persistent "agree to rules" button in #rules, intro role grant/removal, one-intro-post enforcement
aws.py        RallyBotModel (PynamoDB base: table RallyBot, us-east-2, keys id+sort); raw boto3 get/delete helpers
shared.py     Singleton: client, guild, channel-name cache, scheduler, message_channel (dedupe + quiet mode), role mention constants
```

Flow: RSS feed → per-event page scrape → PynamoDB upsert → Discord scheduled-event create/edit → forum post in #event-chat (pilot) or at-mention announcement in the category channel (legacy), with online/in-person tagging either way.

## Conventions & absence notes

- **AWS infra is not in this repo.** The `RallyBot` table and both GSIs (`timestamp-index` on `timestamp`, `snowflake_id-index` on `snowflake_id`) must be created manually — don't look for Terraform/CloudFormation, and new index queries need a matching GSI.
- **All deployment identity is hardcoded**: guild ID + role mention IDs in `main.py` (duplicates of `shared.py` constants removed), Meetup group slug in `events.py`, forum + announcements channel IDs in `forum.py`, table/region in `aws.py`, mod role ID in `report.py`. Env vars only carry secrets/toggles.
- `MeetupEvent` pilot fields: `forum_thread_id` (thread snowflake of the #event-chat post, `null` when unposted) and `created_at` (first-scheduled time; set only on true creation in `events.py` and `from_discord_event`, never refreshed on updates — the weekly digest's "newly planned" section depends on it). No new GSI needed: the digest scans `timestamp-index`.
- **30-day hold-back**: events starting more than a month out get no forum post and no "newly planned" digest alert (`forum.FORUM_POST_WINDOW` / `forum.event_in_post_window`); the daily `update_events` run creates held-back posts via `forum.ensure_forum_post` once they enter the window, and the first digest after that lists them as newly planned (detected via the thread snowflake's creation time, no extra ddb field).
- **No tests, no CI, no lint config.** Keep changes verified by compile + manual reasoning; don't scaffold a test framework unasked.
- `requirements.txt` is hand-pinned (`~=`);
- Meetup payloads: `meetup_event_sample.json` documents the `__NEXT_DATA__` → `props.pageProps.event` shape.
- Datetimes are stored as UTCDateTimeAttribute + a parallel millisecond `timestamp` number (for the GSI); see `MeetupEvent.start_time` property — keep both in sync when touching event timing.
- Discord API limits encoded in code: descriptions truncated to 999 chars (+ link), locations to 99 chars.
- The mod action buttons gate on `moderate_members`/`ban_members`/administrator — preserve that check when touching `report.py` views.
