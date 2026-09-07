"""Forum pilot functionality (experimental; gated behind the FORUM_PILOT env var).

Everything this module does - forum posts for events + the weekly announcements
digest - only runs when FORUM_PILOT is set to any non-empty value (checked via
forum_pilot_enabled()). Unset it and restart to roll the pilot back to the old
at-mention behavior with zero code changes. Forum posts of events that get
cancelled are cleaned up even with the pilot off, so no orphans are left behind.

Events are posted to the #event-chat forum channel:
- title: "Event: <event title>"
- body: the event content + links to the discord event and the meetup event
- tags: the event's category tag + "in-person" or "online" (matched by name
  against the forum's configured tags; missing tags are created automatically
  when the bot has permission, otherwise logged and skipped)

Once a week (Sundays 3pm CT) a digest is posted to the announcements channel:
- msg 1: intro line (only sent when msg 2 or msg 3 is sent)
- msg 2: in-person events, ping @in-person-events (skipped when empty)
- msg 3: online events, ping @online-events (skipped when empty)
Each of msg 2/3 has a "What's happening this week" section (events starting in
the upcoming week, monday through sunday) and a "Newly planned events" section
(events first scheduled since the last weekly message; each event appears here
at most once, enforced via the event's created_at timestamp).
"""
import asyncio
import datetime as dt
import os
import traceback

import discord

import events
from shared import shared, IN_PERSON_MENTION, ONLINE_MENTION

FORUM_CHANNEL_ID = 1543307748006174920  # #event-chat forum channel
ANNOUNCEMENTS_CHANNEL_ID = 1244283919218770030  # #announcements
FORUM_POST_MAX_LEN = 2000  # discord's hard limit for forum post content

_forum_channel_cache: discord.ForumChannel | None = None

def forum_pilot_enabled() -> bool:
	return not not os.getenv('FORUM_PILOT')

async def get_forum_channel(refresh: bool = False) -> discord.ForumChannel | None:
	global _forum_channel_cache
	if _forum_channel_cache and not refresh:
		return _forum_channel_cache
	try:
		channel = await shared.guild.fetch_channel(FORUM_CHANNEL_ID)
	except discord.NotFound:
		print(f"ERROR: forum channel {FORUM_CHANNEL_ID} not found")
		return None
	if not isinstance(channel, discord.ForumChannel):
		print(f"ERROR: channel {FORUM_CHANNEL_ID} is not a forum channel (got {type(channel).__name__})")
		return None
	_forum_channel_cache = channel
	return channel

async def _create_forum_tag(forum_channel: discord.ForumChannel, name: str) -> discord.ForumTag | None:
	"""Create a missing tag on the forum channel (best effort)."""
	try:
		tag = await forum_channel.create_tag(name=name)
		print(f"Created forum tag '{name}' on channel {forum_channel.id}")
		return tag
	except discord.Forbidden:
		print(f"WARNING: no permission to create forum tag '{name}' (needs manage-channels); skipping that tag")
	except Exception:
		print(f"ERROR: failed creating forum tag '{name}':\n{traceback.format_exc()}")
	return None

async def resolve_tags(forum_channel: discord.ForumChannel, event: events.MeetupEvent) -> list[discord.ForumTag]:
	"""Resolve the event's category + in-person/online tags, creating any that
	are missing on the forum channel as needed. Tags that can neither be found
	nor created are logged and skipped (the post still gets the rest)."""
	category = event.category or "other"
	if category not in events.categories:
		category = "other"
	wanted = [category, ("online" if event.online else "in-person")]
	tags = []
	for name in wanted:
		by_name = {t.name.lower(): t for t in forum_channel.available_tags}
		tag = by_name.get(name.lower())
		if tag is None:
			# refresh from the api first: the tag may exist but be missing from
			# the startup cache (e.g. added via the discord ui after boot)
			fresh = await get_forum_channel(refresh=True)
			if fresh:
				forum_channel = fresh
				tag = {t.name.lower(): t for t in fresh.available_tags}.get(name.lower())
		if tag is None:
			tag = await _create_forum_tag(forum_channel, name)
		if tag is None:
			print(f"WARNING: could not resolve or create forum tag '{name}', skipping that tag")
		else:
			tags.append(tag)
	return tags

def _discord_event_link(event: events.MeetupEvent) -> str | None:
	if not event.snowflake_id:
		return None
	return f"https://discord.com/events/{shared.guild.id}/{int(event.snowflake_id)}"

def _event_forum_body(event: events.MeetupEvent) -> str:
	"""Forum post body: the event content plus discord + meetup links."""
	if not event.online and event.location:
		where = event.location
	else:
		where = "Online"
	# prefer the untruncated meetup description; `description` is capped at 999
	# chars for discord scheduled events
	body_desc = (event.full_description or event.description or "").strip()
	lines = [
		f"**When:** {where} - <t:{round(event.start_time.timestamp())}:F>",
		"",
		body_desc,
		"",
	]
	discord_link = _discord_event_link(event)
	if discord_link:
		lines.append(f"**Discord event:** {discord_link}")
	if event.link:
		lines.append(f"**Meetup event:** {event.link}")
	body = "\n".join(lines)
	if len(body) > FORUM_POST_MAX_LEN:
		# discord physically rejects longer forum post content; trim as close
		# to the full text as the hard limit allows
		append = f" ... [full event]({event.link})" if event.link else " ..."
		body = body[0:(FORUM_POST_MAX_LEN - len(append))] + append
		print(f"WARNING: forum body for {event.sort} | {event.title} exceeded {FORUM_POST_MAX_LEN} chars, truncated")
	return body

async def create_forum_post(event: events.MeetupEvent) -> int | None:
	"""Create the event's forum post and record its thread id on the event row.
	Returns the thread id (or None when the post couldn't be created)."""
	if event.forum_thread_id:
		# already posted (e.g. discord event recreated after out-of-band deletion);
		# update the existing post instead of creating a duplicate
		await update_forum_post(event)
		return int(event.forum_thread_id)
	if shared.quiet:
		print(f"(quiet mode) would create forum post for {event.sort} | {event.title}")
		return None
	forum_channel = await get_forum_channel()
	if not forum_channel:
		print(f"ERROR: forum channel {FORUM_CHANNEL_ID} not found; cannot post {event.title}")
		return None
	try:
		tags = await resolve_tags(forum_channel, event)
		thread, starter = await forum_channel.create_thread(
			name=f"Event: {event.title}",
			content=_event_forum_body(event),
			applied_tags=tags,
		)
	except Exception as e:
		print(f"ERROR: failed creating forum post for {event.sort} | {event.title}:\n{traceback.format_exc()}")
		return None
	event.forum_thread_id = thread.id
	event.save()
	print(f"Created forum post {thread.id} for {event.sort} | {event.title}")
	return thread.id

async def update_forum_post(event: events.MeetupEvent) -> None:
	"""Update the event's forum post title, body and tags to match."""
	if shared.quiet:
		print(f"(quiet mode) would update forum post for {event.sort} | {event.title}")
		return
	if not event.forum_thread_id:
		# no post recorded (created before the pilot, or creation failed) - backfill it
		print(f"no forum post recorded for {event.sort} | {event.title}, creating one...")
		await create_forum_post(event)
		return
	forum_channel = await get_forum_channel()
	if not forum_channel:
		print(f"ERROR: forum channel {FORUM_CHANNEL_ID} not found; cannot update {event.title}")
		return
	try:
		thread = shared.guild.get_channel_or_thread(int(event.forum_thread_id)) \
			or await shared.guild.fetch_channel(int(event.forum_thread_id))
	except discord.NotFound:
		thread = None
	if not isinstance(thread, discord.Thread):
		print(f"WARNING: forum thread {event.forum_thread_id} for {event.sort} | {event.title} is gone; recreating post")
		await create_forum_post(event)
		return
	try:
		if thread.archived:
			await thread.edit(archived=False)
		body = _event_forum_body(event)
		starter = thread.starter_message
		if not starter:
			# the starter message id == the thread id for forum posts
			starter = await thread.fetch_message(thread.id)
		if starter.content != body:
			await starter.edit(content=body)
		expected_title = f"Event: {event.title}"
		if thread.name != expected_title:
			await thread.edit(name=expected_title)
		current_tag_names = {t.name for t in (thread.applied_tags or [])}
		wanted_tags = await resolve_tags(forum_channel, event)
		if current_tag_names != {t.name for t in wanted_tags}:
			await thread.edit(applied_tags=wanted_tags)
		print(f"Updated forum post {thread.id} for {event.sort} | {event.title}")
	except Exception as e:
		print(f"ERROR: failed updating forum post for {event.sort} | {event.title}:\n{traceback.format_exc()}")

async def delete_forum_post(event: events.MeetupEvent) -> None:
	"""Remove the event's forum post (the ddb row is handled by the caller)."""
	if not event.forum_thread_id:
		return
	if shared.quiet:
		print(f"(quiet mode) would delete forum post {event.forum_thread_id} for {event.sort} | {event.title}")
		return
	try:
		thread = await shared.guild.fetch_channel(int(event.forum_thread_id))
		if isinstance(thread, discord.Thread):
			await thread.delete()
			print(f"Deleted forum post {event.forum_thread_id} for {event.sort} | {event.title}")
	except discord.NotFound:
		print(f"forum post {event.forum_thread_id} for {event.sort} | {event.title} already gone")
	except Exception as e:
		print(f"ERROR: failed deleting forum post {event.forum_thread_id}:\n{traceback.format_exc()}")

async def sync_forum_posts() -> None:
	"""Startup sync: make sure every tracked upcoming event has a forum post.
	Creates missing ones and refreshes stale ones (e.g. posts written before
	full_description existed). In-sync posts are left untouched, so this is
	cheap to run on every startup."""
	now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
	# the pynamo scan is blocking network I/O; materialize it off the event loop
	upcoming = await asyncio.to_thread(lambda: list(
		events.MeetupEvent.scan(index_name="timestamp-index",
			filter_condition=events.MeetupEvent.timestamp > now_ms)))
	for event in upcoming:
		if not event.forum_thread_id:
			print(f"backfilling forum post for {event.sort} | {event.title}")
			await create_forum_post(event)
		else:
			await update_forum_post(event)

# ---------------- weekly announcements ----------------

def _next_weekday(now: dt.datetime, weekday: int) -> dt.datetime:
	"""Midnight CT on the next given weekday (0=monday); today counts."""
	days_ahead = (weekday - now.weekday()) % 7
	day = now.date() + dt.timedelta(days=days_ahead)
	return dt.datetime.combine(day, dt.time.min, tzinfo=shared.est)

def _last_weekly_run(now: dt.datetime) -> dt.datetime:
	"""The most recent sunday-3pm CT moment strictly before now, used as the
	cutoff for 'newly planned' events."""
	days_since_sunday = (now.weekday() - 6) % 7
	candidate = (now - dt.timedelta(days=days_since_sunday)).replace(hour=15, minute=0, second=0, microsecond=0)
	if candidate >= now:
		candidate -= dt.timedelta(days=7)
	return candidate

def _post_link(event: events.MeetupEvent) -> str:
	"""Web URL of the event's forum post, falling back to the meetup link."""
	if event.forum_thread_id and shared.guild:
		return f"https://discord.com/channels/{shared.guild.id}/{event.forum_thread_id}"
	return event.link or ""

def _fmt_day(start_time: dt.datetime) -> str:
	return start_time.strftime("%b %d")

def _planned_events_message(mention: str, happening: list[events.MeetupEvent], newly: list[events.MeetupEvent]) -> str | None:
	sections = []
	if happening:
		sections.append("\n".join(["## What's happening this week:"] +
			[f"- {_fmt_day(e.start_time)}: [{e.title}]({_post_link(e)})" for e in happening]))
	if newly:
		sections.append("\n".join(["## Newly planned events:"] +
			[f"- {_fmt_day(e.start_time)}: [{e.title}]({_post_link(e)})" for e in newly]))
	if not sections:
		return None
	return "\n".join([mention] + sections)

def _build_digest_messages(now: dt.datetime | None = None) -> list[str]:
	"""Compose the sunday announcement. Returns only the messages that have
	content; an empty list means nothing goes out this week (and then the intro
	line isn't sent either)."""
	if now is None:
		now = dt.datetime.now(shared.est)
	week_start = _next_weekday(now, 0)  # upcoming monday, midnight
	week_end = week_start + dt.timedelta(days=7)
	last_run = _last_weekly_run(now)
	# future events only: they cover both the upcoming-week window and the
	# "newly planned" window (past events should never be announced)
	future = events.MeetupEvent.scan(index_name="timestamp-index",
		filter_condition=events.MeetupEvent.timestamp > int(now.timestamp() * 1000))
	happening = {"in-person": [], "online": []}
	newly = {"in-person": [], "online": []}
	for event in future:
		if not event.start_time:
			continue
		kind = "online" if event.online else "in-person"
		if week_start <= event.start_time < week_end:
			happening[kind].append(event)
		if event.created_at:
			created = event.created_at if event.created_at.tzinfo else event.created_at.replace(tzinfo=dt.timezone.utc)
			if created >= last_run:
				newly[kind].append(event)
	for lst in list(happening.values()) + list(newly.values()):
		lst.sort(key=lambda e: e.start_time)
	if not (happening["in-person"] or newly["in-person"] or happening["online"] or newly["online"]):
		print("weekly announcements: nothing to send this week")
		return []
	messages = [f"Today is {now.strftime('%B')} {now.day}, {now.year} and here are our CAH event announcements~!"]
	for kind, mention in (("in-person", IN_PERSON_MENTION), ("online", ONLINE_MENTION)):
		msg = _planned_events_message(mention, happening[kind], newly[kind])
		if msg:
			messages.append(msg)
	return messages

async def send_weekly_announcements() -> None:
	"""Scheduled job: post the weekly digest to the announcements channel."""
	if not forum_pilot_enabled():
		return
	# the pynamo scan inside is blocking network I/O; keep it off the event loop
	messages = await asyncio.to_thread(_build_digest_messages)
	if not messages:
		return
	channel = shared.guild.get_channel_or_thread(ANNOUNCEMENTS_CHANNEL_ID)
	if not channel:
		try:
			channel = await shared.guild.fetch_channel(ANNOUNCEMENTS_CHANNEL_ID)
		except Exception as e:
			print(f"ERROR: announcements channel {ANNOUNCEMENTS_CHANNEL_ID} not found:\n{traceback.format_exc()}")
			return
	for msg in messages:
		print(f"sending weekly announcement -> {msg.splitlines()[0]}")
		if shared.quiet:
			print("(quiet mode: skipping actual send)")
			continue
		try:
			await channel.send(msg)
		except Exception as e:
			print(f"ERROR: failed sending weekly announcement:\n{traceback.format_exc()}")

if __name__ == "__main__":
	# debug harness: prints what would be sent without touching discord
	# (still needs AWS creds to scan the event table)
	for m in _build_digest_messages():
		print("=== message ===")
		print(m)
		print()
