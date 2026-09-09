"""Offline verification for the forum pilot (no live token, no ddb, no discord).

Run inside the project venv: .venv/bin/python tests/tests_forum_pilot.py
Follows the discord-bot-development skill's verification procedure: import
smoke test with Client.run stubbed, then assert on captured fake-sends.
Prints a final all-passed marker only when every assertion holds.
"""
import asyncio
import datetime as dt
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- fake discord layer (must exist before importing bot modules) ----
captured = {"threads": [], "sends": [], "starter_edits": [], "thread_edits": [], "created_tags": []}

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
		self.available_tags = list(tags)

	async def create_tag(self, name, *, moderator_ids=None):
		tag = FakeTag(name)
		self.available_tags.append(tag)
		captured["created_tags"].append(name)
		return tag

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
				reason = "Not Found"
				headers = {}
			raise discord.errors.NotFound(_FakeResponse(), "nope")
		return ch


shared_mod.shared.guild = FakeGuild(
	FakeForumChannel(forum.FORUM_CHANNEL_ID, [FakeTag(n) for n in
		["book club", "conventions", "food", "gaming", "karaoke", "outdoor",
		 "watch party", "volunteering", "other", "in-person", "online"]]),
	FakeChannel(forum.ANNOUNCEMENTS_CHANNEL_ID),
)


def make_event(sort=111, title="Anime Night", category="watch party", online=False,
		forum_thread_id=None, created_at=None, start_offset_days=3, full_description=None):
	ev = events.MeetupEvent(sort=sort)
	ev.title = title
	ev.description = "A watch party for fans."
	ev.full_description = full_description if full_description is not None else ev.description
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

	# 3b. missing tags are created automatically (needs manage-channels perm)
	ch = shared_mod.shared.guild._forum
	ch.available_tags = [t for t in ch.available_tags if t.name != "karaoke"]
	n_created = len(captured["created_tags"])
	tags = await forum.resolve_tags(ch, make_event(sort=366, category="karaoke"))
	check("missing tag auto-created", "karaoke" in captured["created_tags"])
	check("created tag returned", any(t.name == "karaoke" for t in tags))
	check("created tag stored on channel", any(t.name == "karaoke" for t in ch.available_tags))
	del captured["created_tags"][:]

	# 3c. tag creation failure (no permission) is non-fatal
	class NoPermChannel(FakeForumChannel):
		async def create_tag(self, name, *, moderator_ids=None):
			class _Resp:
				status = 403
				reason = "Forbidden"
				headers = {}
			raise discord.errors.Forbidden(_Resp(), "nope")
	no_perm = NoPermChannel(forum.FORUM_CHANNEL_ID, [FakeTag("food")])
	res = await forum._create_forum_tag(no_perm, "nope-tag")
	check("forbidden tag create is non-fatal", res is None)
	tags2 = await forum.resolve_tags(no_perm, make_event(sort=377, category="food"))
	check("existing tag still resolves without creating", any(t.name == "food" for t in tags2) and captured["created_tags"] == [])

	# 3d. full (untruncated) description wins over the 999-char event description
	long_text = ("Come hang out! " * 120).strip()  # ~1800 chars, way over 999
	ev_full = make_event(sort=344, full_description=long_text)
	ev_full.description = long_text[:940] + "... [full event](https://meetup.example)"
	await forum.create_forum_post(ev_full)
	check("full description in forum body", long_text in captured["threads"][3]["content"])
	check("truncated desc not in forum body", "... [full event](https://meetup.example)" not in captured["threads"][3]["content"])

	# 3c. even the full text is capped at discord's 4000-char forum limit
	huge = ("x" * 4500) + "END-MARKER"
	ev_huge = make_event(sort=355, full_description=huge)
	await forum.create_forum_post(ev_huge)
	huge_body = captured["threads"][4]["content"]
	check("4000-char forum limit respected", len(huge_body) <= forum.FORUM_POST_MAX_LEN)

	# 3d. update also prefers the full description
	t_full = FakeThread(id=880000000000000003)
	shared_mod.shared.guild.threads[880000000000000003] = t_full
	ev_full.forum_thread_id = 880000000000000003
	mark = len(captured["starter_edits"])
	await forum.update_forum_post(ev_full)
	check("update uses full description", long_text in captured["starter_edits"][mark]["content"])

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
	check("old event listed as happening", "- " + ev_in_old.start_time.strftime("%b %d") + ": [Anime Night]" in msgs[1])
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

	# 11. 30-day hold-back: far-out events get no forum post until they start
	# in less than a month; the daily check (ensure_forum_post) creates the
	# post once the event enters the window
	n_posts = len(captured["threads"])
	ev_far = make_event(sort=777, title="Far Out Event", start_offset_days=40)
	check("far-out event not in post window", forum.event_in_post_window(ev_far) is False)
	tid = await forum.ensure_forum_post(ev_far)
	check("far-out post held back", tid is None and len(captured["threads"]) == n_posts)
	check("held-back row keeps no thread id", ev_far.forum_thread_id is None)
	await forum.update_forum_post(ev_far)
	check("update path holds back far-out event", len(captured["threads"]) == n_posts)
	# naive datetimes don't crash the window check (interpreted as utc)
	ev_far.datetime = ev_far.datetime.replace(tzinfo=None)
	check("naive datetime handled in window check", forum.event_in_post_window(ev_far) is False)
	# events with no start time at all are held back
	ev_far_nostart = make_event(sort=778)
	ev_far_nostart.start_time = None
	check("missing start time held back", forum.event_in_post_window(ev_far_nostart) is False)
	# the daily catch-up creates the post once the event enters the window
	ev_far.start_time = dt.datetime.now(shared_mod.shared.est) + dt.timedelta(days=3)
	tid = await forum.ensure_forum_post(ev_far)
	check("catch-up creates post in window", tid == 990000000000000001 and len(captured["threads"]) == n_posts + 1)
	check("catch-up stores thread id", ev_far.forum_thread_id == 990000000000000001)
	# already-posted events are not touched by the catch-up (no duplicate)
	tid_again = await forum.ensure_forum_post(ev_far)
	check("posted event untouched by catch-up", tid_again == 990000000000000001 and len(captured["threads"]) == n_posts + 1)

	# 12. startup sync: creates in-window unposted events, skips far-out ones
	ev_far2 = make_event(sort=788, start_offset_days=45)
	ev_near = make_event(sort=789, start_offset_days=5)
	events.MeetupEvent.scan = classmethod(lambda cls, **kw: iter([ev_far2, ev_near]))
	try:
		await forum.sync_forum_posts()
	finally:
		events.MeetupEvent.scan = real_scan
	check("sync creates in-window post", ev_near.forum_thread_id == 990000000000000001)
	check("sync holds back far-out post", ev_far2.forum_thread_id is None)

	# 13. digest: far-out events are never "newly planned"; a held-back event
	# whose post was just created by the daily catch-up IS announced (detected
	# via the thread snowflake's creation time)
	ev_new_far = make_event(sort=790, title="Too Far Out", created_at=now, start_offset_days=60)
	fresh_thread_snowflake = int((int(now.timestamp() * 1000) - 1420070400000) << 22)
	ev_released = make_event(sort=791, title="Just Released", created_at=old, forum_thread_id=fresh_thread_snowflake)
	ev_released.start_time = at(4)
	events.MeetupEvent.scan = classmethod(lambda cls, **kw: iter([ev_in_old, ev_in_new, ev_on_new, ev_new_far, ev_released]))
	try:
		msgs_hold = forum._build_digest_messages(now)
	finally:
		events.MeetupEvent.scan = real_scan
	newly_sec = msgs_hold[1].split("## Newly planned events:")[1]
	check("far-out event not in newly planned", "[Too Far Out]" not in newly_sec)
	check("held-back event announced after catch-up", "[Just Released]" in newly_sec)
	check("old event still not newly planned", "[Anime Night]" not in newly_sec)

	# 14. out-of-band thread deletion: update recreates the post instead of
	# recursing update -> create -> update (the stale id must be dropped first)
	gone_thread_ev = make_event(sort=792, forum_thread_id=880000000000000004)
	n_posts = len(captured["threads"])
	await forum.update_forum_post(gone_thread_ev)
	check("404 thread recreated not recursed", len(captured["threads"]) == n_posts + 1 and gone_thread_ev.forum_thread_id == 990000000000000001)

	print("ALL PASSED")

asyncio.run(main())
