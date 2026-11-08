# RallyBot — Agent Context

Discord bot for the **chicago-anime-hangouts** Meetup group that mirrors Meetup events into Discord Scheduled Events and runs a user-report / moderation workflow. Single-tenant and deliberately hardcoded: one guild (`1219601473948614737`), one Meetup group slug, one DynamoDB table. Changing tenant = editing constants in the source (locations noted below).

## Design principles

- **DynamoDB is the source of truth for the mapping.** Every Meetup event (sort key = Meetup guid, partition `id="event"`) stores the Discord snowflake it was published as. Sync reconciles both directions: Meetup changes edit the Discord event; Meetup cancellations/404s delete from both DDB and Discord; Discord events deleted out-of-band are detected and recreated.
- **Single-table design** via PynamoDB (`aws.py`): `MeetupEvent`, `Report`, `Intro`, `Welcome`, and `RulesMessage` share the `RallyBot` table, partitioned by the `id` string (`"event"` / `"report"` / `"intro"` / `"welcome"` / `"rules"`). `RulesMessage` (sort 0) points at the canonical rules-acceptance button message in #rules and is what makes `onboarding.ensure_rules_message` idempotent across restarts — plain GetItem, no GSI needed.
- **Scrape-first, no Meetup API key.** Events come from embedded `script#__NEXT_DATA__` JSON on Meetup pages: the production fetch (`fetch_meetup_events` in `events.py`) reads the events list page's apollo state, cancellation rechecks read single-event pages, and `fetch_meetup_events_rss` keeps the legacy RSS → per-event-page path. This is intentionally fragile-but-accepted; it's a known trade-off, not something to "fix" casually.
- **Dual-write consistency**: every mutation touches both sides (DDB row + Discord event) with per-side error handling — a Discord-side failure must not abort the DDB write and vice versa.
- **Announcements are deduped and silencable**: `shared.message_channel` keeps a 5-message deque to prevent duplicate pings; `QUIET_RALLY` env var turns every send into a log-only dry run.
- **Keep blocking I/O off the event loop.** All `requests` calls (Meetup scrape, `llm.ai_categorize`) must run via `asyncio.to_thread` from async call sites — direct blocking calls stall discord.py heartbeats.

## Tech stack

- Python 3.10+ syntax; Docker image runs `python:3.13-slim`. **Tabs for indentation**, not spaces.
- discord.py ~2.7.1 (app commands, scheduled events, modals/views incl. UserSelect-in-modal, requires 2.6+ for selects in modals), PynamoDB ~6.1 (ORM) over boto3, APScheduler ~3.11 (AsyncIOScheduler), requests + BeautifulSoup/lxml for scraping.
- No web framework, no queue — one long-running process.

## Commands

```bash
pip install -r requirements.txt   # or: uv venv .venv && uv pip install -r requirements.txt --python .venv/bin/python
python main.py                    # runs the bot; needs DISCORD_TOKEN + AWS creds
python events.py                  # debug harness: rechecks all tracked events against Meetup (sleeps 5s per event)
python tests/tests_import.py && python tests/tests_forum_pilot.py && python tests/tests_intro_guard.py && python tests/tests_adhoc.py && python tests/apollo_fetch_test.py   # offline test scripts (run from repo root; they bootstrap sys.path)
.venv/bin/python tests/live_meetup_test.py   # LIVE smoke test: real meetup.com fetch (list page + 2 event pages + rss); no AWS/Discord writes; exit 0 pass / 1 broken / 2 inconclusive
docker build -t rallybot .        # non-root user; copies the 9 .py files explicitly — add new modules to the Dockerfile COPY line
docker run -d -e DISCORD_TOKEN=... -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... -e OPENROUTER_API_KEY=... --name rallybot rallybot
```

No CI. Verification = the offline test scripts in `tests/` (above) plus `python -m py_compile *.py`. A weekly Hermes cron job ("RallyBot meetup scrape weekly check") runs `tests/live_meetup_test.py` against live Meetup and opens a `fix/meetup-scrape-<date>` branch when it exits 1.

## Configuration

Read via `os.getenv` only — no dotenv loading, no config file.

- Required: `DISCORD_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (`AWS_SESSION_TOKEN` for temp creds)
- `OPENROUTER_API_KEY` — OpenRouter API key for AI features (event categorization); unset ⇒ everything lands in category `other` (logged, not fatal)
- `OPENROUTER_MODEL` — OpenRouter model ID (default `deepseek/deepseek-v4.1-flash`)
- `MOD_CHANNEL` — channel name for report notifications (default `moderator-only`)
- `QUIET_RALLY` — any non-empty value ⇒ silent/dry-run mode

## Architecture

```
main.py       entrypoint: client, intents (incl. members), cron jobs (update_events daily 12:30 CT, notify_events hourly, weekly announcements digest sundays 3pm CT), create/update/forum flow, onboarding event hooks, ad hoc event hooks
events.py     MeetupEvent model + list/RSS/page scrape → __NEXT_DATA__ JSON → upsert; cancellation checks
llm.py        reusable OpenRouter client: chat() helper + event categorizer (system prompt, categories, retry→other fallback) + forum-post event extractor (ai_extract_event, JSON mode)
forum.py      event posts in the #event-chat forum (create/update/delete + tags), weekly announcements digest builder/sender, startup post sync/backfill
adhoc.py      ad hoc forum events: real-time tracking of user posts in #event-chat (ForumEvent rows, id="forum_event"), AI extraction → source="forum" MeetupEvent records for cah.moe, nonpublic tag gating, replaceable status replies, startup backfill sweep; recurring posts materialize one event row per occurrence (past rows kept as history, future filled through a 30-day horizon by the daily 12:45 CT fill job, sort = (start_ms << 16) | (thread_id & 0xFFFF), per-thread lookup via snowflake_id = thread id on the existing snowflake_id-index)
report.py     Report model + "Report…" context menu, mod-channel action buttons (ignore/warn/timeout/ban), escalating timeouts, /rb warn slash command
onboarding.py New-user flow: #intro welcome on join, persistent "agree to rules" button in #rules, intro role grant/removal, one-intro-post enforcement
aws.py        RallyBotModel (PynamoDB base: table RallyBot, us-east-2, keys id+sort); raw boto3 get/delete helpers
shared.py     Singleton: client, guild, channel-name cache, scheduler, message_channel (dedupe + quiet mode), role mention constants
```

Flow: events list page scrape → PynamoDB upsert → Discord scheduled-event create/edit → forum post in #event-chat with online/in-person tagging → weekly announcements digest. User-created #event-chat posts flow separately in real time: adhoc pipeline → AI extraction → ad hoc event record (source="forum") → cah.moe.

## Conventions & absence notes

- **AWS infra is not in this repo.** The `RallyBot` table and both GSIs (`timestamp-index` on `timestamp`, `snowflake_id-index` on `snowflake_id`) must be created manually — don't look for Terraform/CloudFormation, and new index queries need a matching GSI.
- **All deployment identity is hardcoded**: guild ID + role mention IDs in `main.py` (duplicates of `shared.py` constants removed), Meetup group slug in `events.py`, forum + announcements channel IDs in `forum.py`, table/region in `aws.py`, mod role ID in `report.py`. Env vars only carry secrets/toggles.
- `MeetupEvent` forum fields: `forum_thread_id` (thread snowflake of the #event-chat post, `null` when unposted) and `created_at` (first-scheduled time; set only on true creation in `events.py` and `from_discord_event`, never refreshed on updates — the weekly digest's "newly planned" section depends on it). No new GSI needed: the digest scans `timestamp-index`.
- **`MeetupEvent.source` discriminates ad hoc events** (`"meetup"` default, `"forum"` for records extracted from user posts in #event-chat by `adhoc.py`; rows from before the field existed deserialize as `None`, so guards must use `(event.source or "meetup")`). Ad hoc rows: sort key = thread snowflake, `link` = the Discord forum post URL, `snowflake_id = 0`, no `forum_thread_id`, no Discord scheduled event. Every meetup-facing path must skip `source="forum"` rows (guards live in `events.check_existing_event`, `forum.create_forum_post`/`sync_forum_posts`, and `main.update_events`' reconciliation) — a meetup recheck of an ad hoc row would 404-delete it. Ad hoc records are joined to their posts via `adhoc.ForumEvent` rows (`id="forum_event"`, sort = thread snowflake); the forum's `nonpublic` tag deletes/recreates the event record, and a startup sweep backfills + reconciles.
- **Single-table scan trap:** `SomeModel.scan()` on the shared table deserializes EVERY row (`event`/`report`/`intro`/...) as that model with its attributes `None`. Any scan-based reconciliation must filter on the partition discriminator (`row.id`/`row.thread_id` is None → not our partition) before acting.
- **Recurring forum posts (standing events):** the extractor's `recurrence` field (validated by `adhoc._parse_recurrence`; anything malformed → treated as one-off) drives `adhoc._materialize_occurrences`, which creates one `source="forum"` event row per occurrence through `OCCURRENCE_HORIZON` (30d). **History is immutable**: edits delete only FUTURE occurrence rows and re-materialize; nonpublic/deleted posts remove past AND future rows for that thread. The past-event reanalysis guard applies to one-off rows only (`row.recurrence is None`). `created_at` is stamped only on a series' first-ever row so the weekly digest's "newly planned" fires once per series, not per occurrence. The cah.moe Lambda keys off `timestamp` on `id="event"` rows and treats `sort` as opaque — occurrence rows need no site change.
- **30-day hold-back**: events starting more than a month out get no forum post and no "newly planned" digest alert (`forum.FORUM_POST_WINDOW` / `forum.event_in_post_window`); the daily `update_events` run creates held-back posts via `forum.ensure_forum_post` once they enter the window, and the first digest after that lists them as newly planned (detected via the thread snowflake's creation time, no extra ddb field).
- **No CI, no lint config.** Verification = compile + the offline test scripts in `tests/`; don't scaffold a test framework unasked — the scripts bootstrap sys.path and stub discord/AWS.
- `requirements.txt` is hand-pinned (`~=`);
- Meetup payloads: `meetup_event_sample.json` documents the `__NEXT_DATA__` → `props.pageProps.event` shape.
- Datetimes are stored as UTCDateTimeAttribute + a parallel millisecond `timestamp` number (for the GSI); see `MeetupEvent.start_time` property — keep both in sync when touching event timing.
- Discord API limits encoded in code: descriptions truncated to 999 chars (+ link), locations to 99 chars.
- The mod action buttons gate on `moderate_members`/`ban_members`/administrator — preserve that check when touching `report.py` views.
