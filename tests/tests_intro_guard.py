"""Offline verification for the intro-record guards (no live token, no real ddb).

Regression tests for the false "You've already posted your introduction"
report: Discord posts a "<user> joined the server" system message in #intro
AUTHORED BY the joining member, and every non-staff message in #intro used
to be recorded as that user's one intro — instantly locking them out before
they ever typed anything. Also covers tombstone healing (a recorded intro
whose message was deleted must not lock the user out forever) and guards
for the previously-correct behaviors.

Run inside the project venv: .venv/bin/python tests/tests_intro_guard.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# quiet mode must be OFF for these tests (welcome sends happen)
os.environ.pop("QUIET_RALLY", None)

import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed run)")

import onboarding

# ---- fake ddb stores (stubbed before any model call; nothing hits AWS) ----
intro_store: dict[int, dict] = {}
welcome_store: dict[int, dict] = {}
intro_saves: list[dict] = []


def _fake_intro_get(id, sort):
	if sort not in intro_store:
		raise onboarding.Intro.DoesNotExist()
	row = intro_store[sort]
	return onboarding.Intro(sort=sort, message_id=row.get("message_id"), timestamp=row.get("timestamp"))


def _fake_intro_save(self, *a, **kw):
	intro_saves.append({"user": self.sort, "message_id": self.message_id})
	intro_store[self.sort] = {"message_id": self.message_id, "timestamp": self.timestamp}


def _fake_intro_delete(self, *a, **kw):
	return intro_store.pop(self.sort, None)


def _fake_welcome_get(id, sort):
	if sort not in welcome_store:
		raise onboarding.Welcome.DoesNotExist()
	row = welcome_store[sort]
	return onboarding.Welcome(sort=sort, message_id=row.get("message_id"))


def _fake_welcome_save(self, *a, **kw):
	welcome_store[self.sort] = {"message_id": self.message_id}


def _fake_welcome_delete(self, *a, **kw):
	return welcome_store.pop(self.sort, None)


onboarding.Intro.get = _fake_intro_get
onboarding.Intro.save = _fake_intro_save
onboarding.Intro.delete = _fake_intro_delete
onboarding.Welcome.get = _fake_welcome_get
onboarding.Welcome.save = _fake_welcome_save
onboarding.Welcome.delete = _fake_welcome_delete

# ---- fake discord objects ----
# Subclass the real types and class-shadow read-only properties so isinstance
# checks in onboarding.py pass; instances get a __dict__ because the subclass
# declares no __slots__.


class FakePerms:
	administrator = False
	manage_messages = False
	moderate_members = False


class FakeMember(discord.Member):
	id = 0
	bot = False
	guild = None
	guild_permissions = None
	roles = ()

	def __init__(self, id):
		self.id = id
		self.roles = []
		self.guild_permissions = FakePerms()
		self.sent_dms = []
		self.added_roles = []
		self.removed_roles = []

	def __str__(self):
		# discord.py's Member.__str__ reaches into self._user; keep f-strings happy
		return f"FakeMember#{self.id}"

	async def send(self, content=None, **kw):
		self.sent_dms.append(content)

	async def add_roles(self, *roles, **kw):
		self.added_roles.extend(roles)

	async def remove_roles(self, *roles, **kw):
		self.removed_roles.extend(roles)


class FakeMessage(discord.Message):
	id = 0
	type = None
	author = None
	guild = None

	def __init__(self, id, type, author, guild):
		self.id = id
		self.type = type
		self.author = author
		self.guild = guild
		self.deleted = False

	async def delete(self, *a, **kw):
		self.deleted = True


class FakeRole:
	def __init__(self, id, name):
		self.id = id
		self.name = name


class FakeIntroChannel:
	def __init__(self):
		self.sent = []
		self.messages: dict[int, FakeMessage] = {}
		self.fail_fetch_with: Exception | None = None

	async def send(self, content=None, **kw):
		self.sent.append({"content": content, "embed": kw.get("embed")})
		return types.SimpleNamespace(id=555000000000000001, content=content)

	async def fetch_message(self, id):
		if self.fail_fetch_with is not None:
			raise self.fail_fetch_with
		msg = self.messages.get(id)
		if msg is None:
			raise discord.NotFound(
				types.SimpleNamespace(status=404, reason="Not Found", text='{"message": "Unknown Message"}', headers={}),
				"Unknown Message",
			)
		return msg


class FakeGuild:
	id = 1219601473948614737

	def __init__(self):
		self._intro = FakeIntroChannel()
		self._role = FakeRole(onboarding.INTRO_ROLE_ID, "introductions")

	def get_role(self, role_id):
		return self._role if role_id == onboarding.INTRO_ROLE_ID else None

	async def fetch_role(self, role_id):
		return self.get_role(role_id)

	def get_channel(self, channel_id):
		return self._intro if channel_id == onboarding.INTRO_CHANNEL_ID else None

	def get_channel_or_thread(self, channel_id):
		return self.get_channel(channel_id)

	async def fetch_channel(self, channel_id):
		return self.get_channel(channel_id)


class FakeResponse:
	def __init__(self):
		self.sent = []

	async def send_message(self, content=None, **kw):
		self.sent.append({"content": content, **kw})


class FakeInteraction:
	def __init__(self, member, guild):
		self.user = member
		self.guild = guild
		self.response = FakeResponse()


# ---- scenario helpers ----
UID = 111222333444555666
WELCOME_MSG_ID = 777000000000000001


def reset():
	intro_store.clear()
	welcome_store.clear()
	intro_saves.clear()
	guild = FakeGuild()
	member = FakeMember(UID)
	member.guild = guild
	return guild, member


def put_intro_row(message_id, timestamp=1700000000000):
	intro_store[UID] = {"message_id": message_id, "timestamp": timestamp}


def make_system_message(guild, member):
	"""A Discord '<user> joined the server' notice in #intro (author = joiner)."""
	return FakeMessage(888000000000000001, discord.MessageType.new_member, member, guild)


def make_real_message(guild, member, msg_id=999000000000000001):
	"""A genuine user post (type default) in #intro."""
	return FakeMessage(msg_id, discord.MessageType.default, member, guild)


# ---- the regression tests ----

def test_system_message_does_not_consume_intro():
	"""ROOT CAUSE: a join-system message in #intro must NOT record an intro row."""
	guild, member = reset()
	msg = make_system_message(guild, member)
	asyncio.run(onboarding.handle_intro_message(msg))
	assert intro_store == {}, f"system message recorded an intro row: {intro_store}"
	assert not msg.deleted, "system message should be left alone"
	assert member.sent_dms == [], "system message must not trigger the repeat-intro DM"
	print("PASS: join-system message does not consume the intro")


def test_system_message_with_existing_valid_record_ignored():
	"""A rejoining member's new join notice must not delete their valid intro row."""
	guild, member = reset()
	put_intro_row(999000000000000001)
	# their original (real) intro is still in the channel
	guild._intro.messages[999000000000000001] = make_real_message(guild, member, 999000000000000001)
	msg = make_system_message(guild, member)
	asyncio.run(onboarding.handle_intro_message(msg))
	assert UID in intro_store, "valid intro row was wiped by a system message"
	assert not msg.deleted
	assert member.sent_dms == []
	print("PASS: join-system message leaves a valid intro record alone")


def test_accept_when_record_points_at_system_message_heals():
	"""THE REPORTER'S CASE: row exists but the 'intro' was a join-system message.

	Accepting the rules must heal (delete the bogus row), grant the role, and
	give the normal success message instead of 'already posted'.
	"""
	guild, member = reset()
	put_intro_row(888000000000000001)
	# the system message is still sitting in #intro
	guild._intro.messages[888000000000000001] = make_system_message(guild, member)
	interaction = FakeInteraction(member, guild)
	asyncio.run(onboarding.handle_accept(interaction))
	assert UID not in intro_store, "stale row pointing at a system message was not cleared"
	assert member.added_roles, "intro role was not granted"
	content = interaction.response.sent[-1]["content"]
	assert "Thanks for agreeing" in content, f"wrong response: {content!r}"
	assert "already posted" not in content
	print("PASS: tombstoned-by-system-message record heals on accept")


def test_accept_when_record_points_at_deleted_message_heals():
	"""Row exists but the recorded message was deleted: heal, grant, succeed."""
	guild, member = reset()
	put_intro_row(999000000000000001)  # nothing in the channel: fetch 404s
	interaction = FakeInteraction(member, guild)
	asyncio.run(onboarding.handle_accept(interaction))
	assert UID not in intro_store, "stale row pointing at a deleted message was not cleared"
	assert member.added_roles, "intro role was not granted"
	content = interaction.response.sent[-1]["content"]
	assert "Thanks for agreeing" in content, f"wrong response: {content!r}"
	print("PASS: tombstoned-by-deletion record heals on accept")


def test_accept_with_valid_record_still_locked():
	"""A genuine prior intro (message still in the channel) still locks #intro."""
	guild, member = reset()
	put_intro_row(999000000000000001)
	guild._intro.messages[999000000000000001] = make_real_message(guild, member, 999000000000000001)
	interaction = FakeInteraction(member, guild)
	asyncio.run(onboarding.handle_accept(interaction))
	assert UID in intro_store, "valid record must be kept"
	assert not member.added_roles, "intro role must NOT be granted"
	content = interaction.response.sent[-1]["content"]
	assert "already posted" in content, f"wrong response: {content!r}"
	print("PASS: valid prior intro still blocks a second one")


def test_accept_fresh_grants_role():
	"""No record at all: the normal first-time flow (guard against regressions)."""
	guild, member = reset()
	interaction = FakeInteraction(member, guild)
	asyncio.run(onboarding.handle_accept(interaction))
	assert member.added_roles, "intro role was not granted"
	content = interaction.response.sent[-1]["content"]
	assert "Thanks for agreeing" in content, f"wrong response: {content!r}"
	assert intro_store == {}, "fresh accept must not create an intro row"
	print("PASS: fresh accept grants the role")


def test_transient_verification_failure_keeps_record():
	"""If Discord can't tell us whether the message exists, stay locked."""
	guild, member = reset()
	put_intro_row(999000000000000001)
	guild._intro.fail_fetch_with = RuntimeError("transient outage")
	interaction = FakeInteraction(member, guild)
	asyncio.run(onboarding.handle_accept(interaction))
	assert UID in intro_store, "transient failure must not wipe the record"
	assert not member.added_roles
	content = interaction.response.sent[-1]["content"]
	assert "already posted" in content, f"wrong response: {content!r}"
	print("PASS: transient verification failure keeps the record (fail closed)")


def test_real_intro_records_and_strips_role():
	"""A genuine first post records the row and removes the intro role (guard)."""
	guild, member = reset()
	member.roles = [guild._role]
	msg = make_real_message(guild, member)
	asyncio.run(onboarding.handle_intro_message(msg))
	assert UID in intro_store, "real intro was not recorded"
	assert intro_store[UID]["message_id"] == msg.id
	assert guild._role in member.removed_roles, "intro role was not removed"
	assert not msg.deleted
	print("PASS: real intro is recorded and the role stripped")


def test_repeat_intro_deleted_with_dm():
	"""A second real post is deleted and the author DM'd (guard)."""
	guild, member = reset()
	put_intro_row(999000000000000001)
	guild._intro.messages[999000000000000001] = make_real_message(guild, member, 999000000000000001)
	msg = make_real_message(guild, member, 999000000000000002)
	asyncio.run(onboarding.handle_intro_message(msg))
	assert msg.deleted, "repeat intro was not deleted"
	assert member.sent_dms and "already posted" in member.sent_dms[0]
	assert UID in intro_store, "valid record must survive a repeat attempt"
	print("PASS: repeat intro is deleted with an explanatory DM")


def test_join_with_system_message_tombstone_gets_welcome():
	"""A rejoining member whose row is a system-message tombstone gets a welcome."""
	guild, member = reset()
	put_intro_row(888000000000000001)
	guild._intro.messages[888000000000000001] = make_system_message(guild, member)
	asyncio.run(onboarding.on_member_join(member))
	assert guild._intro.sent, "welcome was skipped for a tombstoned record"
	assert UID in welcome_store, "welcome pointer was not saved"
	assert UID not in intro_store, "tombstone row was not cleared on join"
	print("PASS: tombstoned rejoin gets a fresh welcome")


def test_join_with_valid_record_skips_welcome():
	"""A rejoining member with a genuine intro gets no welcome (one per lifetime)."""
	guild, member = reset()
	put_intro_row(999000000000000001)
	guild._intro.messages[999000000000000001] = make_real_message(guild, member, 999000000000000001)
	asyncio.run(onboarding.on_member_join(member))
	assert not guild._intro.sent, "welcome sent despite a valid intro record"
	print("PASS: valid prior intro still suppresses the welcome")


# ---- runner ----
if __name__ == "__main__":
	tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
	failed = []
	for name, fn in tests:
		try:
			fn()
		except Exception as e:
			failed.append((name, e))
			print(f"FAIL: {name}: {type(e).__name__}: {e}")
	if failed:
		print(f"\n{len(failed)}/{len(tests)} FAILED")
		raise SystemExit(1)
	print(f"\nALL {len(tests)} INTRO-GUARD TESTS PASSED")
