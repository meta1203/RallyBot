# Ad Hoc Forum Events (user-created posts → cah.moe) — Implementation Plan

> **For Hermes:** Execute directly in-session, task-by-task (user preference — no subagent dispatch).

**Goal:** Track user-created event posts in the #event-chat forum in real time, use AI to extract event info from each post into a DynamoDB event record (so cah.moe picks it up), and keep that record in lockstep with the post (edits → recompute, `nonpublic` tag → delete/recreate, post deletion → delete).

**Architecture:** New module `adhoc.py` owns a `ForumEvent` tracking row (id=`"forum_event"`, sort = forum thread snowflake) per user post plus the processing pipeline. `MeetupEvent` gains a `source` field (`"meetup"` default, `"forum"` for ad hoc records, sort key = thread snowflake) so existing Meetup reconciliation/sync code can skip them. The extractor is a new `llm.ai_extract_event()` JSON-mode call over the existing `llm.chat()`. Status notices are posted as a single replaceable reply per thread (the previous bot reply is deleted when a new one is posted); the reply's message id is tracked on the `ForumEvent` row. All triggers are real-time discord.py events plus a startup backfill/reconciliation sweep (covers posts created while the bot was offline — the user's addendum replaced the original 5pm cron with "everything real-time").

**Tech Stack:** No new dependencies. discord.py 2.7.1 raw/thread events, PynamoDB single-table, OpenRouter via `llm.chat()`, `hashlib` for change detection.

**Decisions locked in by the user (do not re-open):**
- Notices are **public in-thread replies that persist** until the bot replies again in that thread (then the old reply is deleted and the new one posted). Reply message id lives on the `ForumEvent` row.
- **Everything is a real-time handler**, including initial post creation. No daily 5pm cron. A startup sweep backfills anything missed while offline.
- Ad hoc events flow to **website + weekly digest + all event views**.
- Backfill processes **all existing posts** (past ones too — analyzed once, recorded).
- Extraction floor: **start datetime + location** ("Online"/"Discord" counts as a location). Title and description may be AI-derived; preserve the original post title when it reads as the event title.

**Assumptions decided by the agent (low-stakes, stated so they're reviewable):**
- Timezone: posts stating a bare time ("7pm") are assumed `America/Chicago` — single-tenant Chicago group, matches `shared.central_time`.
- Past-event guard blocks **recomputation** only. `nonpublic` tag add/remove and thread deletion still mutate records for past events (deletion isn't recomputation). Tag-removal recreation for past events uses the last stored extraction (no AI call — see `last_extraction` field).
- A recompute that drops below the floor (edit removed the date) deletes the now-stale event record and posts the missing-info reply.
- A transient AI failure (network/HTTP/parse) posts **nothing** and leaves the row unprocessed — the next trigger or startup sweep retries. Only a *successful* extraction lacking floor fields produces the missing-info reply.
- Non-event posts (chit-chat in the forum) are recorded with state `not_event` and get no reply, no AI call on later triggers (hash-guarded).
- The bot never edits the user's post — `forum.py` create/update paths get a `source="forum"` early-return guard.
- `snowflake_id` stays `0` on ad hoc events: **no Discord Scheduled Event is created** for them (spec only asks for the website record).

---

## Verified facts (probed live, not from docs)

- **Gate (run once, green):** `.venv/bin/python -m py_compile *.py` passes; all four offline scripts pass with `.venv/bin/python` (`tests/tests_import.py`, `tests/tests_forum_pilot.py`, `tests/tests_intro_guard.py`, `tests/apollo_fetch_test.py`). System python lacks boto3 — **all test steps must use `.venv/bin/python`**.
- discord.py **2.7.1** installed. `state.py:parse_thread_create` dispatches **`on_thread_create(thread)`** for `newly_created` forum threads (thread carries `parent_id`, `owner_id`, `applied_tags`, `name`). `parse_thread_update` dispatches **`on_raw_thread_update(raw)`** always, and `on_thread_update(old, new)` only when the thread was cached. `parse_thread_delete` dispatches **`on_raw_thread_delete(raw)`**.
- `RawThreadUpdateEvent` attrs: `data, guild_id, parent_id, thread, thread_id, thread_type`. `RawThreadDeleteEvent`: `guild_id, parent_id, thread, thread_id, thread_type`. Message edits dispatch `on_raw_message_edit` with `RawMessageUpdateEvent` (`cached_message, channel_id, data, guild_id, message, message_id`) — **`channel_id` is the thread's id, and a forum post's starter message id equals the thread id**, so `payload.message_id == payload.channel_id` identifies a starter edit.
- There is **no** `RawThreadCreateEvent`/`on_raw_thread_create` in 2.7.1 — use `on_thread_create` for creation.
- `aws.RallyBotModel`: `id` (str hash key), **`sort` is `NumberAttribute`** (a snowflake fits fine), plus a generic `data` string attr. `DynamoDBClient.read_raw/delete_raw` exist for salvage paths.
- `MeetupEvent.__init__` forces `self.id = "event"`; `created_at` is stamped only on true creation (weekly digest's "newly planned" depends on it); `start_time` setter keeps `datetime` + ms `timestamp` in sync — always set times via `event.start_time = ...`.
- `forum.py` constants/helpers reusable: `FORUM_CHANNEL_ID` (1543307748006174920), `get_forum_channel()`, `_create_forum_tag()`, `FORUM_THREAD_NAME_MAX_LEN`. `forum.create_forum_post` is called from `main.notify_new_event`, `ensure_forum_post`, and internally from `update_forum_post` — **one guard inside `create_forum_post` covers every call site**; `sync_forum_posts` scans `timestamp-index` and calls create/update per row, so it needs its own skip.
- `MeetupEvent.delete()` (async) deletes the Discord scheduled event **and** calls `forum.delete_forum_post` — both are harmless no-ops for ad hoc records (`snowflake_id` falsy → skip; `forum_thread_id` None → early return). Reuse it for ad hoc record deletion.
- `main.update_events`' reconciliation loop re-checks every future tracked event **not** in the day's Meetup scrape via `events.check_existing_event`, which 404-deletes anything whose Meetup page is gone — ad hoc events **would be wiped by this** without a `source` guard in `check_existing_event` (put it there; it covers both call paths).
- The bot does **not** request the `message_content` gateway intent (`main.py:16-21`), so cached/gateway message `.content` from other users may be empty. **REST fetches always return full content** — the pipeline must `await thread.fetch_message(thread.id)` (starter id == thread id) before hashing/extraction, never trust `thread.starter_message.content` from cache.
- `Message.edited_at` (message.py:529/592) is parsed from `edited_timestamp` on **both** gateway `MESSAGE_UPDATE` payloads and REST fetches — `thread.fetch_message(thread.id).edited_at` is reliable without any intent, and `RawMessageUpdateEvent.data` also carries `edited_timestamp` directly. It is `None` for never-edited messages. It can only be read from the message object, so it cannot replace the content hash (which needs the same fetch); it serves as a free secondary "modified since seen" signal stored alongside the hash.
- Digest integration needs **no code change**: `_build_digest_messages` scans `timestamp-index` for future events, `_post_link` falls back to `event.link` when `forum_thread_id` is None (the ad hoc link IS the forum post URL), and `created_at` presence drives "newly planned".
- `llm.chat(messages)` is blocking — all call sites go through `asyncio.to_thread` (repo-wide rule; the same applies to every PynamoDB call from async code).
- Dockerfile COPY line (currently): `COPY main.py shared.py events.py llm.py aws.py report.py onboarding.py forum.py ./` — **append `adhoc.py`**.
- AGENTS.md edits require live approval on this host — surface the diff when executing Task 9.
- Repo style: **tabs**, no pytest, offline scripts in `tests/` bootstrap `sys.path` and stub discord/PynamoDB (see `tests/tests_forum_pilot.py:16-101` for the fake-layer pattern to copy).

---

## Data model

### `ForumEvent` (new, in `adhoc.py`) — tracks a forum post, per the user's requested schema

| attribute | type | notes |
|---|---|---|
| `thread_id` | `NumberAttribute` | == `sort`; forum thread snowflake |
| `event_sort` | `NumberAttribute(null=True)` | sort key of the linked `MeetupEvent` row (always == thread_id when present); None = no record |
| `state` | `UnicodeAttribute(null=True)` | `event` / `missing_info` / `not_event` / `nonpublic` |
| `missing_fields` | `UnicodeAttribute(null=True)` | comma-joined missing floor fields, for the missing-info reply |
| `last_extraction` | `UnicodeAttribute(null=True)` | JSON of the last successful extraction (enables tag-removal recreation without reanalyzing past events) |
| `last_analyzed_hash` | `UnicodeAttribute(null=True)` | sha256 of `name + starter content` only (tag ids deliberately excluded: the nonpublic gate handles tag toggles on every trigger before change detection, and tags-in-hash would break the no-AI tag-removal fast path by forcing a reanalysis of possibly-past events); primary change-detection guard |
| `last_edited_at` | `UTCDateTimeAttribute(null=True)` | Discord's own `edited_timestamp` of the starter at last analysis; secondary "modified since seen" signal (never-edited posts are None) |
| `last_analyzed_at` | `UTCDateTimeAttribute(null=True)` | |
| `reply_message_id` | `NumberAttribute(null=True)` | the bot's current status reply in the thread (deleted before the next reply is posted) |
| `nonpublic_seen` | `BooleanAttribute(default=False)` | True once the "skipped" reply has fired for the current run of `nonpublic` being applied; reset to False when the tag is removed (so re-adding it re-alerts) |
| `created_at` | `UTCDateTimeAttribute(null=True)` | when the post was first seen |

`__init__` forces `self.id = "forum_event"` (mirrors `MeetupEvent`).

### `MeetupEvent` changes (in `events.py`)

```python
	# where the record came from: "meetup" (default) or "forum" (ad hoc event
	# extracted from a user post in #event-chat; sort key == thread snowflake,
	# link points at the forum post, no discord scheduled event is created)
	source = UnicodeAttribute(default="meetup")
```

Ad hoc record shape: `sort = thread_id`, `link = f"https://discord.com/channels/{shared.guild.id}/{thread_id}"`, `snowflake_id = 0`, `online`/`location`/`title`/`description`/`full_description` from extraction, `endtime = start + (end | duration | 1h)`, `created_at` stamped once on true creation, `category` from the extractor (validated against `llm.categories`), `forum_thread_id` stays **None** (the post already exists and is user-owned).

---

## AI extraction (`llm.py`)

```python
EXTRACT_SYSTEM_PROMPT = (
	"You are an event extraction bot for a Chicago anime meetup community. "
	"You will be given a Discord forum post (a title line and a body). Extract the event it describes. "
	"Reply ONLY with a single JSON object, no markdown fences, with exactly these keys:\n"
	'{"is_event": <bool — false if the post does not describe an actual event happening at a stated or inferable date/time>, '
	'"title": <string — the event title; use the original post title as-is when it reads as an event title, otherwise derive one>, '
	'"start": <string|null — start date/time as ISO 8601 WITH timezone offset; when the post gives no timezone, assume America/Chicago; null if unknown>, '
	'"end": <string|null — end date/time, same format, null if unknown>, '
	'"duration_minutes": <int|null — event duration in minutes, null if unknown>, '
	'"location": <string|null — real-world venue/address, or "Online"/"Discord" for online events, null if unknown>, '
	'"online": <bool>, '
	'"description": <string — the event description; prefer the post\'s own wording>, '
	'"category": <one of: {CATEGORIES}>, '
	'"missing": <list — which of "start" and/or "location" could not be determined; empty list when both are known>}'
)
EXTRACT_REQUIRED_FIELDS = ("start", "location")

def ai_extract_event(post_title: str, post_body: str) -> dict | None:
	"""Blocking extraction of event info from a forum post. Returns the parsed
	dict, or None when the AI call or JSON parsing fails (transient — callers
	must NOT treat this as missing information). Validate against
	EXTRACT_REQUIRED_FIELDS before trusting; start/end parse via
	dt.datetime.fromisoformat. Call via asyncio.to_thread from async code."""
```

Implementation notes:
- Build on `chat()` exactly like `ai_categorize` does (one JSON-parse retry with a corrective follow-up message, `None` after a second failure or any exception). Categories are interpolated from `categories` at call time.
- Caller-side post-processing (in `adhoc.py`, keeps llm.py pure): validate `start`/`location` non-empty; `category` not in `categories` → `"other"`; end = `end` else `start + duration_minutes` else `start + 1 hour`; clamp `title` to the 100-char thread-name limit when anything downstream needs it (website doesn't).

---

## Status replies (one replaceable slot per thread)

```python
async def post_status_reply(thread: discord.Thread, row: ForumEvent, content: str):
	"""Delete the row's previous status reply (if any), post the new one, and
	record the new message id. Silent no-op in quiet mode (ddb still updates)."""
	if row.reply_message_id:
		try:
			old = await thread.fetch_message(int(row.reply_message_id))
			await old.delete()
		except (discord.NotFound, discord.Forbidden):
			pass  # already gone or can't manage it — post the new reply anyway
		except Exception:
			print(f"ERROR: deleting old status reply {row.reply_message_id} in {thread.id}:\n{traceback.format_exc()}")
	if shared.quiet:
		print(f"(quiet mode) would post status reply in {thread.id}: {content!r}")
		return
	try:
		msg = await thread.send(content)
		row.reply_message_id = msg.id
	except Exception:
		print(f"ERROR: posting status reply in {thread.id}:\n{traceback.format_exc()}")
```

Wording (mention `<@{thread.owner_id}>` in `content` so the author is pinged — mentions in content notify, embeds don't):
- **Added:** "…Saw your post and added your event to cah.moe so website visitors can see it! 📅 If you'd rather it NOT appear on the website, add the `nonpublic` tag to this post and I'll remove it."
- **Missing info:** "…tried to add this event to cah.moe but couldn't because of missing information: **{missing_fields joined}**. Please update the post with the missing info and I'll try again!" (list exactly which of start date/time / location was missing).
- **Nonpublic skipped:** "…this post has the `nonpublic` tag, so it won't be shown on cah.moe."

All three share the single slot — a thread's newest notice replaces the old one.

---

## Core pipeline (`adhoc.py`)

```python
NONPUBLIC_TAG_NAME = "nonpublic"

def _starter_content_hash(thread, starter) -> str:
	tag_ids = ",".join(sorted(str(t.id) for t in (thread.applied_tags or [])))
	return hashlib.sha256(f"{thread.name}\x00{starter.content}\x00{tag_ids}".encode()).hexdigest()

async def _fetch_starter(thread) -> discord.Message:
	# starter message id == thread id; REST fetch returns full content even
	# without the message_content intent
	return await thread.fetch_message(thread.id)

def _has_nonpublic_tag(thread) -> bool:
	return NONPUBLIC_TAG_NAME in {t.name.lower() for t in (thread.applied_tags or [])}

async def process_thread(thread: discord.Thread, *, force: bool = False) -> None:
	"""Full pipeline for one forum post. Idempotent: unchanged posts (same
	content/tag/name hash) do nothing."""
```

`process_thread` logic, in order:
1. Skip bot-owned posts: `thread.owner_id == shared.client.user.id` → return (RallyBot's own mirrored posts are Meetup events, already tracked).
2. Load-or-create the `ForumEvent` row (`ForumEvent.get` → `DoesNotExist` → new row, `created_at = now`).
3. **Nonpublic gate** (runs on every trigger, before anything else):
   - tag applied and `not row.nonpublic_seen` → set `nonpublic_seen=True`, `state="nonpublic"`; if an event record exists, `await events.MeetupEvent(...).delete()` via load-by-sort (async delete touches only DDB for ad hoc rows); clear `event_sort`/`last_analyzed_hash`; post the nonpublic reply. **Do not return before saving the row.**
   - tag applied and `nonpublic_seen` already True → nothing (the once-per-addition rule).
   - tag absent and `nonpublic_seen` True → reset `nonpublic_seen=False`, `state=None` and fall through to normal processing (this is the tag-removal path: record gets recreated below without any AI call — step 5b).
4. REST-fetch the starter (`_fetch_starter`); compute the hash. The post counts as changed when **`hash != row.last_analyzed_hash` OR `starter.edited_at != row.last_edited_at`** (belt-and-suspenders: the hash is the primary signal and a no-op edit that only bumps `edited_timestamp` without changing content still hash-matches — but an `edited_at` mismatch alone triggers reanalysis, e.g. if the row was written before the hash existed). If `not force` and not changed → save row (in case step 3 reset the flag) and return. This is what makes re-fires (archive toggles, `on_raw_thread_update` on any attribute) cheap and no-op.
5. **Past-event guard:** if `row.event_sort` is set, load the event; if its `start_time` is in the past → save hash/`last_analyzed_at` and return (no reanalyze, no recompute).
6. **Recompute path (edit, new post, tag-removal recreation):**
   a. *Tag-removal fast path:* when `row.event_sort` is None but `row.last_extraction` exists and the previous state was `nonpublic` (tag was just removed) → rebuild the event record from the stored extraction JSON, **no AI call** (needed for past events where reanalysis is forbidden; also cheaper for future ones). Then post the added reply again.
   b. Otherwise: `extraction = await asyncio.to_thread(llm.ai_extract_event, thread.name, starter.content)`.
      - `extraction is None` → transient failure: log, save hash (so the next edit/sweep can still retry? No — save nothing; leave `last_analyzed_hash` untouched so a later trigger retries), return.
      - `not is_event` → `state="not_event"`; if an event record somehow exists, delete it; clear reply? No reply is posted (spec is silent; posting would spam non-events). Save hash. Done.
      - Missing floor (`"start"` or `"location"` missing) → if an event record exists, delete it (recompute-from-good-to-bad). Set `state="missing_info"`, `missing_fields`, post the missing-info reply, save.
      - Valid → upsert `MeetupEvent`: `sort=thread.id`, `source="forum"`, `link` = forum post URL, fields from extraction, `created_at = now` **only on true creation** (fresh `MeetupEvent.get('event', thread.id)` raise → stamp; else keep existing `created_at`), `snowflake_id=0`, `forum_thread_id=None`. Stamp `row.last_extraction` (compact JSON of the raw extraction + resolved fields). `state="event"`, post the added reply, save.
7. Always persist `row.last_analyzed_hash` / `row.last_edited_at` / `last_analyzed_at` / `state` before returning (single `row.save()` at the end of each branch).

Deletion/cleanup (shared by `on_raw_thread_delete` and the startup sweep):

```python
async def cleanup_deleted_post(thread_id: int) -> None:
	try:
		row = await asyncio.to_thread(ForumEvent.get, int(thread_id))
	except ForumEvent.DoesNotExist:
		return
	except Exception:
		return  # transient ddb hiccup — the sweep will retry
	if row.event_sort:
		try:
			ev = await asyncio.to_thread(events.MeetupEvent.get, "event", int(row.event_sort))
			await ev.delete()  # async delete: no-op discord/forum side for ad hoc rows
		except events.MeetupEvent.DoesNotExist:
			pass
	await asyncio.to_thread(row.delete)
```

---

## Discord event wiring (`main.py`)

Four small handlers plus nothing else on the hot paths — each delegates into `adhoc` with `asyncio.to_thread` only around blocking bits (the pipeline itself awaits the AI call via `to_thread` internally; PynamoDB calls inside `process_thread` are wrapped the same way):

```python
@client.event
async def on_thread_create(thread: discord.Thread):
	# user-created event posts in #event-chat -> ad hoc website records
	if thread.parent_id == forum.FORUM_CHANNEL_ID:
		await adhoc.process_thread(thread)

@client.event
async def on_raw_thread_update(payload: discord.RawThreadUpdateEvent):
	# catches tag add/remove, archive/unarchive, name edits — including
	# threads missing from cache (raw.thread may be None; fetch fresh)
	if payload.parent_id == forum.FORUM_CHANNEL_ID:
		thread = shared.guild.get_channel_or_thread(payload.thread_id) \
			or await shared.guild.fetch_channel(payload.thread_id)  # try/except NotFound
		if isinstance(thread, discord.Thread):
			await adhoc.process_thread(thread)

@client.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
	# a forum post's starter message id == its thread id: that equality
	# identifies a body edit (replies inside the thread are ignored)
	if payload.channel_id == payload.message_id and payload.guild_id:
		thread = shared.guild.get_channel_or_thread(payload.channel_id)
		if isinstance(thread, discord.Thread) and thread.parent_id == forum.FORUM_CHANNEL_ID:
			await adhoc.process_thread(thread)

@client.event
async def on_raw_thread_delete(payload: discord.RawThreadDeleteEvent):
	# parent_id is present on forum-thread deletions; fall back to the
	# ForumEvent row's existence, which only event-chat posts ever get
	if payload.parent_id == forum.FORUM_CHANNEL_ID or True:
		await adhoc.cleanup_deleted_post(payload.thread_id)
```

(The `or True` placeholder above is illustrative only — implement it as: call `cleanup_deleted_post` unconditionally; the `ForumEvent.get` gate inside makes it safe and self-limiting. Edits to replies inside a thread (`payload.message_id != payload.channel_id`) are ignored on purpose — only the post body is analyzed.)

---

## Startup (`main.on_ready`, after `forum.sync_forum_posts()`)

```python
try:
	await adhoc.startup_sweep()
except Exception:
	print(f"ad hoc event sweep failed:\n{get_stacktrace()}")
```

`adhoc.startup_sweep()` (async, sequential — gentle on OpenRouter and the Discord API):
1. **Ensure the `nonpublic` tag exists** on the forum channel: resolve by name in `available_tags`; missing → `await forum._create_forum_tag(channel, "nonpublic")` (best-effort, logged if Forbidden). Resolve tags by name at use time (tag ids differ per channel; name is the stable contract).
2. **Sweep live threads:** `await shared.guild.active_threads()` (guild-level REST call — `ForumChannel` has no `active_threads`; verified against 2.7.1) filtered to `parent_id == FORUM_CHANNEL_ID`, plus `async for t in channel.archived_threads(limit=None)`; dedupe by id and `process_thread` each. The hash guard makes already-processed posts no-ops, so this doubles as the "created while offline" backstop and the one-time backfill of all existing posts.
3. **Sweep tracked rows for deleted posts:** iterate `ForumEvent.scan()` (materialize in `to_thread`), **skipping rows whose `id != "forum_event"` or `thread_id is None`** — the table is single-design, so scan deserializes every event/report/intro row as a `ForumEvent` and those must not be reconciled. For each real row whose thread is gone (`fetch_channel` raises NotFound → cleanup; Forbidden → keep the record, fail closed), `await cleanup_deleted_post(row.thread_id)` — cleans up posts deleted while the bot was offline.
4. `await asyncio.sleep(1)` between threads in step 2 to stay under Discord's message-deletion rate limits (old-reply cleanup) and OpenRouter's free-tier pacing.

---

## Guards on existing flows (the "don't break Meetup" checklist)

1. **`events.check_existing_event`** — first line:
   ```python
	if (event.source or "meetup") != "meetup":
		return True  # ad hoc record: nothing to recheck against meetup
   ```
   Without it, the daily reconciliation 404-deletes every ad hoc event (their "meetup page" doesn't exist).
2. **`forum.create_forum_post`** — first line: `if (event.source or "meetup") == "forum": return None` (plus a comment: the post already exists and is user-owned; the bot must never rewrite it). This alone covers `notify_new_event`, `ensure_forum_post`, and the 404-recreate path in `update_forum_post` (all route through create).
3. **`forum.sync_forum_posts`** — `for event in upcoming:` add `if (event.source or "meetup") == "forum": continue` (it iterates rows directly, not through create).
4. **`main.update_events` reconciliation loop** — `if event.sort not in hashed_ids:` add the same source skip before `check_existing_event` (defense in depth; saves a pointless meetup fetch even with guard #1 in place).
5. Everything else needs **no** change: `notify_events` queries `snowflake_id-index` by scheduled-event id (0-ad hoc rows never match an id the bot queries); `MeetupEvent.delete()`'s Discord/forum side-arms early-return for ad hoc rows; the digest already renders them correctly (verified above).

---

## Tasks

### Task 1: `MeetupEvent.source` + the five guards

**Objective:** Introduce the source discriminator and make every existing Meetup flow safe for ad hoc rows.

**Files:**
- Modify: `events.py` (add `source` attr after `full_description`; guard at top of `check_existing_event`)
- Modify: `forum.py` (guard in `create_forum_post`; skip in `sync_forum_posts`)
- Modify: `main.py` (skip in `update_events` reconciliation loop)
- Test: `tests/tests_adhoc.py` (new)

**Steps:** Write the test first — checks: (a) a `MeetupEvent` with `source="forum"` passes through `check_existing_event` without any HTTP call (monkeypatch `events._meetup_url_to_json` with a bomb) and returns True; (b) `create_forum_post` with a `source="forum"` event records no thread create in the fake channel; (c) `sync_forum_posts` skips forum rows. Use the fake layer from `tests/tests_forum_pilot.py:16-101` verbatim (copy into the new script; it must subclass real discord types and stub `MeetupEvent.save` before constructing objects). Then implement the three edits.

**Verify:** `.venv/bin/python -m py_compile events.py forum.py main.py && .venv/bin/python tests/tests_adhoc.py` → all-passed marker. Run the other four offline scripts — still pass.

### Task 2: `ForumEvent` model + hash/fetch helpers in `adhoc.py`

**Objective:** The tracking-row schema and pure helpers.

**Files:**
- Create: `adhoc.py` (module docstring mirroring `forum.py`'s style; `ForumEvent` incl. `last_edited_at`; `_starter_content_hash`; `_fetch_starter`; `_has_nonpublic_tag`; `NONPUBLIC_TAG_NAME`)
- Test: `tests/tests_adhoc.py` (append)

**Verify:** compile + test (construct a `ForumEvent(sort=123)`, assert `id == "forum_event"`, round-trip a hash, `_has_nonpublic_tag` with fake tags).

### Task 3: `llm.ai_extract_event`

**Objective:** JSON-mode extraction with one retry, `None` on transient failure.

**Files:**
- Modify: `llm.py` (constants + function per the section above)
- Test: `tests/tests_adhoc.py` (append) — patch `llm.chat` with fake responses: valid JSON → dict; fenced JSON → parsed; garbage twice → None; assert the corrective retry happens once. **Stub `llm.chat` before any real call — never let tests hit OpenRouter.**

**Verify:** compile + `.venv/bin/python tests/tests_adhoc.py`.

### Task 4: Status-reply manager

**Objective:** `post_status_reply` with delete-then-post replacement and quiet mode.

**Files:**
- Modify: `adhoc.py`
- Test: `tests/tests_adhoc.py` (append) — fake thread whose `send`/`fetch_message`/`delete` record into `captured`; assert: first call posts and stores id; second call deletes the old then posts; quiet mode stores nothing but doesn't crash; deleted-old-id (NotFound) still posts.

### Task 5: `process_thread` core flow

**Objective:** New-post → event record | missing-info | not-event, with hash idempotency and past-event guard.

**Files:**
- Modify: `adhoc.py` (+ `link`/URL helper)
- Test: `tests/tests_adhoc.py` (append) — scenarios: (1) valid post → `MeetupEvent` row with `source="forum"`, `sort==thread.id`, `link` = forum URL, `endtime` = start+1h when unknown, `created_at` stamped, added reply posted, `last_extraction` stored, `last_edited_at` persisted from the starter's `edited_at`; (2) same post again → no second AI call (call counter), `created_at` unchanged; (2b) `edited_at` changed but content identical → reanalysis runs (the OR in the change check); (2c) `edited_at` bumped with identical hash bookkeeping → single AI call, no duplicate reply churn; (3) missing start → missing-info reply lists fields, **no** event row; (4) missing location only → same; (5) `is_event=false` → state not_event, no reply; (6) AI returns None → no reply, no row mutation of hash; (7) edit (different content) → recompute updates the row, `created_at` preserved; (8) edit back below floor → event row deleted + missing-info reply; (9) event in past + edit → no AI call, row untouched; (10) tag-removal fast path → record rebuilt from `last_extraction`, zero AI calls. Stub `events.MeetupEvent.get/save/delete` and `adhoc.ForumEvent.get/save/delete` per the skill's DoesNotExist rule (raise the model's own `DoesNotExist`).

### Task 6: Nonpublic tag handling

**Objective:** Add → delete + once-per-addition reply; remove → recreate.

**Files:**
- Modify: `adhoc.py` (nonpublic gate inside `process_thread`, per the pipeline order)
- Test: `tests/tests_adhoc.py` (append) — (1) tag applied → event row deleted, reply posted, `nonpublic_seen=True`, `state="nonpublic"`; (2) second trigger with tag still on → no second reply; (3) tag removed → `nonpublic_seen=False`, record recreated, added reply posted; (4) re-add tag → reply again (the once-per-addition resets); (5) nonpublic on a post with no prior record → reply only, no crash; (6) nonpublic on a past event's post → record still deleted (deletion ≠ recompute).

### Task 7: Discord event wiring in `main.py`

**Objective:** The four real-time handlers.

**Files:**
- Modify: `main.py` (import `adhoc`; four `@client.event` functions per the section above)
- Test: `tests/tests_adhoc.py` (append) — call the handler functions directly with fake payloads (subclass `discord.RawThreadUpdateEvent` / `RawMessageUpdateEvent` / `RawThreadDeleteEvent` and set the attrs; shadow read-only props as plain attributes per the skill): thread in #event-chat routes to `process_thread` (spy), other parents don't; starter-edit equality test (`message_id == channel_id` routes, reply edits don't); delete routes to `cleanup_deleted_post`.

**Verify:** import smoke test with `Client.run` stubbed (per the discord-bot-development skill — mandatory after class-adjacent edits): `.venv/bin/python -c "import discord.client; discord.client.Client.run = lambda self,*a,**k: print('(stubbed)'); import main"` — must print `(stubbed)` without exceptions.

### Task 8: Startup sweep

**Objective:** `nonpublic` tag ensure + full-thread backfill + deleted-post reconciliation.

**Files:**
- Modify: `adhoc.py` (`startup_sweep`), `main.py` (`on_ready` hook after `forum.sync_forum_posts()`)
- Test: `tests/tests_adhoc.py` (append) — fake forum channel with `active_threads`/`archived_threads` async iterators; assert every post got processed, the tag gets created when missing, a row whose thread 404s gets cleaned up, and `ForumEvent.save`/`delete` stubs never hit AWS.

**Verify:** `.venv/bin/python tests/tests_adhoc.py` all-passed; other four scripts still pass.

### Task 9: Dockerfile + AGENTS.md + full gate

**Files:**
- Modify: `Dockerfile:25` → `COPY main.py shared.py events.py llm.py aws.py report.py onboarding.py forum.py adhoc.py ./`
- Modify: `AGENTS.md` (live approval needed): architecture diagram gains an `adhoc.py` line (below `onboarding.py`: "ad hoc forum events: real-time #event-chat post tracking, AI extraction → source='forum' event rows, nonpublic tag gating, replaceable status replies"); commands block gains `tests/tests_adhoc.py`; the `MeetupEvent` notes mention the `source` field and that reconciliation/sync paths skip `source="forum"` rows.

**Verify:**
1. `.venv/bin/python -m py_compile *.py tests/*.py` → clean.
2. All five offline scripts pass via `.venv/bin/python`.
3. `docker build -t rallybot .` → succeeds; `docker run --rm rallybot python -c "import adhoc"` exits 0.

---

## Risks / tradeoffs

- **AI extraction quality** is the load-bearing bet: dates like "next Friday", DST edges, and compound events (a post listing three games on three dates) may misextract. Mitigations: floor validation, `last_extraction` round-trip so tag toggles don't re-roll the dice, hash guard so transient nonsense doesn't cascade, and the missing-info reply loop gives users a correction path. The extraction prompt instructs ISO-with-offset + CT-default explicitly.
- **Reply-deletion cadence:** Discord rate-limits bulk deletes; sequential processing with the 1s sleep in the sweep keeps it well inside limits. Live handlers replace at most one reply per trigger.
- **`on_raw_thread_update` volume:** every archive/lock/name-change on any post fires it; the hash guard makes non-material touches a single `get` + no-op save. Acceptable for a single-tenant guild.
- **Digest "newly planned" double-alerts** if a record is deleted (nonpublic) and recreated — `created_at` is re-stamped on the new row. Accepted; matches the spec's "(re)create" semantics.
- **Backfill cost:** one OpenRouter call per existing post on first boot; the hash guard means it never happens again for unchanged posts.

## Out of scope

- A `/rb` slash command to re-trigger analysis or list tracked posts (can be added later off the same pipeline).
- Caching the `nonpublic` tag's id (name resolution is cheap and id-stable-per-channel but the channel's tag list can change).
- Making `cah.moe` changes — the site already renders `source`-agnostic event rows from the table.
