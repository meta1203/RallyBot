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

	async def delete(self, *a, **kw):
		captured["deleted_replies"].append(getattr(self, "id", None))


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

	async def active_threads(self):
		# mirrors the guild-level REST call the real sweep uses
		return list(self.threads.values())

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
	check("hash ignores tag changes", h1 == adhoc._starter_content_hash(t3, t3.starter_message))
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

	# ---- tasks 4-6: status replies, process_thread pipeline, nonpublic ----
	# stub all pynamo access: rows live in plain dicts
	forum_rows = {}
	event_rows = {}
	deleted_event_sorts = []
	deleted_rows = []
	ForumDNE = adhoc.ForumEvent.DoesNotExist
	EventDNE = events.MeetupEvent.DoesNotExist

	def stub_forum_get(cls, hash_key, sort_key):
		key = int(sort_key)
		if key not in forum_rows:
			raise ForumDNE()
		return forum_rows[key]
	adhoc.ForumEvent.get = classmethod(stub_forum_get)
	def _forum_save(self, *a, **kw):
		forum_rows[int(self.sort)] = self
	adhoc.ForumEvent.save = _forum_save
	def _forum_delete(self, *a, **kw):
		deleted_rows.append(int(self.sort))
		forum_rows.pop(int(self.sort), None)
	adhoc.ForumEvent.delete = _forum_delete
	def _event_get(cls, hash_key, sort_key):
		key = int(sort_key)
		if key not in event_rows:
			raise EventDNE()
		return event_rows[key]
	events.MeetupEvent.get = classmethod(_event_get)
	def _event_save(self, *a, **kw):
		event_rows[int(self.sort)] = self
	events.MeetupEvent.save = _event_save
	async def _event_delete(self, *a, **kw):
		deleted_event_sorts.append(int(self.sort))
		event_rows.pop(int(self.sort), None)
	events.MeetupEvent.delete = _event_delete
	# scan stub: snowflake_id-index lookups filter event_rows by the
	# condition's value (fc.values = (Path, {'N': '<int>'})); anything else
	# returns all rows (timestamp-index digest scans etc. are re-stubbed
	# per-scenario where needed)
	def _scan_stub(cls, **kw):
		fc = kw.get("filter_condition")
		if kw.get("index_name") == "snowflake_id-index" and fc is not None:
			try:
				tid = int(fc.values[1].value["N"])  # Value.value = {'N': '<int>'}
			except Exception:
				tid = None
			return iter([ev for ev in list(event_rows.values())
				if ev.source == "forum" and int(ev.snowflake_id or 0) == tid])
		return iter(list(event_rows.values()))
	events.MeetupEvent.scan = classmethod(_scan_stub)

	real_extract = llm.ai_extract_event
	ai_responses = []
	def _fake_extract(title, body):
		captured["ai_calls"] += 1
		if not ai_responses:
			raise AssertionError("unexpected AI call")
		return ai_responses.pop(0)
	llm.ai_extract_event = _fake_extract

	def make_extraction(**over):
		base = {"is_event": True, "title": "Board Game Night", "start": "2026-11-10T19:00:00-06:00",
			"end": None, "duration_minutes": None, "location": "The Game Room", "online": False,
			"description": "Bring your favorite games.", "category": "gaming", "missing": []}
		base.update(over)
		return base

	def reset_scenario():
		forum_rows.clear(); event_rows.clear()
		deleted_event_sorts.clear(); deleted_rows.clear()
		ai_responses.clear()
		captured["sends"].clear(); captured["deleted_replies"].clear()
		captured["ai_calls"] = 0
		shared_mod.shared.guild.threads.clear()

	def new_post(tid, content="Come play board games Friday 7pm at The Game Room!", tags=(), edited_at=None, owner=5550001, name="Board Game Night"):
		t = FakeThread(id=tid, parent_id=forum.FORUM_CHANNEL_ID, name=name, content=content, applied_tags=tags, owner_id=owner, edited_at=edited_at)
		shared_mod.shared.guild.threads[tid] = t
		return t

	# -- valid post -> event record + added reply --
	reset_scenario()
	t = new_post(910000000000000001)
	ai_responses.append(make_extraction())
	await adhoc.process_thread(t)
	row = forum_rows[t.id]
	ev = event_rows[t.id]
	check("row created with state event", row.state == "event" and row.event_sort == t.id)
	check("event row written with source forum", ev.source == "forum" and ev.sort == t.id)
	check("event link is the forum post", ev.link == f"https://discord.com/channels/{shared_mod.shared.guild.id}/{t.id}")
	start = dt.datetime.fromisoformat("2026-11-10T19:00:00-06:00")
	check("1-hour default endtime", ev.endtime == start + dt.timedelta(hours=1))
	check("created_at stamped", ev.created_at is not None)
	check("no scheduled event / no forum thread id", int(ev.snowflake_id) == 0 and ev.forum_thread_id is None)
	check("added reply pings author + nonpublic hint", len(captured["sends"]) == 1 and "<@5550001>" in captured["sends"][0][1] and "nonpublic" in captured["sends"][0][1])
	check("reply id tracked on row", row.reply_message_id is not None)
	check("last_extraction stored", json.loads(row.last_extraction)["title"] == "Board Game Night")
	check("one AI call", captured["ai_calls"] == 1)
	created_at_1 = ev.created_at

	# -- unchanged post retrigger: no AI call, no new reply --
	await adhoc.process_thread(t)
	check("no AI call on unchanged post", captured["ai_calls"] == 1)
	check("retrigger keeps created_at", event_rows[t.id].created_at == created_at_1)
	check("no duplicate reply", len(captured["sends"]) == 1)

	# -- edited_at change with identical content still reanalyzes --
	t.starter_message.edited_at = dt.datetime.now(dt.timezone.utc)
	ai_responses.append(make_extraction(title="Board Game Night v2"))
	await adhoc.process_thread(t)
	check("edited_at change triggers reanalysis", captured["ai_calls"] == 2)
	check("recompute updates the record", event_rows[t.id].title == "Board Game Night v2")
	check("recompute preserves created_at", event_rows[t.id].created_at == created_at_1)

	# -- below the floor: start missing --
	reset_scenario()
	t = new_post(910000000000000002)
	ai_responses.append(make_extraction(start=None, missing=["start"]))
	await adhoc.process_thread(t)
	row = forum_rows[t.id]
	check("missing start -> no event row", t.id not in event_rows and row.state == "missing_info")
	check("missing start reply lists the field", any("start date/time" in c for _, c in captured["sends"]))

	# -- below the floor: location missing --
	reset_scenario()
	t = new_post(910000000000000003)
	ai_responses.append(make_extraction(location=None, missing=["location"]))
	await adhoc.process_thread(t)
	check("missing location -> no event row", t.id not in event_rows and forum_rows[t.id].state == "missing_info")
	check("missing location reply lists the field", any("location" in c for _, c in captured["sends"]))

	# -- not an event: silent --
	reset_scenario()
	t = new_post(910000000000000004, content="just chatting about the new episode")
	ai_responses.append({"is_event": False})
	await adhoc.process_thread(t)
	check("not_event state, no reply", forum_rows[t.id].state == "not_event" and captured["sends"] == [])
	check("not_event drops stale record + old reply", t.id not in event_rows and forum_rows[t.id].reply_message_id is None)

	# -- transient AI failure: silent, retryable --
	reset_scenario()
	t = new_post(910000000000000005)
	ai_responses.append(None)
	await adhoc.process_thread(t)
	check("transient failure: no reply, row unsaved", captured["sends"] == [] and t.id not in forum_rows)

	# -- edit below the floor deletes the stale record --
	reset_scenario()
	t = new_post(910000000000000006)
	ai_responses.append(make_extraction())
	await adhoc.process_thread(t)
	t.starter_message.content = "moved, details TBD"
	t.starter_message.edited_at = dt.datetime.now(dt.timezone.utc)
	ai_responses.append(make_extraction(location=None, missing=["location"]))
	await adhoc.process_thread(t)
	check("edit below floor deletes event record", t.id not in event_rows and forum_rows[t.id].state == "missing_info")
	check("missing-info reply replaces added reply", len(captured["sends"]) == 2 and len(captured["deleted_replies"]) == 1)

	# -- past-event guard: no reanalysis after the event started --
	reset_scenario()
	t = new_post(910000000000000007)
	past_start = (dt.datetime.now(shared_mod.shared.central_time) - dt.timedelta(days=2)).isoformat()
	ai_responses.append(make_extraction(start=past_start))
	await adhoc.process_thread(t)
	n_calls = captured["ai_calls"]
	t.starter_message.content = "bump, see you all!"
	t.starter_message.edited_at = dt.datetime.now(dt.timezone.utc)
	await adhoc.process_thread(t)
	check("past event not reanalyzed", captured["ai_calls"] == n_calls)
	check("past event record kept", t.id in event_rows)

	# -- bot-owned posts are skipped --
	reset_scenario()
	class _User:
		id = 42
	class _Client:
		user = _User()
	shared_mod.shared.client = _Client()
	t = new_post(910000000000000008, owner=42)
	await adhoc.process_thread(t)
	check("bot-owned post skipped", captured["ai_calls"] == 0 and t.id not in forum_rows)
	shared_mod.shared.client = None

	# -- wrong channel is skipped --
	reset_scenario()
	t = FakeThread(id=910000000000000009, parent_id=999)
	await adhoc.process_thread(t)
	check("non-forum thread ignored", captured["ai_calls"] == 0 and 910000000000000009 not in forum_rows)

	# -- nonpublic: add -> delete + once-per-application reply --
	reset_scenario()
	t = new_post(910000000000000010, tags=[FakeTag("gaming", 2)])
	ai_responses.append(make_extraction())
	await adhoc.process_thread(t)
	t.applied_tags = [FakeTag("gaming", 2), FakeTag("nonpublic", 99)]
	await adhoc.process_thread(t)
	check("nonpublic deletes event record", t.id not in event_rows and deleted_event_sorts == [t.id])
	check("nonpublic reply posted", any("`nonpublic` tag" in c for _, c in captured["sends"]))
	check("nonpublic_seen set", forum_rows[t.id].nonpublic_seen is True and forum_rows[t.id].state == "nonpublic")
	n_sends = len(captured["sends"])
	await adhoc.process_thread(t)
	check("no duplicate nonpublic reply", len(captured["sends"]) == n_sends)

	# -- nonpublic: remove -> recreate without an AI call --
	t.applied_tags = [FakeTag("gaming", 2)]
	await adhoc.process_thread(t)
	check("tag removal recreates record", t.id in event_rows and forum_rows[t.id].state == "event")
	check("recreation used no AI call", captured["ai_calls"] == 1)
	check("recreation posts the added reply again", len(captured["sends"]) == n_sends + 1)
	check("nonpublic_seen reset", forum_rows[t.id].nonpublic_seen is False)

	# -- nonpublic: re-add alerts again (once per application) --
	t.applied_tags = [FakeTag("gaming", 2), FakeTag("nonpublic", 99)]
	await adhoc.process_thread(t)
	check("re-added tag alerts again", len(captured["sends"]) == n_sends + 2 and t.id not in event_rows)

	# -- nonpublic on a fresh post: reply only --
	reset_scenario()
	t = new_post(910000000000000011, tags=[FakeTag("nonpublic", 99)])
	await adhoc.process_thread(t)
	check("nonpublic fresh post: reply only, no record", t.id not in event_rows and forum_rows[t.id].state == "nonpublic" and len(captured["sends"]) == 1)

	# -- nonpublic on a past event's post still deletes the record --
	reset_scenario()
	t = new_post(910000000000000012)
	ai_responses.append(make_extraction(start=past_start))
	await adhoc.process_thread(t)
	t.applied_tags = [FakeTag("nonpublic", 99)]
	await adhoc.process_thread(t)
	check("past event record deleted on nonpublic", t.id not in event_rows)

	# -- cleanup_deleted_post --
	reset_scenario()
	t = new_post(910000000000000013)
	ai_responses.append(make_extraction())
	await adhoc.process_thread(t)
	await adhoc.cleanup_deleted_post(t.id)
	check("cleanup deletes rows", t.id not in forum_rows and t.id not in event_rows and deleted_rows == [t.id])
	await adhoc.cleanup_deleted_post(999999999999999999)
	check("cleanup of unknown id is a no-op", deleted_rows == [t.id])

	# ---- task 7: real-time event wiring in main.py ----
	import main
	real_process = adhoc.process_thread
	real_cleanup = adhoc.cleanup_deleted_post
	processed = []
	cleaned = []
	async def _spy_process(thread, **kw):
		processed.append(thread.id)
	async def _spy_cleanup(thread_id):
		cleaned.append(int(thread_id))
	adhoc.process_thread = _spy_process
	adhoc.cleanup_deleted_post = _spy_cleanup

	class FakeRawThreadUpdate(discord.RawThreadUpdateEvent):
		def __init__(self, thread_id, parent_id):
			self.thread_id = thread_id
			self.parent_id = parent_id
	class FakeRawMessageEdit(discord.RawMessageUpdateEvent):
		def __init__(self, message_id, channel_id, guild_id=1219601473948614737):
			self.message_id = message_id
			self.channel_id = channel_id
			self.guild_id = guild_id
	class FakeRawThreadDelete(discord.RawThreadDeleteEvent):
		def __init__(self, thread_id, parent_id):
			self.thread_id = thread_id
			self.parent_id = parent_id

	# thread create delegates (parent filtering lives in process_thread)
	await main.on_thread_create(FakeThread(id=910000000000000020, parent_id=forum.FORUM_CHANNEL_ID))
	await main.on_thread_create(FakeThread(id=910000000000000021, parent_id=999))
	check("thread_create delegates all threads", processed == [910000000000000020, 910000000000000021])

	# raw thread update fetches (cached or not) and routes by parent
	shared_mod.shared.guild.threads[910000000000000020] = FakeThread(id=910000000000000020, parent_id=forum.FORUM_CHANNEL_ID)
	processed.clear()
	await main.on_raw_thread_update(FakeRawThreadUpdate(910000000000000020, forum.FORUM_CHANNEL_ID))
	await main.on_raw_thread_update(FakeRawThreadUpdate(910000000000000022, 999))
	check("thread_update routes by parent", processed == [910000000000000020])

	# uncached thread: fetch_channel resolves via the FakeGuild's registry
	processed.clear()
	t_uncached = FakeThread(id=910000000000000023, parent_id=forum.FORUM_CHANNEL_ID)
	shared_mod.shared.guild.threads[910000000000000023] = t_uncached
	await main.on_raw_thread_update(FakeRawThreadUpdate(910000000000000023, forum.FORUM_CHANNEL_ID))
	check("thread_update handles uncached threads", processed == [910000000000000023])
	# a truly unknown thread 404s on fetch and is swallowed, not raised
	class _MissingGuild(FakeGuild):
		async def fetch_channel(self, id):
			raise discord.errors.NotFound(_not_found_resp, "nope")
	shared_mod.shared.guild = _MissingGuild(shared_mod.shared.guild._forum, shared_mod.shared.guild._announce)
	shared_mod.shared.guild.threads = {}
	await main.on_raw_thread_update(FakeRawThreadUpdate(910000000000000024, forum.FORUM_CHANNEL_ID))
	check("404 on thread fetch is swallowed", processed == [910000000000000023])
	shared_mod.shared.guild = FakeGuild(shared_mod.shared.guild._forum, shared_mod.shared.guild._announce)
	shared_mod.shared.guild.threads[910000000000000023] = t_uncached

	# starter edit (message_id == channel_id) reanalyzes; replies don't
	processed.clear()
	await main.on_raw_message_edit(FakeRawMessageEdit(910000000000000023, 910000000000000023))
	check("starter edit triggers reanalysis", processed == [910000000000000023])
	await main.on_raw_message_edit(FakeRawMessageEdit(770000000000000001, 910000000000000023))
	check("reply edit ignored", processed == [910000000000000023])
	await main.on_raw_message_edit(FakeRawMessageEdit(910000000000000023, 910000000000000023, guild_id=None))
	check("guild-less edit ignored", processed == [910000000000000023])

	# thread delete routes to cleanup
	await main.on_raw_thread_delete(FakeRawThreadDelete(910000000000000023, forum.FORUM_CHANNEL_ID))
	check("thread delete routes to cleanup", cleaned == [910000000000000023])

	adhoc.process_thread = real_process
	adhoc.cleanup_deleted_post = real_cleanup

	# ---- task 8: startup sweep ----
	adhoc.SWEEP_DELAY = 0  # no sleeps in tests
	# stub the scan BEFORE the first sweep: this box has ambient read-only
	# AWS creds and the real scan would deserialize prod table rows
	adhoc.ForumEvent.scan = classmethod(lambda cls, **kw: iter(list(forum_rows.values())))
	# restore the dict-backed stubs the wiring tests replaced
	def stub_forum_get2(cls, hash_key, sort_key):
		key = int(sort_key)
		if key not in forum_rows:
			raise ForumDNE()
		return forum_rows[key]
	adhoc.ForumEvent.get = classmethod(stub_forum_get2)
	async def _spy_process2(thread, **kw):
		processed.append(thread.id)
	adhoc.process_thread = _spy_process2
	async def _spy_cleanup2(thread_id):
		cleaned.append(int(thread_id))
	adhoc.cleanup_deleted_post = _spy_cleanup2

	reset_scenario()
	g = shared_mod.shared.guild
	t_active = new_post(910000000000000030)  # registered in guild.threads via new_post
	t_archived = FakeThread(id=910000000000000031, parent_id=forum.FORUM_CHANNEL_ID)
	g._forum._archived = [t_archived]
	processed.clear()
	await adhoc.startup_sweep()
	check("sweep processes active + archived threads", sorted(processed) == [910000000000000030, 910000000000000031])
	check("sweep creates the nonpublic tag", "nonpublic" in captured["created_tags"])
	g._forum.available_tags = [t for t in g._forum.available_tags if t.name != "nonpublic"]
	captured["created_tags"].clear()
	g._forum.available_tags.append(FakeTag("nonpublic", 88))
	await adhoc.startup_sweep()
	check("existing nonpublic tag not recreated", captured["created_tags"] == [])
	g._forum._archived = []

	# reconciliation: rows whose thread is gone get cleaned up, live ones kept
	reset_scenario()
	g.threads[910000000000000030] = FakeThread(id=910000000000000030, parent_id=forum.FORUM_CHANNEL_ID)
	live_row = adhoc.ForumEvent(sort=910000000000000030, thread_id=910000000000000030)
	gone_row = adhoc.ForumEvent(sort=910000000000000032, thread_id=910000000000000032)
	forum_rows[910000000000000030] = live_row
	forum_rows[910000000000000032] = gone_row
	event_rows.clear()
	adhoc.ForumEvent.scan = classmethod(lambda cls, **kw: iter([live_row, gone_row]))
	cleaned.clear()
	await adhoc.startup_sweep()
	check("offline-deleted post cleaned up", cleaned == [910000000000000032])

	# inaccessible (403) thread: fail closed, keep the record
	class _ForbiddenGuild(FakeGuild):
		async def fetch_channel(self, id):
			if int(id) == 910000000000000032:
				raise discord.errors.Forbidden(_not_found_resp, "nope")
			return await super().fetch_channel(id)
	saved_guild = shared_mod.shared.guild
	shared_mod.shared.guild = _ForbiddenGuild(g._forum, g._announce)
	shared_mod.shared.guild.threads = {910000000000000030: FakeThread(id=910000000000000030, parent_id=forum.FORUM_CHANNEL_ID)}
	forum_rows[910000000000000032] = gone_row
	cleaned.clear()
	await adhoc.startup_sweep()
	check("403 thread keeps its record (fail closed)", cleaned == [])
	shared_mod.shared.guild = saved_guild

	adhoc.ForumEvent.get = classmethod(stub_forum_get)
	adhoc.process_thread = real_process
	adhoc.cleanup_deleted_post = real_cleanup

	# ---- tasks 10-12: recurrence & occurrence materialization ----
	# (the task-8 reconciliation test left a fixed scan stub behind; point it
	# back at the live forum_rows dict for this section)
	adhoc.ForumEvent.scan = classmethod(lambda cls, **kw: iter(list(forum_rows.values())))
	# parsing / validation
	check("recurrence: weekly from weekday name", adhoc._parse_recurrence({"kind": "weekly", "weekday": "friday", "time": "19:00"}) == {"kind": "weekly", "weekday": 4, "hour": 19, "minute": 0, "tz": "America/Chicago"})
	check("recurrence: stored-rule shape accepted", adhoc._parse_recurrence({"kind": "biweekly", "weekday": 2, "hour": 18, "minute": 30, "anchor": "2026-11-04T18:30:00-06:00"}) is not None)
	check("recurrence: bad kind rejected", adhoc._parse_recurrence({"kind": "daily", "weekday": 1, "time": "19:00"}) is None)
	check("recurrence: bad time rejected", adhoc._parse_recurrence({"kind": "weekly", "weekday": 1, "time": "25:00"}) is None)
	check("recurrence: missing weekday rejected", adhoc._parse_recurrence({"kind": "weekly", "time": "19:00"}) is None)
	check("recurrence: monthly day range enforced", adhoc._parse_recurrence({"kind": "monthly", "day_of_month": 40, "time": "19:00"}) is None)
	check("recurrence: non-dict rejected", adhoc._parse_recurrence("every friday lol") is None)

	# next-occurrence math (America/Chicago)
	ct = shared_mod.shared.central_time
	wed_noon = dt.datetime(2026, 11, 4, 12, 0, tzinfo=ct)
	nf = adhoc._next_occurrence({"kind": "weekly", "weekday": 4, "hour": 19, "minute": 0}, wed_noon)
	check("next weekly occurrence", nf == dt.datetime(2026, 11, 6, 19, 0, tzinfo=ct))
	fri_night = dt.datetime(2026, 11, 6, 20, 0, tzinfo=ct)
	nf2 = adhoc._next_occurrence({"kind": "weekly", "weekday": 4, "hour": 19, "minute": 0}, fri_night)
	check("same-day past time rolls a week", nf2 == dt.datetime(2026, 11, 13, 19, 0, tzinfo=ct))
	nb = adhoc._next_occurrence({"kind": "biweekly", "weekday": 4, "hour": 19, "minute": 0, "anchor": "2026-11-06T19:00:00-06:00"}, fri_night)
	check("biweekly stays on anchor phase", nb == dt.datetime(2026, 11, 20, 19, 0, tzinfo=ct))
	nm = adhoc._next_occurrence({"kind": "monthly", "day_of_month": 31, "hour": 19, "minute": 0}, dt.datetime(2027, 1, 31, 20, 0, tzinfo=ct))
	check("monthly skips months lacking the day", nm == dt.datetime(2027, 3, 31, 19, 0, tzinfo=ct))

	# occurrence sort keys: unique per thread+minute, chronological, in a
	# band clear of meetup guids and discord snowflakes
	s_a = adhoc._occurrence_sort(nf, 111)
	s_b = adhoc._occurrence_sort(nf, 222)
	check("occurrence sort unique per thread", s_a != s_b)
	check("occurrence sort chronological", s_a < adhoc._occurrence_sort(nf2, 111))
	check("occurrence sort in its own band", 1e11 < s_a < 7e17)

	# full pipeline: weekly post materializes through the horizon
	reset_scenario()
	t = new_post(910000000000000040, name="Weekly Anime Night", content="Every Friday 7pm at the shop!")
	now = dt.datetime.now(shared_mod.shared.central_time)
	rule = adhoc._parse_recurrence({"kind": "weekly", "weekday": "friday", "time": "19:00"})
	expected_dates = adhoc._upcoming_occurrence_dates(rule, now, now + adhoc.OCCURRENCE_HORIZON)
	check("horizon yields several weekly dates", len(expected_dates) >= 4)
	ai_responses.append(make_extraction(title="Weekly Anime Night", start=expected_dates[0].isoformat(), recurrence={"kind": "weekly", "weekday": "friday", "time": "19:00"}))
	await adhoc.process_thread(t)
	row = forum_rows[t.id]
	occs = [ev for ev in event_rows.values() if ev.source == "forum" and int(ev.snowflake_id or 0) == t.id]
	check("weekly series materialized through horizon", len(occs) == len(expected_dates))
	check("first occurrence is the next friday", min(ev.start_time for ev in occs) == expected_dates[0])
	check("occurrence rows in the event partition", all(ev.id == "event" for ev in occs))
	check("occurrence timestamps synced", all(int(ev.timestamp) == int(ev.start_time.timestamp() * 1000) for ev in occs))
	check("occurrence endtime keeps series duration", all(ev.endtime == ev.start_time + dt.timedelta(hours=1) for ev in occs))
	check("created_at only on the first occurrence", sum(1 for ev in occs if ev.created_at is not None) == 1)
	check("event_sort points at the next occurrence", row.event_sort == adhoc._occurrence_sort(expected_dates[0], t.id))
	check("recurring reply states the interpretation", any("weekly event on Fridays" in c and "7:00 PM" in c for _, c in captured["sends"]))
	check("recurrence stored on the row", json.loads(row.recurrence)["kind"] == "weekly")
	check("stored extraction carries the rule", json.loads(row.last_extraction)["recurrence"]["kind"] == "weekly")
	# idempotent: unchanged post reprocessed -> no new rows, no new AI call
	n_rows = len(event_rows)
	await adhoc.process_thread(t)
	check("unchanged recurring post does nothing", len(event_rows) == n_rows and captured["ai_calls"] == 1)

	# schedule change via edit: past rows stay (history), future rebuilt
	first_ev = min(occs, key=lambda e: e.start_time)
	first_ev.start_time = now - dt.timedelta(days=1)
	first_ev.timestamp = int(first_ev.start_time.timestamp() * 1000)
	first_ev.endtime = first_ev.start_time + dt.timedelta(hours=1)
	t.starter_message.content = "Correction: we meet every MONDAY 7pm at the shop!"
	t.starter_message.edited_at = dt.datetime.now(dt.timezone.utc)
	next_monday = adhoc._next_occurrence({"kind": "weekly", "weekday": 0, "hour": 19, "minute": 0}, dt.datetime.now(shared_mod.shared.central_time))
	ai_responses.append(make_extraction(title="Weekly Anime Night", start=next_monday.isoformat(), recurrence={"kind": "weekly", "weekday": "monday", "time": "19:00"}))
	await adhoc.process_thread(t)
	occs2 = [ev for ev in event_rows.values() if ev.source == "forum" and int(ev.snowflake_id or 0) == t.id]
	check("past occurrence kept on schedule change", first_ev.sort in {e.sort for e in occs2})
	now2 = dt.datetime.now(shared_mod.shared.central_time)
	future2 = [e for e in occs2 if e.start_time > now2]
	check("future occurrences rebuilt on the new weekday", future2 and all(e.start_time.weekday() == 0 for e in future2))
	check("recurring edit reanalyzes (past-guard exempt)", captured["ai_calls"] == 2)

	# nonpublic on a recurring post removes the whole series (past + future)
	t.applied_tags = [FakeTag("gaming", 2), FakeTag("nonpublic", 99)]
	await adhoc.process_thread(t)
	check("nonpublic removes the whole series", not any(int(ev.snowflake_id or 0) == t.id for ev in event_rows.values()))
	n_sends_recurring = len(captured["sends"])
	# tag removal re-materializes the series with no AI call
	t.applied_tags = [FakeTag("gaming", 2)]
	await adhoc.process_thread(t)
	occs3 = [ev for ev in event_rows.values() if ev.source == "forum" and int(ev.snowflake_id or 0) == t.id]
	rule3 = adhoc._parse_recurrence(json.loads(forum_rows[t.id].recurrence))
	_now3 = dt.datetime.now(shared_mod.shared.central_time)
	expected3 = adhoc._upcoming_occurrence_dates(rule3, _now3, _now3 + adhoc.OCCURRENCE_HORIZON)
	check("tag removal re-materializes the series", len(occs3) == len(expected3) and forum_rows[t.id].state == "event")
	check("series rebuild used no AI call", captured["ai_calls"] == 2)

	# daily fill job: a gap in the future window (e.g. the bot was offline
	# while occurrences slipped past) gets refilled from the stored rule
	by_start = sorted(occs3, key=lambda e: e.start_time)
	gone1, gone2 = by_start[-2], by_start[-1]
	del event_rows[gone1.sort]
	del event_rows[gone2.sort]
	await adhoc.materialize_future_occurrences()
	occs4 = [ev for ev in event_rows.values() if ev.source == "forum" and int(ev.snowflake_id or 0) == t.id]
	now4 = dt.datetime.now(shared_mod.shared.central_time)
	future4 = [e for e in occs4 if e.start_time > now4]
	check("fill job restores the future window", len(future4) == len(expected3))
	check("fill job recreates the missing occurrences", {gone1.sort, gone2.sort} <= {e.sort for e in occs4})
	check("fill job adds nothing beyond the refill", len(occs4) == len(occs3))
	check("fill job leaves created_at off refilled rows", all(e.created_at is None for e in (gone1, gone2)))

	print("ALL PASSED")

asyncio.run(main())
