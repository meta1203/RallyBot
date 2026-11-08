"""Forum functionality for events.

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

Every digest send (including empty weeks) is recorded in a ddb ledger
(AnnounceLedger, partition id="announce_weekly", sort = the scheduled run
slot's epoch ms). Nothing is ever auto-sent outside the scheduled job — the
ledger exists so a missed run (bot offline / mid-send crash) is detectable
and recoverable by hand via /rally admin reannounce, which rebuilds the
digest with `now` = the original scheduled slot. A partial send (some
messages sent, some failed) is recorded per message id, so a reannounce
without force completes exactly the holes instead of duplicating what went
out already. A log-only startup check (check_missed_digest) surfaces gaps.
Events that start more than a month out are held back: no forum post is
created for them and they are not alerted as newly planned in the digest
until they start in less than 30 days (event_in_post_window). The daily
update_events check then creates the held-back posts once they enter the
window (ensure_forum_post). A held-back event is announced in the digest at
the first weekly run after its post is created (detected via the forum
thread's snowflake creation time, so no extra ddb field is needed).
"""
import asyncio
import datetime as dt
import json
import traceback
from typing import Literal

import discord
from discord import app_commands

import events
from aws import RallyBotModel
from shared import shared, IN_PERSON_MENTION, ONLINE_MENTION
from selfroles import admin_group
from pynamodb.exceptions import DoesNotExist

FORUM_CHANNEL_ID = 1543307748006174920  # #event-chat forum channel
ANNOUNCEMENTS_CHANNEL_ID = 1244283919218770030  # #announcements
FORUM_POST_MAX_LEN = 2000  # discord's hard limit for forum post content
FORUM_POST_WINDOW = dt.timedelta(days=30)  # events starting further out are held back

FORUM_THREAD_NAME_MAX_LEN = 100  # discord's hard limit for thread names

LEDGER_ID = "announce_weekly"  # ddb partition for the digest send ledger


class AnnounceLedger(RallyBotModel):
	"""One row per scheduled weekly digest run (id="announce_weekly",
	sort = the scheduled sunday-3pm-CT slot's epoch ms). `data` is JSON:
	{"sent_at": <iso>, "forced": <bool>, "message_ids": [<int|null>, ...]} —
	message_ids is index-aligned with the digest's message list and carries
	null for every message that was not successfully sent. Empty weeks are
	recorded too (message_ids = []), so "no row" reliably means "never ran".
	No GSI needed: the slot is computed from the schedule, plain GetItem."""

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = LEDGER_ID


def _digest_slot(now: dt.datetime) -> dt.datetime:
	"""The scheduled sunday-3pm CT slot the currently executing (or recovered)
	digest belongs to. The +5s nudge resolves a fire landing exactly on the
	slot to this week instead of last week."""
	return _last_weekly_run(now + dt.timedelta(seconds=5))


async def get_digest_ledger(slot_ms: int) -> AnnounceLedger | None:
	"""Ledger row for a digest slot; None when never recorded (a read error
	is logged and reported as None too — callers must treat that as
	"unknown, likely unsent", which is the safe direction for recovery)."""
	try:
		return await asyncio.to_thread(AnnounceLedger.get, LEDGER_ID, slot_ms)
	except DoesNotExist:
		return None
	except Exception:
		print(f"ERROR: failed reading announce ledger for slot {slot_ms}:\n{traceback.format_exc()}")
		return None


async def record_digest_send(slot_ms: int, message_ids: list, forced: bool = False) -> None:
	"""Record a digest send (or an empty week) after the discord sends."""
	row = AnnounceLedger(sort=slot_ms)
	row.data = json.dumps({
		"sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
		"forced": forced,
		"message_ids": message_ids,
	})
	await asyncio.to_thread(row.save)


async def _announcements_channel():
	"""The #announcements channel, cache-first with a fetch fallback."""
	channel = shared.guild.get_channel_or_thread(ANNOUNCEMENTS_CHANNEL_ID)
	if not channel:
		try:
			channel = await shared.guild.fetch_channel(ANNOUNCEMENTS_CHANNEL_ID)
		except Exception:
			print(f"ERROR: announcements channel {ANNOUNCEMENTS_CHANNEL_ID} not found:\n{traceback.format_exc()}")
			return None
	if not isinstance(channel, (discord.TextChannel, discord.Thread)):
		print(f"ERROR: channel {ANNOUNCEMENTS_CHANNEL_ID} is not a text channel (got {type(channel).__name__})")
		return None
	return channel

_forum_channel_cache: discord.ForumChannel | None = None

def forum_title(event: events.MeetupEvent) -> str:
	"""Thread title for the event's forum post, capped at discord's
	100-char thread-name limit (a too-long name makes create/edit fail)."""
	title = f"Event: {event.title}"
	if len(title) > FORUM_THREAD_NAME_MAX_LEN:
		title = title[0:FORUM_THREAD_NAME_MAX_LEN - 3] + "..."
	return title

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

def event_in_post_window(event: events.MeetupEvent) -> bool:
	"""True when the event starts soon enough (in less than a month) to be
	posted to the forum / alerted as newly planned in the weekly digest.
	Far-out events are held back; the daily update_events check posts them
	once they enter the window (see ensure_forum_post)."""
	start = event.start_time
	if not start:
		return False
	if not start.tzinfo:
		# defensive: rows should always carry tz-aware datetimes
		start = start.replace(tzinfo=dt.timezone.utc)
	return start < dt.datetime.now(shared.central_time) + FORUM_POST_WINDOW

async def ensure_forum_post(event: events.MeetupEvent) -> int | None:
	"""Daily-check catch-up: create the forum post for an already-seen event
	that is still unposted and has now entered the post window. Events with a
	post are left untouched (no redundant update API calls) and far-out
	events stay held back."""
	if event.forum_thread_id:
		return int(event.forum_thread_id)
	return await create_forum_post(event)

async def create_forum_post(event: events.MeetupEvent) -> int | None:
	"""Create the event's forum post and record its thread id on the event row.
	Returns the thread id (or None when the post couldn't be created)."""
	if (event.source or "meetup") == "forum":
		# ad hoc event: the forum post already exists and is user-owned —
		# the bot must never create/rewrite it (this guard covers every
		# call path: notify_new_event, ensure_forum_post, update_forum_post)
		return None
	if event.forum_thread_id:
		# already posted (e.g. discord event recreated after out-of-band deletion);
		# update the existing post instead of creating a duplicate
		await update_forum_post(event)
		return int(event.forum_thread_id)
	if not event_in_post_window(event):
		print(f"holding back forum post for {event.sort} | {event.title}: starts more than a month out ({event.start_time})")
		return None
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
			name=forum_title(event),
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
		# no post recorded (created before the pilot, creation failed, or the
		# event starts too far out and is being held back) - backfill only
		# once the event is inside the post window
		if not event_in_post_window(event):
			print(f"holding back forum post for {event.sort} | {event.title}: starts more than a month out")
			return
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
		# drop the stale thread id first: with it still set, create_forum_post
		# routes straight back into this 404 path (update -> create -> update
		# -> ...) instead of actually creating a fresh post
		event.forum_thread_id = None
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
		expected_title = forum_title(event)
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
	"""Startup sync: make sure every tracked upcoming event inside the post
	window has a forum post.
	Creates missing ones and refreshes stale ones (e.g. posts written before
	full_description existed). In-sync posts are left untouched, so this is
	cheap to run on every startup."""
	now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
	# the pynamo scan is blocking network I/O; materialize it off the event loop
	upcoming = await asyncio.to_thread(lambda: list(
		events.MeetupEvent.scan(index_name="timestamp-index",
			filter_condition=events.MeetupEvent.timestamp > now_ms)))
	for event in upcoming:
		if (event.source or "meetup") == "forum":
			# ad hoc event: its post already exists (user-owned); skip silently
			continue
		if not event.forum_thread_id:
			if not event_in_post_window(event):
				print(f"holding back forum post for {event.sort} | {event.title}: starts more than a month out")
				continue
			print(f"backfilling forum post for {event.sort} | {event.title}")
			await create_forum_post(event)
		else:
			await update_forum_post(event)

# ---------------- weekly announcements ----------------

def _next_weekday(now: dt.datetime, weekday: int) -> dt.datetime:
	"""Midnight CT on the next given weekday (0=monday); today counts."""
	days_ahead = (weekday - now.weekday()) % 7
	day = now.date() + dt.timedelta(days=days_ahead)
	return dt.datetime.combine(day, dt.time.min, tzinfo=shared.central_time)

def _last_weekly_run(now: dt.datetime) -> dt.datetime:
	"""The most recent sunday-3pm CT moment strictly before now, used as the
	cutoff for 'newly planned' events."""
	days_since_sunday = (now.weekday() - 6) % 7
	candidate = (now - dt.timedelta(days=days_since_sunday)).replace(hour=15, minute=0, second=0, microsecond=0)
	if candidate >= now:
		candidate -= dt.timedelta(days=7)
	return candidate

def _snowflake_time(snowflake) -> dt.datetime:
	"""Creation timestamp of a discord object decoded from its snowflake id
	(discord epoch 1420070400000, top 42 bits)."""
	return dt.datetime.fromtimestamp(((int(snowflake) >> 22) + 1420070400000) / 1000, tz=dt.timezone.utc)

def _post_link(event: events.MeetupEvent) -> str:
	"""Web URL of the event's forum post, falling back to the meetup link."""
	if event.forum_thread_id and shared.guild:
		return f"https://discord.com/channels/{shared.guild.id}/{event.forum_thread_id}"
	return event.link or ""

def _fmt_day(start_time: dt.datetime) -> str:
	# always render the wall-clock day in CT, even when the stored datetime
	# carries a different offset (e.g. the discord-event path stores UTC)
	return start_time.astimezone(shared.central_time).strftime("%b %d")

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
		now = dt.datetime.now(shared.central_time)
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
			# an event counts as newly planned from the moment the community can
			# see it: either it was scheduled since the last digest, or its forum
			# post was created since then — that second case is how a far-out
			# event that was held back until it entered the post window gets
			# announced, at the first digest after the daily check posts it.
			# The post's creation time is decoded from the thread snowflake, so
			# no extra ddb field is needed.
			try:
				thread_created = _snowflake_time(event.forum_thread_id) if event.forum_thread_id else None
			except Exception:
				thread_created = None
			# far-out events are never alerted (only events starting within the
			# post window); they surface via the daily check's catch-up
			if event_in_post_window(event) and (created >= last_run or (thread_created and thread_created >= last_run)):
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
	"""Scheduled job: post the weekly digest to the announcements channel and
	record the send in the ddb ledger. Never auto-catches up: a missed run
	(bot offline / job error) stays unsent until /rally admin reannounce."""
	now = dt.datetime.now(shared.central_time)
	slot = _digest_slot(now)
	# the pynamo scan inside is blocking network I/O; keep it off the event loop
	messages = await asyncio.to_thread(_build_digest_messages, now)
	if not messages:
		print("weekly announcements: nothing to send this week")
		if not shared.quiet:
			# an empty week is still a handled week: record it so the gap
			# check doesn't flag it and a reannounce doesn't retry it
			await record_digest_send(int(slot.timestamp() * 1000), [], forced=False)
		return
	channel = await _announcements_channel()
	if not channel:
		return
	sent_ids: list = []
	for msg in messages:
		print(f"sending weekly announcement -> {msg.splitlines()[0]}")
		if shared.quiet:
			print("(quiet mode: skipping actual send)")
			sent_ids.append(None)
			continue
		try:
			sent = await channel.send(msg)
			sent_ids.append(sent.id)
		except Exception:
			# the ledger keeps per-message ids, so a failure here is visible
			# and exactly completable by /rally admin reannounce (no force)
			print(f"ERROR: failed sending weekly announcement:\n{traceback.format_exc()}")
			sent_ids.append(None)
	if not shared.quiet:
		await record_digest_send(int(slot.timestamp() * 1000), sent_ids, forced=False)


async def check_missed_digest() -> None:
	"""Startup visibility only (never sends): warn when the most recent
	scheduled digest slot has no ledger record, i.e. the run never completed —
	typically because the bot was offline sunday 3pm CT. Recovery is manual:
	/rally admin reannounce."""
	now = dt.datetime.now(shared.central_time)
	slot = _digest_slot(now)
	row = await get_digest_ledger(int(slot.timestamp() * 1000))
	if row is None:
		print(f"WARNING: no send record for the weekly digest slot {slot:%Y-%m-%d %H:%M %Z} — "
			"if the bot was offline then, recover it with /rally admin reannounce")


def _reannounce_admin(interaction: discord.Interaction) -> bool:
	"""Admins and members with Manage Server may reannounce."""
	member = interaction.user
	if not isinstance(member, discord.Member):
		return False
	perms = member.guild_permissions
	return perms.administrator or perms.manage_guild


async def reannounce_digest(interaction: discord.Interaction, week: str, force: bool) -> None:
	"""Manual recovery for a missed/failed weekly digest: rebuild the digest
	for the chosen scheduled slot (with `now` = that slot, so the content and
	the 'newly planned' cutoff match what should have gone out at the time)
	and send only what the ledger shows never went out. force=True resends
	every message. Ephemeral reply summarizes the outcome."""
	await interaction.response.defer(ephemeral=True)
	now = dt.datetime.now(shared.central_time)
	slot = _digest_slot(now)
	if week == "previous":
		slot -= dt.timedelta(days=7)
	slot_ms = int(slot.timestamp() * 1000)
	slot_label = slot.strftime("%b %d %Y, %I:%M %p %Z")

	ledger = await get_digest_ledger(slot_ms)
	prior_ids: list = []
	prior_sent_at = None
	if ledger and ledger.data:
		try:
			parsed = json.loads(ledger.data)
			prior_ids = parsed.get("message_ids") or []
			prior_sent_at = parsed.get("sent_at")
		except Exception:
			print(f"WARNING: unreadable announce ledger row for slot {slot_ms}, treating as no record:\n{traceback.format_exc()}")
			prior_ids = []

	# rebuild for the original slot; blocking pynamo scan off the event loop
	messages = await asyncio.to_thread(_build_digest_messages, slot)
	if not messages:
		note = ""
		if any(mid is not None for mid in prior_ids):
			note = f" (the earlier send recorded {sum(1 for m in prior_ids if m is not None)} message(s) that are still in the channel)"
		if shared.quiet:
			await interaction.followup.send(f"(quiet mode) Nothing to announce for {slot_label}{note}.", ephemeral=True)
			return
		await record_digest_send(slot_ms, [], forced=force)
		await interaction.followup.send(f"Nothing to announce for {slot_label} — the digest had no content, recorded as handled{note}.", ephemeral=True)
		return

	if force:
		unsent = list(range(len(messages)))
	else:
		# index-aligned with the digest message list: skip what the ledger
		# shows as already sent, complete the holes (indexes past the
		# recorded list count as unsent)
		unsent = [i for i, mid in enumerate(prior_ids) if mid is None and i < len(messages)]
		unsent += list(range(len(prior_ids), len(messages)))
		if not unsent:
			await interaction.followup.send(
				f"The digest for {slot_label} was already sent (recorded {prior_sent_at}, "
				f"{sum(1 for m in prior_ids if m is not None)} message(s)). Use force=True to resend it anyway.",
				ephemeral=True)
			return

	if shared.quiet:
		await interaction.followup.send(f"(quiet mode) Would send {len(unsent)} message(s) for {slot_label}.", ephemeral=True)
		return
	channel = await _announcements_channel()
	if not channel:
		await interaction.followup.send(f"ERROR: announcements channel {ANNOUNCEMENTS_CHANNEL_ID} not found; nothing sent.", ephemeral=True)
		return
	sent_ids: list = list(prior_ids[:len(messages)])
	while len(sent_ids) < len(messages):
		sent_ids.append(None)
	sent_count = 0
	for i in unsent:
		msg = messages[i]
		print(f"reannounce: sending digest message {i + 1}/{len(messages)} for slot {slot_label}")
		try:
			sent = await channel.send(msg)
			sent_ids[i] = sent.id
			sent_count += 1
		except Exception:
			print(f"ERROR: reannounce failed sending digest message {i + 1}/{len(messages)}:\n{traceback.format_exc()}")
	await record_digest_send(slot_ms, sent_ids, forced=force)
	await interaction.followup.send(
		f"Sent {sent_count} of {len(messages)} digest message(s) for {slot_label}"
		+ (" (forced resend)" if force else " (completed the missing ones)")
		+ f" to <#{ANNOUNCEMENTS_CHANNEL_ID}>.", ephemeral=True)


@admin_group.command(name="reannounce", description="Recover or resend a weekly announcements digest")
@app_commands.guild_only
@app_commands.check(_reannounce_admin)
@app_commands.describe(
	week="current = the most recent scheduled digest run (default), previous = the one before it",
	force="Resend every message even if the ledger shows it already went out",
)
async def reannounce_command(interaction: discord.Interaction, week: Literal["current", "previous"] = "current", force: bool = False):
	await reannounce_digest(interaction, week, force)


@reannounce_command.error
async def reannounce_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	if isinstance(error, app_commands.CheckFailure):
		message = "You need Administrator or Manage Server to use reannounce."
		if interaction.response.is_done():
			await interaction.followup.send(message, ephemeral=True)
		else:
			await interaction.response.send_message(message, ephemeral=True)

if __name__ == "__main__":
	# debug harness: prints what would be sent without touching discord
	# (still needs AWS creds to scan the event table)
	for m in _build_digest_messages():
		print("=== message ===")
		print(m)
		print()
