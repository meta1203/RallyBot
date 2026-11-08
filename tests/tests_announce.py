"""Offline verification for the weekly digest send ledger + /rally admin
reannounce in forum.py (no live token, no real ddb, no AWS calls).

Covers: the digest slot math (an exact-slot fire resolves to this week, not
last week), ledger recording on full/partial/failed/empty/quiet sends, the
reannounce behaviors (no-op when already sent, hole-completion without
force, forced resend, recovery of a never-run slot, empty-week handling),
the startup gap check, and the admin permission gate + command registration.

Run inside the project venv: .venv/bin/python tests/tests_announce.py
"""
import asyncio
import datetime as dt
import io
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# quiet mode must be OFF by default here (sends are captured directly)
os.environ.pop("QUIET_RALLY", None)

import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed run)")

import forum
import shared
from pynamodb.exceptions import DoesNotExist

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
	global PASS, FAIL
	if cond:
		PASS += 1
		print(f"  ok: {name}")
	else:
		FAIL += 1
		print(f"FAIL: {name} {detail}")


CT = shared.shared.central_time

# ---- fake ddb store (stubbed before any model call; nothing hits AWS) ----
ledger_store: dict[int, dict] = {}


def _fake_ledger_get(hash_key, sort_ms):
	row = ledger_store.get(int(sort_ms))
	if row is None:
		raise DoesNotExist
	out = forum.AnnounceLedger(sort=int(sort_ms))
	out.data = row["data"]
	return out


def _fake_ledger_save(self, *a, **kw):
	ledger_store[int(self.sort)] = {"data": self.data}


forum.AnnounceLedger.get = staticmethod(_fake_ledger_get)
forum.AnnounceLedger.save = _fake_ledger_save


def _ledger_data(slot_ms):
	row = ledger_store.get(int(slot_ms))
	return json.loads(row["data"]) if row else None


# ---- fake discord objects ----
class FakeChannel(discord.TextChannel):
	"""Subclasses the real TextChannel so the isinstance guard in
	_announcements_channel passes; __init__ is skipped (expected Pyright
	noise on offline-test fakes)."""

	def __init__(self, fail_indexes=()):
		self.sent: list[str] = []
		self.next_id = 9000
		self.fail_indexes = set(fail_indexes)

	async def send(self, content):
		if len(self.sent) in self.fail_indexes:
			# recorded before appending: the failing send is not kept
			self.sent.append("")
			raise RuntimeError("discord send failed (simulated)")
		self.next_id += 1
		self.sent.append(content)
		return types.SimpleNamespace(id=self.next_id)


class FakeGuild:
	def __init__(self, channel):
		self._channel = channel

	def get_channel_or_thread(self, cid):
		return self._channel if cid == forum.ANNOUNCEMENTS_CHANNEL_ID else None


class FakeResponse:
	def __init__(self):
		self.deferred = False
		self.sends: list[dict] = []

	async def defer(self, **kwargs):
		self.deferred = True
		self.sends.append({"defer": kwargs})

	async def send_message(self, *args, **kwargs):
		entry = dict(kwargs)
		if args:
			entry["content"] = args[0]
		self.sends.append(entry)


class FakeFollowup:
	def __init__(self):
		self.sends: list[dict] = []

	async def send(self, *args, **kwargs):
		entry = dict(kwargs)
		if args:
			entry["content"] = args[0]
		self.sends.append(entry)


def make_interaction():
	inter = types.SimpleNamespace()
	inter.user = "tester"
	inter.response = FakeResponse()
	inter.followup = FakeFollowup()
	return inter


def reply_of(inter):
	msgs = [s.get("content") for s in inter.followup.sends if s.get("content")]
	return msgs[-1] if msgs else None


def slot_ms_for(week="current"):
	slot = forum._digest_slot(dt.datetime.now(CT))
	if week == "previous":
		slot -= dt.timedelta(days=7)
	return int(slot.timestamp() * 1000)


def set_digest(messages):
	forum._build_digest_messages = lambda now=None: list(messages)


# ---- slot math ----
# sunday june 7 2026 is a sunday; 3pm CT slot resolution
sun_3pm = dt.datetime(2026, 6, 7, 15, 0, 0, tzinfo=CT)
check("slot at the exact fire instant resolves to this week",
	forum._digest_slot(sun_3pm) == sun_3pm,
	f"got {forum._digest_slot(sun_3pm)}")
check("slot a moment after the fire stays this week",
	forum._digest_slot(sun_3pm + dt.timedelta(seconds=30)) == sun_3pm)
check("slot on saturday resolves to the previous sunday",
	forum._digest_slot(dt.datetime(2026, 6, 6, 12, 0, 0, tzinfo=CT)) ==
	dt.datetime(2026, 5, 31, 15, 0, 0, tzinfo=CT))
check("slot on monday morning resolves to yesterday's sunday",
	forum._digest_slot(dt.datetime(2026, 6, 8, 9, 0, 0, tzinfo=CT)) == sun_3pm)

# ---- scheduled send records the ledger ----
shared.shared._quiet = False
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
set_digest(["intro line", "in-person section"])

asyncio.run(forum.send_weekly_announcements())
data = _ledger_data(slot_ms_for())
check("full send records both message ids",
	data is not None and data["message_ids"] == [9001, 9002], f"{data}")
check("full send is not marked forced", data and data["forced"] is False)
check("full send actually sent 2 messages", len(channel.sent) == 2)

# ---- partial failure records the hole ----
channel = FakeChannel(fail_indexes={1})
shared.shared.guild = FakeGuild(channel)
ledger_store.clear()
asyncio.run(forum.send_weekly_announcements())
data = _ledger_data(slot_ms_for())
check("failed send records id + null hole",
	data and data["message_ids"][0] is not None and data["message_ids"][1] is None,
	f"{data}")
check("failed send delivered only 1 message", len([m for m in channel.sent if m]) == 1)
partial_ids = data["message_ids"]

# ---- empty week is recorded as handled ----
set_digest([])
ledger_store.clear()
asyncio.run(forum.send_weekly_announcements())
data = _ledger_data(slot_ms_for())
check("empty week records an empty row", data is not None and data["message_ids"] == [], f"{data}")

# ---- quiet mode records nothing ----
shared.shared._quiet = True
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
ledger_store.clear()
asyncio.run(forum.send_weekly_announcements())
check("quiet mode sends nothing", channel.sent == [])
check("quiet mode records no ledger row", ledger_store == {})
shared.shared._quiet = False

# ---- reannounce: already sent -> no-op without force ----
set_digest(["intro line", "in-person section"])
ledger_store.clear()
ledger_store[slot_ms_for()] = {"data": json.dumps({
	"sent_at": "2026-06-07T21:00:05+00:00", "forced": False,
	"message_ids": [111, 222]})}
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
inter = make_interaction()
asyncio.run(forum.reannounce_digest(inter, "current", False))
check("reannounce on a complete ledger sends nothing", channel.sent == [])
check("reannounce replies 'already sent'", "already sent" in (reply_of(inter) or ""), reply_of(inter))

# ---- reannounce: completes exactly the holes ----
ledger_store.clear()
ledger_store[slot_ms_for()] = {"data": json.dumps({
	"sent_at": "2026-06-07T21:00:05+00:00", "forced": False,
	"message_ids": [partial_ids[0], None]})}
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
inter = make_interaction()
asyncio.run(forum.reannounce_digest(inter, "current", False))
check("reannounce sends only the missing message", len(channel.sent) == 1, str(channel.sent))
data = _ledger_data(slot_ms_for())
check("reannounce keeps the original id and fills the hole",
	data and data["message_ids"][0] == partial_ids[0] and data["message_ids"][1] is not None,
	f"{data}")
check("reannounce completion mentions the count", "1 of 2" in (reply_of(inter) or ""), reply_of(inter))
check("reannounce defers ephemeral", inter.response.deferred)

# ---- reannounce: force resends everything ----
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
inter = make_interaction()
asyncio.run(forum.reannounce_digest(inter, "current", True))
check("forced reannounce resends all messages", len(channel.sent) == 2, str(channel.sent))
data = _ledger_data(slot_ms_for())
check("forced reannounce marks forced", data and data["forced"] is True)
check("forced reannounce reply says forced", "forced" in (reply_of(inter) or "").lower(), reply_of(inter))

# ---- reannounce: never-run slot (missed run) -> full recovery send ----
ledger_store.clear()
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
inter = make_interaction()
asyncio.run(forum.reannounce_digest(inter, "current", False))
check("reannounce of a missed run sends all messages", len(channel.sent) == 2, str(channel.sent))
data = _ledger_data(slot_ms_for())
check("recovery run records both ids", data and data["message_ids"] == [channel.next_id - 1, channel.next_id], f"{data}")

# ---- reannounce: empty content week ----
ledger_store.clear()
set_digest([])
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
inter = make_interaction()
asyncio.run(forum.reannounce_digest(inter, "current", False))
check("reannounce of an empty week sends nothing", channel.sent == [])
data = _ledger_data(slot_ms_for())
check("reannounce of an empty week records it as handled", data and data["message_ids"] == [], f"{data}")
check("reannounce empty-week reply mentions no content", "no content" in (reply_of(inter) or ""), reply_of(inter))

# ---- reannounce: previous week targets a different slot ----
ledger_store.clear()
set_digest(["intro line", "in-person section"])
channel = FakeChannel()
shared.shared.guild = FakeGuild(channel)
inter = make_interaction()
asyncio.run(forum.reannounce_digest(inter, "previous", False))
check("previous-week reannounce sends (no record for that slot)", len(channel.sent) == 2)
data = _ledger_data(slot_ms_for("previous"))
check("previous-week reannounce records the previous slot", data is not None, f"{ledger_store.keys()}")
check("previous-week reannounce left this week's slot untouched", slot_ms_for() not in ledger_store)

# ---- startup gap check ----
ledger_store.clear()
buf = io.StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
	asyncio.run(forum.check_missed_digest())
finally:
	sys.stdout = old_stdout
check("gap check warns when the slot has no record", "WARNING: no send record" in buf.getvalue(), buf.getvalue())

ledger_store[slot_ms_for()] = {"data": json.dumps({"sent_at": "x", "forced": False, "message_ids": [1]})}
buf = io.StringIO()
sys.stdout = buf
try:
	asyncio.run(forum.check_missed_digest())
finally:
	sys.stdout = old_stdout
check("gap check stays quiet when the slot is recorded", "WARNING" not in buf.getvalue(), buf.getvalue())

# ---- permission gate ----
class FakePerms(types.SimpleNamespace):
	pass


def make_member(administrator=False, manage_guild=False):
	class FakeMember(discord.Member):
		def __init__(self):
			self._perms = FakePerms(administrator=administrator, manage_guild=manage_guild)
		@property
		def guild_permissions(self):
			return self._perms
		@guild_permissions.setter
		def guild_permissions(self, v):
			self._perms = v
	return types.SimpleNamespace(user=FakeMember())


plain = make_member()
check("gate rejects a plain member", forum._reannounce_admin(plain) is False)
check("gate accepts administrator", forum._reannounce_admin(make_member(administrator=True)) is True)
check("gate accepts manage_guild", forum._reannounce_admin(make_member(manage_guild=True)) is True)

# ---- command registration ----
admin_cmds = {c.name: c for c in forum.admin_group.commands}
check("reannounce registered under /rally admin", "reannounce" in admin_cmds, str(list(admin_cmds)))
check("reannounce has a week choice", {p.name for p in admin_cmds["reannounce"].parameters} >= {"week", "force"})

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)