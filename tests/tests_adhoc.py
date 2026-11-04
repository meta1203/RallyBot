"""Offline verification for the ad hoc forum-event feature (no live token, no
ddb, no discord, no OpenRouter).

Run inside the project venv: .venv/bin/python tests/tests_adhoc.py
Follows the discord-bot-development skill's verification procedure and the
fake-layer pattern of tests/tests_forum_pilot.py: stub discord's Client.run,
subclass real discord types for fakes, stub every PynamoDB model method the
code under test can reach, then assert on captured fake-sends.
Prints a final all-passed marker only when every assertion holds.
"""
import asyncio
import datetime as dt
import json
import sys
import types

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

# ---- fake discord layer (must exist before importing bot modules) ----
captured = {
	"threads": [],
	"sends": [],
	"status_replies": [],  # (thread_id, content) posted by adhoc status replies
	"deleted_replies": [],
	"starter_edits": [],
	"thread_edits": [],
	"created_tags": [],
	"ai_calls": 0,  # llm.ai_extract_event call counter
}

import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed run)")


class FakeTag:
	def __init__(self, name, id=0):
		self.name = name
		self.id = id


class FakeStarter:
	def __init__(self, content="", edited_at=None):
		self.content = content
		self.edited_at = edited_at

	async def edit(self, **kw):
		captured["starter_edits"].append(kw)
		if "content" in kw:
			self.content = kw["content"]


class FakeThread(discord.Thread):
	# shadow the base class' read-only properties with plain attributes
	starter_message = None
	applied_tags = None
	archived = False
	parent_id = None
	owner_id = None

	def __init__(self, id, name="My Cool Event", parent_id=None, owner_id=5550001, content="", applied_tags=(), edited_at=None):
		self.id = id
		self.name = name
		self.parent_id = parent_id
		self.owner_id = owner_id
		self.starter_message = FakeStarter(content, edited_at)
		self.applied_tags = list(applied_tags)
		self.messages = {id: self.starter_message}
		self.deleted = False

	async def edit(self, **kw):
		captured["thread_edits"].append((self.id, dict(kw)))
		if "name" in kw:
			self.name = kw["name"]
		if "applied_tags" in kw:
			self.applied_tags = kw["applied_tags"]

	async def fetch_message(self, id):
		if id not in self.messages:
			class _Resp:
				status = 404
				reason = "Not Found"
				headers = {}
			raise discord.errors.NotFound(_Resp(), "nope")
		return self.messages[id]

	async def send(self, content):
		captured["sends"].append((self.id, content))
		msg = FakeStarter(content)
		msg.id = 770000000000000000 + len(captured["sends"])
		self.messages[msg.id] = msg
		return msg


class FakeForumChannel(discord.ForumChannel):
	# shadow the base class' read-only property with a plain attribute
	available_tags = ()
	active_threads = ()

	def __init__(self, id, tags):
		self.id = id
		self.available_tags = list(tags)
		self.active_threads = []
		self._archived = []

	def archived_threads(self, limit=None):
		# async iterator, like the real API
		async def _gen():
			for t in list(self._archived):
				yield t
		return _gen()

	async def create_tag(self, name, *, moderator_ids=None):
		tag = FakeTag(name, id=len(self.available_tags) + 1)
		self.available_tags.append(tag)
		captured["created_tags"].append(name)
		return tag

	async def create_thread(self, name, content, applied_tags):
		thread = FakeThread(id=990000000000000001, name=name, parent_id=self.id, content=content, applied_tags=applied_tags)
		captured["threads"].append({"name": name, "content": content, "tags": [t.name for t in applied_tags]})
		self.active_threads.append(thread)
		return thread, thread.starter_message


class FakeChannel(discord.TextChannel):
	def __init__(self, id):
		self.id = id

	async def send(self, content):
		captured["sends"].append((self.id, content))
		return None


# ---- import the bot modules with fakes in place ----
import events
import forum
import adhoc
import shared as shared_mod

# never touch real AWS from the test: stub the pynamo save
events.MeetupEvent.save = lambda self, *a, **kw: None

_not_found_resp = types.SimpleNamespace(status=404, reason="Not Found", headers={})


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
			raise discord.errors.NotFound(_not_found_resp, "nope")
		return ch


shared_mod.shared.guild = FakeGuild(
	FakeForumChannel(forum.FORUM_CHANNEL_ID, [FakeTag(n, i) for i, n in enumerate(
		["book club", "conventions", "food", "gaming", "karaoke", "outdoor",
		 "watch party", "volunteering", "other", "in-person", "online"])]),
	FakeChannel(forum.ANNOUNCEMENTS_CHANNEL_ID),
)


def check(name, cond):
	if not cond:
		print(f"FAIL: {name}")
		sys.exit(1)
	print(f"PASS: {name}")


async def main():
	# ---- task 1: source discriminator + guards ----
	ev_plain = events.MeetupEvent(sort=1)
	check("source defaults to meetup", (getattr(ev_plain, "source", None) or "meetup") == "meetup")

	# check_existing_event must not fetch meetup for ad hoc rows
	def _bomb(*a, **kw):
		raise AssertionError("meetup fetch attempted for an ad hoc event")
	real_fetch = events._meetup_url_to_json
	events._meetup_url_to_json = _bomb
	ev_adhoc = events.MeetupEvent(sort=990000000000000001)
	ev_adhoc.source = "forum"
	try:
		res = await events.check_existing_event(ev_adhoc)
	finally:
		events._meetup_url_to_json = real_fetch
	check("ad hoc event skips meetup recheck", res is True)

	# create_forum_post must refuse to create posts for ad hoc events
	n_threads = len(captured["threads"])
	tid = await forum.create_forum_post(ev_adhoc)
	check("create_forum_post refuses ad hoc event", tid is None and len(captured["threads"]) == n_threads)

	# sync_forum_posts must skip ad hoc rows entirely
	n_threads = len(captured["threads"])
	real_scan = events.MeetupEvent.scan
	events.MeetupEvent.scan = classmethod(lambda cls, **kw: iter([ev_adhoc]))
	try:
		await forum.sync_forum_posts()
	finally:
		events.MeetupEvent.scan = real_scan
	check("sync_forum_posts skips ad hoc rows", len(captured["threads"]) == n_threads)

	# ---- task 2: ForumEvent model + helpers ----
	row = adhoc.ForumEvent(sort=990000000000000001)
	check("ForumEvent id is forum_event", row.id == "forum_event")
	t = FakeThread(id=990000000000000001, parent_id=forum.FORUM_CHANNEL_ID, name="My Cool Event", content="hello")
	t.applied_tags = [FakeTag("nonpublic", 3), FakeTag("gaming", 2)]
	h1 = adhoc._starter_content_hash(t, t.starter_message)
	check("hash is deterministic", h1 == adhoc._starter_content_hash(t, t.starter_message))
	t2 = FakeThread(id=t.id, name=t.name, content="changed")
	check("hash is content-sensitive", h1 != adhoc._starter_content_hash(t2, t2.starter_message))
	t3 = FakeThread(id=t.id, name=t.name, content="hello", applied_tags=[FakeTag("gaming", 2)])
	check("hash is tag-sensitive", h1 != adhoc._starter_content_hash(t3, t3.starter_message))
	check("nonpublic detected", adhoc._has_nonpublic_tag(t))
	check("nonpublic absent -> False", not adhoc._has_nonpublic_tag(t3))
	naive = dt.datetime(2026, 11, 5, 12, 0, 0)
	aware = dt.datetime(2026, 11, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
	check("norm_dt equalizes naive/aware utc", adhoc._norm_dt(naive) == aware and adhoc._norm_dt(None) is None)

	# ---- task 3: llm.ai_extract_event ----
	import llm
	real_chat = llm.chat
	llm.OPENROUTER_API_KEY = "test-key"  # tests stub chat entirely; never hit OpenRouter
	valid_json = json.dumps({
		"is_event": True, "title": "Karaoke Night", "start": "2026-11-07T19:00:00-06:00",
		"end": None, "duration_minutes": None, "location": "Big Mouth Chicago",
		"online": False, "description": "Sing!", "category": "karaoke", "missing": [],
	})
	fenced = "```json\n" + valid_json + "\n```"
	prose = "Sure! Here is the JSON you asked for:\n" + valid_json + "\nHope that helps!"

	def stub_chat(reply, calls=None):
		calls = {"n": 0} if calls is None else calls
		def _chat(messages, timeout=60):
			calls["n"] += 1
			return reply() if callable(reply) else reply
		llm.chat = _chat
		return calls

	calls = stub_chat(valid_json)
	res = llm.ai_extract_event("Karaoke Night", "Nov 7, 7pm at Big Mouth Chicago")
	check("extract parses valid json", isinstance(res, dict) and res["title"] == "Karaoke Night")
	check("extract makes one call for clean json", calls["n"] == 1)

	calls = stub_chat(fenced)
	res = llm.ai_extract_event("t", "b")
	check("extract strips code fences", isinstance(res, dict) and res["is_event"] is True)

	calls = stub_chat(prose)
	res = llm.ai_extract_event("t", "b")
	check("extract slices json out of prose", isinstance(res, dict) and res["category"] == "karaoke")

	garbage_calls = stub_chat("this is not json at all, sorry!")
	res = llm.ai_extract_event("t", "b")
	check("garbage twice -> None", res is None and garbage_calls["n"] == 2)

	def _raise(messages, timeout=60):
		raise RuntimeError("network down")
	llm.chat = _raise
	res = llm.ai_extract_event("t", "b")
	llm.chat = real_chat
	check("transport failure -> None (transient, not missing info)", res is None)

	# the system prompt must interpolate the category list at call time
	prompt_seen = {}
	def _capture_chat(messages, timeout=60):
		prompt_seen["system"] = messages[0]["content"]
		return valid_json
	llm.chat = _capture_chat
	llm.ai_extract_event("t", "b")
	llm.chat = real_chat
	check("system prompt carries category list", "karaoke" in prompt_seen.get("system", ""))

	print("ALL PASSED")

asyncio.run(main())
