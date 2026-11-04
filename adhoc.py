"""Ad hoc forum events: user-created event posts in #event-chat -> cah.moe.

Every user post in the forum channel is tracked with a ForumEvent row
(id="forum_event", sort = thread snowflake). A real-time pipeline (see
process_thread) extracts event info from the post via the AI extractor
(llm.ai_extract_event) and writes an ad hoc MeetupEvent record
(source="forum", sort = thread snowflake, link = the forum post URL) that
cah.moe renders alongside the Meetup-mirrored events. No discord scheduled
event is created and the bot never rewrites the user's post.

Lifecycle, all real-time (a startup sweep backfills anything missed while
the bot was offline):
- post created  -> analyze; valid -> event record + status reply;
  missing start/location -> public reply listing what's missing
- post edited   -> reanalyze + recompute (unless the event already started)
- `nonpublic` tag added   -> event record deleted + once-per-application reply
- `nonpublic` tag removed -> record recreated from the last extraction
- post deleted  -> ForumEvent row + event record deleted

Status notices are a single replaceable reply per thread: posting a new one
deletes the previous bot reply (reply_message_id is tracked on the row).
"""
import asyncio
import datetime as dt
import hashlib
import json
import traceback

import discord

import events
import forum
import llm
from aws import RallyBotModel
from shared import shared

from pynamodb.attributes import UnicodeAttribute, NumberAttribute, UTCDateTimeAttribute, BooleanAttribute

NONPUBLIC_TAG_NAME = "nonpublic"
FORUM_SOURCE = "forum"
SWEEP_DELAY = 1.0  # seconds between sweep threads (delete/AI rate limits)


class ForumEvent(RallyBotModel):
	"""Tracking row for one user-created forum post (see module docstring)."""
	thread_id = NumberAttribute()  # == sort; the forum thread's snowflake
	event_sort = NumberAttribute(null=True)  # linked MeetupEvent sort key; None = no record
	state = UnicodeAttribute(null=True)  # event / missing_info / not_event / nonpublic
	missing_fields = UnicodeAttribute(null=True)  # comma-joined floor fields for the missing-info reply
	last_extraction = UnicodeAttribute(null=True)  # JSON of the last resolved extraction (enables no-AI recreation)
	last_analyzed_hash = UnicodeAttribute(null=True)  # sha256 of name + starter content + sorted tag ids
	last_edited_at = UTCDateTimeAttribute(null=True)  # discord's own edited_timestamp at last analysis
	last_analyzed_at = UTCDateTimeAttribute(null=True)
	reply_message_id = NumberAttribute(null=True)  # the bot's current status reply in the thread
	nonpublic_seen = BooleanAttribute(default=False)  # True once the skip reply fired for the current tag application
	created_at = UTCDateTimeAttribute(null=True)

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = "forum_event"


def _post_link(thread_id) -> str:
	"""Web URL of the forum post (also used as the ad hoc event's `link`)."""
	return f"https://discord.com/channels/{shared.guild.id}/{int(thread_id)}"


async def startup_sweep() -> None:
	"""Startup catch-up + one-time backfill: make sure the `nonpublic` tag
	exists, process every thread in the forum channel (hash-guarded, so
	already-processed posts are cheap no-ops), and clean up tracked rows
	whose post was deleted while the bot was offline."""
	channel = await forum.get_forum_channel()
	if not channel:
		print("ERROR: forum channel not found; skipping ad hoc event sweep")
		return
	names = {t.name.lower() for t in channel.available_tags}
	if NONPUBLIC_TAG_NAME not in names:
		await forum._create_forum_tag(channel, NONPUBLIC_TAG_NAME)

	threads: dict[int, discord.Thread] = {}
	try:
		# guild-level REST call; filter to the forum channel
		for t in await shared.guild.active_threads():
			if getattr(t, "parent_id", None) == forum.FORUM_CHANNEL_ID:
				threads[int(t.id)] = t
	except Exception:
		print(f"WARN: guild.active_threads() failed:\n{traceback.format_exc()}")
	try:
		async for t in channel.archived_threads(limit=None):
			threads[int(t.id)] = t
	except Exception:
		print(f"WARN: archived_threads() failed:\n{traceback.format_exc()}")

	print(f"ad hoc event sweep: processing {len(threads)} forum post(s)")
	for tid, thread in threads.items():
		try:
			await process_thread(thread)
		except Exception:
			print(f"ERROR: sweep processing failed for thread {tid}:\n{traceback.format_exc()}")
		await asyncio.sleep(SWEEP_DELAY)

	# reconcile tracked rows against reality: a row whose thread no longer
	# fetches (404) means the post was deleted while offline
	try:
		rows = await asyncio.to_thread(lambda: list(ForumEvent.scan()))
	except Exception:
		print("WARN: ForumEvent scan failed; skipping deleted-post reconciliation")
		return
	for row in rows:
		# the table is single-design: scan deserializes EVERY row (events,
		# reports, ...) as ForumEvent — only real forum_event rows have a
		# thread_id, everything else is another partition's data
		if (row.id or "") != "forum_event" or row.thread_id is None:
			continue
		tid = int(row.thread_id)
		if tid in threads:
			continue
		try:
			await shared.guild.fetch_channel(tid)
			# fetch succeeded: the thread is alive (e.g. archived but visible)
		except discord.NotFound:
			try:
				await cleanup_deleted_post(tid)
			except Exception:
				print(f"ERROR: sweep cleanup failed for thread {tid}:\n{traceback.format_exc()}")
		except discord.Forbidden:
			# fail closed: the thread exists but is inaccessible — keep the row
			print(f"WARN: thread {tid} not accessible; keeping its tracked record")
		except Exception:
			print(f"WARN: could not verify thread {tid}; keeping its tracked record")


def _starter_content_hash(thread, starter) -> str:
	"""Change-detection fingerprint: post title + starter body. Tag changes
	are deliberately excluded — the nonpublic gate handles tag toggles on
	every trigger before change detection, and keeping tags out of the hash
	lets the tag-removal fast path detect "content unchanged" (a tag-only
	hash change would force an AI reanalysis of possibly-past events)."""
	raw = f"{thread.name}\x00{starter.content}"
	return hashlib.sha256(raw.encode()).hexdigest()


async def _fetch_starter(thread) -> discord.Message:
	"""REST-fetch the post's starter message (id == thread id). REST returns
	full content even without the message_content intent, unlike the cache."""
	return await thread.fetch_message(int(thread.id))


def _has_nonpublic_tag(thread) -> bool:
	names = {t.name.lower() for t in (thread.applied_tags or [])}
	return NONPUBLIC_TAG_NAME in names


def _norm_dt(value):
	"""Normalize a datetime for equality comparison (naive -> UTC)."""
	if value is None:
		return None
	if isinstance(value, dt.datetime) and value.tzinfo is None:
		return value.replace(tzinfo=dt.timezone.utc)
	return value


MISSING_FIELD_LABELS = {"start": "start date/time", "location": "location"}


async def post_status_reply(thread: discord.Thread, row: ForumEvent, content: str):
	"""Delete the row's previous status reply (if any), post the new one, and
	record the new message id on the row (caller persists the row). Silent
	no-op send in quiet mode; the ddb bookkeeping still happens."""
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


async def _clear_status_reply(thread: discord.Thread, row: ForumEvent):
	"""Remove the current status reply without posting a replacement (used
	when the post stops being an event and the old notice would mislead)."""
	if row.reply_message_id:
		try:
			old = await thread.fetch_message(int(row.reply_message_id))
			await old.delete()
		except (discord.NotFound, discord.Forbidden):
			pass
		except Exception:
			print(f"ERROR: deleting status reply {row.reply_message_id} in {thread.id}:\n{traceback.format_exc()}")
	row.reply_message_id = None


def _resolve_extraction(extraction: dict, thread_name: str) -> tuple[dict | None, list[str]]:
	"""Validate a raw extraction against the floor (start + location, per the
	spec) and normalize it into a resolved field dict. Returns
	(resolved | None, missing_field_keys)."""
	missing = []
	start = None
	start_raw = extraction.get("start")
	if isinstance(start_raw, str) and start_raw.strip():
		try:
			start = dt.datetime.fromisoformat(start_raw.strip())
		except ValueError:
			start = None
	if start is None:
		missing.append("start")
	location = str(extraction.get("location") or "").strip()
	if not location:
		missing.append("location")
	if missing:
		return None, missing
	if start.tzinfo is None:
		# the prompt asks for ISO with offset; a bare time means Chicago
		start = start.replace(tzinfo=shared.central_time)
	end = None
	if extraction.get("end"):
		try:
			end = dt.datetime.fromisoformat(str(extraction["end"]))
		except ValueError:
			end = None
	if end is None and extraction.get("duration_minutes"):
		try:
			end = start + dt.timedelta(minutes=int(extraction["duration_minutes"]))
		except (TypeError, ValueError):
			end = None
	if end is None or end <= start:
		end = start + dt.timedelta(hours=1)  # spec: assume 1 hour when unknown
	category = extraction.get("category")
	if category not in llm.categories:
		category = "other"
	title = str(extraction.get("title") or thread_name or "").strip() or "Untitled Event"
	description = str(extraction.get("description") or "").strip()
	online = bool(extraction.get("online")) or location.lower() in ("online", "discord", "virtual", "zoom")
	resolved = {
		"title": title,
		"start": start.isoformat(),
		"end": end.isoformat(),
		"location": location,
		"online": online,
		"description": description,
		"category": category,
	}
	return resolved, []


def _event_from_resolved(resolved: dict, thread_id: int, created_at=None) -> events.MeetupEvent:
	"""Build the ad hoc MeetupEvent row from a resolved extraction dict."""
	start = dt.datetime.fromisoformat(resolved["start"])
	end = dt.datetime.fromisoformat(resolved["end"])
	location = resolved["location"]
	if len(location) > 99:
		location = location[0:96] + "..."
	description = resolved["description"]
	full_description = description
	if len(description) > 999:
		# match the meetup path's 999-char scheduled-event cap
		append = f"... [full event]({_post_link(thread_id)})"
		description = description[0:(999 - len(append))] + append
	ev = events.MeetupEvent(sort=int(thread_id))
	ev.source = FORUM_SOURCE
	ev.title = resolved["title"]
	ev.description = description
	ev.full_description = full_description
	ev.link = _post_link(thread_id)  # the website link goes to the forum post, not meetup
	ev.start_time = start  # setter keeps datetime + timestamp in sync
	ev.endtime = end
	ev.location = location
	ev.online = bool(resolved["online"])
	ev.category = resolved["category"]
	ev.snowflake_id = 0  # no discord scheduled event for ad hoc events
	ev.forum_thread_id = None  # the post already exists and is user-owned
	ev.created_at = created_at or dt.datetime.now(shared.central_time)
	return ev


async def _load_event(sort: int) -> events.MeetupEvent | None:
	try:
		return await asyncio.to_thread(events.MeetupEvent.get, "event", int(sort))
	except events.MeetupEvent.DoesNotExist:
		return None
	except Exception:
		print(f"ERROR: loading event {sort}:\n{traceback.format_exc()}")
		return None


async def _delete_event_record(row: ForumEvent) -> None:
	"""Delete the ad hoc event record linked to a ForumEvent row (if any)."""
	if not row.event_sort:
		return
	ev = await _load_event(int(row.event_sort))
	row.event_sort = None
	if ev:
		try:
			await ev.delete()  # async: discord/forum side-arms no-op for ad hoc rows
		except Exception:
			print(f"ERROR: deleting ad hoc event {row.thread_id}:\n{traceback.format_exc()}")


async def cleanup_deleted_post(thread_id: int) -> None:
	"""Remove the ForumEvent row + linked event record for a deleted forum
	post. Gated on the row's existence, so it is safe to call for any thread
	id (also used by the startup sweep for posts deleted while offline)."""
	try:
		row = await asyncio.to_thread(ForumEvent.get, "forum_event", int(thread_id))
	except ForumEvent.DoesNotExist:
		return
	except Exception:
		return  # transient ddb hiccup — a later sweep retries
	try:
		await _delete_event_record(row)
		await asyncio.to_thread(row.delete)
	except Exception:
		print(f"ERROR: cleaning up ForumEvent row for deleted post {thread_id}:\n{traceback.format_exc()}")


def _mention(thread) -> str:
	owner = getattr(thread, "owner_id", None)
	return f"<@{owner}>" if owner else ""


async def process_thread(thread: discord.Thread, *, force: bool = False) -> None:
	"""Full real-time pipeline for one #event-chat post. Idempotent: posts
	whose title/body/tags (hash) and edited_at are unchanged do nothing."""
	if getattr(thread, "parent_id", None) != forum.FORUM_CHANNEL_ID:
		return
	if shared.client and thread.owner_id and thread.owner_id == shared.client.user.id:
		return  # the bot's own mirrored Meetup posts are already tracked
	try:
		row = await asyncio.to_thread(ForumEvent.get, "forum_event", int(thread.id))
	except ForumEvent.DoesNotExist:
		row = ForumEvent(sort=int(thread.id), thread_id=int(thread.id))
		row.created_at = dt.datetime.now(shared.central_time)
	except Exception:
		print(f"ERROR: loading ForumEvent row for {thread.id}:\n{traceback.format_exc()}")
		return

	# ---- nonpublic gate (runs on every trigger, before anything else) ----
	was_nonpublic = False
	if _has_nonpublic_tag(thread):
		if row.nonpublic_seen:
			return  # already alerted for this application of the tag
		row.nonpublic_seen = True
		row.state = "nonpublic"
		await _delete_event_record(row)
		# keep last_analyzed_hash: the tag-removal fast path compares the
		# hash against it to rebuild without an AI call
		try:
			await asyncio.to_thread(row.save)
		except Exception:
			print(f"ERROR: saving ForumEvent row {thread.id}:\n{traceback.format_exc()}")
			return
		await post_status_reply(thread, row, f"{_mention(thread)} this post has the `nonpublic` tag, so it won't be shown on cah.moe.")
		await asyncio.to_thread(row.save)
		return
	elif row.nonpublic_seen:
		# the tag was just removed: fall through and recreate the record
		was_nonpublic = True
		row.nonpublic_seen = False

	# ---- change detection ----
	try:
		starter = await _fetch_starter(thread)
	except discord.NotFound:
		return  # post deleted while processing; the delete handler cleans up
	except Exception:
		print(f"ERROR: fetching starter message for {thread.id}:\n{traceback.format_exc()}")
		return
	content_hash = _starter_content_hash(thread, starter)
	edited_at = _norm_dt(getattr(starter, "edited_at", None))
	# was_nonpublic forces processing: the tag removal itself must run the
	# recreation path even though the content is unchanged (the fast path
	# below keeps that rebuild AI-free)
	changed = force or was_nonpublic or content_hash != row.last_analyzed_hash or edited_at != _norm_dt(row.last_edited_at)
	if not changed:
		await asyncio.to_thread(row.save)  # persist any nonpublic flag reset
		return

	# ---- past-event guard: never reanalyze/recompute a started event ----
	if row.event_sort:
		existing = await _load_event(int(row.event_sort))
		if existing and existing.start_time and existing.start_time <= dt.datetime.now(shared.central_time):
			row.last_analyzed_hash = content_hash
			row.last_edited_at = edited_at
			row.last_analyzed_at = dt.datetime.now(shared.central_time)
			await asyncio.to_thread(row.save)
			return

	# ---- recompute path (new post, edit, or tag-removal recreation) ----
	extraction = None
	if was_nonpublic and row.last_extraction and content_hash == row.last_analyzed_hash:
		# tag-removal fast path: the post is unchanged since the nonpublic
		# deletion — rebuild from the stored extraction with no AI call
		# (required for past events, where reanalysis is forbidden)
		try:
			extraction = json.loads(row.last_extraction)
		except Exception:
			print(f"WARN: stored extraction for {thread.id} unparseable, reanalyzing")
			extraction = None
	if extraction is None:
		extraction = await asyncio.to_thread(llm.ai_extract_event, thread.name, starter.content)
		if extraction is None:
			# transient AI failure: post nothing, keep the old hash so a
			# later trigger or startup sweep retries
			print(f"ad hoc event extraction for {thread.id} failed transiently; will retry")
			return

	row.last_analyzed_hash = content_hash
	row.last_edited_at = edited_at
	row.last_analyzed_at = dt.datetime.now(shared.central_time)

	is_event = extraction.get("is_event")
	if is_event is None:
		# key omitted: infer from the floor being met
		is_event = bool(extraction.get("start")) and bool(extraction.get("location"))
	if not is_event:
		# not an event post: drop any stale record + the now-misleading reply
		await _delete_event_record(row)
		await _clear_status_reply(thread, row)
		row.state = "not_event"
		row.last_extraction = None
		row.missing_fields = None
		await asyncio.to_thread(row.save)
		return

	resolved, missing = _resolve_extraction(extraction, thread.name)
	if resolved is None:
		# below the floor: drop a now-stale record and tell the author
		await _delete_event_record(row)
		row.state = "missing_info"
		row.missing_fields = ",".join(missing)
		row.last_extraction = None
		labels = ", ".join(MISSING_FIELD_LABELS.get(m, m) for m in missing)
		await post_status_reply(thread, row, f"{_mention(thread)} I tried to add this event to cah.moe but couldn't because of missing information: **{labels}**. Please update the post with the missing info and I'll try again!")
		await asyncio.to_thread(row.save)
		return

	# valid: upsert the event record, preserving created_at on recompute
	existing = await _load_event(int(thread.id))
	created_at = existing.created_at if existing else dt.datetime.now(shared.central_time)
	ev = _event_from_resolved(resolved, thread.id, created_at=created_at)
	try:
		await asyncio.to_thread(ev.save)
	except Exception:
		print(f"ERROR: saving ad hoc event {thread.id}:\n{traceback.format_exc()}")
		return
	row.event_sort = int(thread.id)
	row.state = "event"
	row.missing_fields = None
	row.last_extraction = json.dumps(resolved)
	await post_status_reply(thread, row, f"{_mention(thread)} saw your post and added your event to cah.moe so website visitors can see it! 📅 If you'd rather it NOT appear on the website, add the `nonpublic` tag to this post and I'll remove it.")
	await asyncio.to_thread(row.save)
