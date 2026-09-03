"""Offline verification for the forum pilot (no live token, no ddb, no discord).

Run inside the project venv: .venv/bin/python tests_forum_pilot.py
Follows the discord-bot-development skill's verification procedure: import
smoke test with Client.run stubbed, then assert on captured fake-sends.
Prints a final all-passed marker only when every assertion holds.
"""
import asyncio
import datetime as dt
import os
import sys
import types

# ---- fake discord layer (must exist before importing bot modules) ----
captured = {"threads": [], "sends": [], "starter_edits": [], "thread_edits": []}

import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed run)")


class FakeTag:
	def __init__(self, name):
		self.name = name


class FakeStarter:
	def __init__(self, content="stale body"):
		self.content = content

	async def edit(self, **kw):
		captured["starter_edits"].append(kw)
		if "content" in kw:
			self.content = kw["content"]


class FakeThread(discord.Thread):
	# shadow the base class' read-only properties with plain attributes
	starter_message = None
	applied_tags = None
	archived = False

	def __init__(self, id, name="Event: old title"):
		self.id = id
		self.name = name
		self.starter_message = FakeStarter()
		self.applied_tags = []
		self.deleted = False

	async def edit(self, **kw):
		captured["thread_edits"].append((self.id, dict(kw)))
		if "name" in kw:
			self.name = kw["name"]
		if "applied_tags" in kw:
			self.applied_tags = kw["applied_tags"]

	async def fetch_message(self, id):
		return self.starter_message


class FakeForumChannel(discord.ForumChannel):
	# shadow the base class' read-only property with a plain attribute
	available_tags = ()

	def __init__(self, id, tags):
		self.id = id
		self.available_tags = tags

	async def create_thread(self, name, content, applied_tags):
		thread = FakeThread(id=990000000000000001, name=name)
		thread.starter_message.content = content
		thread.applied_tags = list(applied_tags)
		captured["threads"].append({"name": name, "content": content, "tags": [t.name for t in applied_tags]})
		return thread, thread.starter_message


class FakeChannel(discord.TextChannel):
	def __init__(self, id):
		self.id = id

	async def send(self, content):
		captured["sends"].append(content)
		return None


# ---- import the bot modules with fakes in place ----
import events
import forum
import shared as shared_mod
from shared import IN_PERSON_MENTION, ONLINE_MENTION

# never touch real AWS from the test: stub the pynamo save
events.MeetupEvent.save = lambda self, *a, **kw: None


class FakeGuild:
	id = 1219601473948614737

	def __init__(self, forum_channel, announce_channel):
		self._forum = forum_channel
		self._announce = announce_channel
		self.threads = {}  # id -> FakeThread

	def get_channel_or_thread(self, id):
		if id == forum.FORUM_CHANNEL_ID:
			return self._forum
		if id == forum.ANNOUNCEMENTS_CHANNEL_ID:
			return self._announce
		return self.threads.get(id)

	async def fetch_channel(self, id):
		ch = self.get_channel_or_thread(id)
		if ch is None:
			class _FakeResponse:
				status = 404
			raise discord.errors.NotFound(_FakeResponse(), "nope")
		return ch


shared_mod.shared.guild = FakeGuild(
	FakeForumChannel(forum.FORUM_CHANNEL_ID, [FakeTag(n) for n in
		["book club", "conventions", "food", "gaming", "karaoke", "outdoor",
		 "watch party", "volunteering", "other", "in-person", "online"]]),
	FakeChannel(forum.ANNOUNCEMENTS_CHANNEL_ID),
)


def make_event(sort=111, title="Anime Night", category="watch party", online=False,
		forum_thread_id=None, created_at=None, start_offset_days=3):
	ev = events.MeetupEvent(sort=sort)
	ev.title = title
	ev.description = "A watch party for fans."
	ev.link = f"https://www.meetup.com/chicago-anime-hangouts/events/{sort}/"
	ev.start_time = dt.datetime.now(shared_mod.shared.est) + dt.timedelta(days=start_offset_days)
	ev.category = category
	ev.online = online
	ev.forum_thread_id = forum_thread_id
	ev.created_at = created_at
	ev.snowflake_id = 1234567890123456789
	return ev


def check(name, cond):
	if not cond:
		print(f"FAIL: {name}")
		sys.exit(1)
	print(f"PASS: {name}")


os.environ["FORUM_PILOT"] = "1"

async def main():
	# 1. create: title, body links, tags
	ev = make_event()
	tid = await forum.create_forum_post(ev)
	check("create returns thread id", tid == 990000000000000001)
	check("event row stores thread id", ev.forum_thread_id == 990000000000000001)
	post = captured["threads"][0]
	check("forum title prefixed", post["name"] == "Event: Anime Night")
	check("discord event link in body", f"https://discord.com/events/{shared_mod.shared.guild.id}/1234567890123456789" in post["content"])
	check("meetup link in body", ev.link in post["content"])
	check("description in body", "A watch party for fans." in post["content"])
	check("category tag applied", "watch party" in post["tags"])
	check("in-person tag applied", "in-person" in post["tags"])

	# 2. online event gets the online tag
	ev_online = make_event(sort=222, category="gaming", online=True)
	await forum.create_forum_post(ev_online)
	check("online tag applied", "online" in captured["threads"][1]["tags"])
	check("gaming tag applied", "gaming" in captured["threads"][1]["tags"])

	# 3. unknown category falls back to 'other' tag
	ev_other = make_event(sort=333, category="nonexistent-cat")
	await forum.create_forum_post(ev_other)
	check("unknown category -> other tag", "other" in captured["threads"][2]["tags"])

	# 4. update: rewrites starter body + title + tags
	existing_thread = FakeThread(id=880000000000000002)
	shared_mod.shared.guild.threads[880000000000000002] = existing_thread
	ev2 = make_event(sort=111, forum_thread_id=880000000000000002)
	before = len(captured["thread_edits"])
	await forum.update_forum_post(ev2)
	edits = captured["thread_edits"][before:]
	edited_ids = {e[0] for e in edits}
	check("update edits the thread", 880000000000000002 in edited_ids)
	name_edits = [e[1].get("name") for e in edits if "name" in e[1]]
	check("update fixes title", "Event: Anime Night" in name_edits)
	tag_edits = [e[1].get("applied_tags") for e in edits if "applied_tags" in e[1]]
	check("update re-applies tags", any(t.name == "watch party" for tl in tag_edits for t in tl))
	check("update rewrote body", any("A watch party for fans." in e.get("content", "") for e in captured["starter_edits"]))

	# 5. create on an already-posted event updates instead of duplicating
	n_posts = len(captured["threads"])
	await forum.create_forum_post(ev2)
	check("no duplicate post", len(captured["threads"]) == n_posts)

	# 6. weekly digest shape: intro, one message per kind, sections, links
	now = dt.datetime.now(shared_mod.shared.est)
	last_run = forum._last_weekly_run(now)
	check("last weekly run is a sunday 3pm", last_run.weekday() == 6 and last_run.hour == 15 and last_run < now)
	week_start = forum._next_weekday(now, 0)
	week_end = week_start + dt.timedelta(days=7)
	old = now - dt.timedelta(days=14)
	fresh = now - dt.timedelta(days=2)
	def at(day_offset, hour=18):
		return week_start + dt.timedelta(days=day_offset, hours=hour)
	# in-person: one happening (old, inside the week window), one newly planned;
	# online: one newly planned. The happening one carries a forum thread id so
	# the digest link path is exercised too.
	ev_in_old = make_event(sort=444, created_at=old, forum_thread_id=990000000000000001)
	ev_in_old.start_time = at(1)
	ev_in_new = make_event(sort=555, title="New In-Person", created_at=fresh)
	ev_in_new.start_time = at(3)
	ev_on_new = make_event(sort=666, title="New Online", online=True, created_at=fresh)
	ev_on_new.start_time = at(5)
	real_scan = events.MeetupEvent.scan
	events.MeetupEvent.scan = classmethod(lambda cls, **kw: iter([ev_in_old, ev_in_new, ev_on_new]))
	try:
		msgs = forum._build_digest_messages(now)
	finally:
		events.MeetupEvent.scan = real_scan
	check("digest sends 3 messages (intro + 2 kinds)", len(msgs) == 3)
	check("intro line format", msgs[0] == f"Today is {now.strftime('%B')} {now.day}, {now.year} and here are our CAH event announcements~!")
	check("in-person msg pings role", msgs[1].startswith(IN_PERSON_MENTION))
	check("online msg pings role", msgs[2].startswith(ONLINE_MENTION))
	check("happening section present", "## What's happening this week:" in msgs[1])
	check("newly planned section present", "## Newly planned events:" in msgs[1])
	check("old event listed as happening", "- " + ev_in_old.start_time.strftime("%b - %d") + ": [Anime Night]" in msgs[1])
	check("forum post link used", f"https://discord.com/channels/{shared_mod.shared.guild.id}/990000000000000001" in msgs[1])
	check("new event listed as newly planned", "[New In-Person]" in msgs[1].split("## Newly planned events:")[1])
	check("old event NOT in newly planned", "[Anime Night]" not in msgs[1].split("## Newly planned events:")[1])
	check("online kind separated", "[New Online]" in msgs[2] and "[Anime Night]" not in msgs[2])

	# 7. empty digest -> no messages at all
	events.MeetupEvent.scan = classmethod(lambda cls, **kw: iter([]))
	try:
		msgs_empty = forum._build_digest_messages(now)
	finally:
		events.MeetupEvent.scan = real_scan
	check("empty digest sends nothing", msgs_empty == [])

	# 8. online-only week: in-person message suppressed, intro kept
	events.MeetupEvent.scan = classmethod(lambda cls, **kw: iter([ev_on_new]))
	try:
		msgs_online_only = forum._build_digest_messages(now)
	finally:
		events.MeetupEvent.scan = real_scan
	check("online-only week sends 2 messages", len(msgs_online_only) == 2)
	check("no in-person message", not any(IN_PERSON_MENTION in m for m in msgs_online_only))

	# 9. pilot kill-switch: weekly job is a no-op when FORUM_PILOT is unset
	del os.environ["FORUM_PILOT"]
	n_sends = len(captured["sends"])
	await forum.send_weekly_announcements()
	check("kill-switch blocks weekly send", len(captured["sends"]) == n_sends)
	os.environ["FORUM_PILOT"] = "1"

	# 10. live send path posts every message in order (scan stays stubbed —
	# this box has no dynamodb access and must never touch real AWS)
	events.MeetupEvent.scan = classmethod(lambda cls, **kw: iter([ev_in_old, ev_in_new, ev_on_new]))
	try:
		await forum.send_weekly_announcements()
	finally:
		events.MeetupEvent.scan = real_scan
	check("weekly send posts all messages", captured["sends"] == msgs)

	print("ALL PASSED")

asyncio.run(main())
