"""Offline verification for selfroles.py (no live token, no real ddb, no AWS calls).

Covers: picker serialization (options/defaults/placeholders), the add/remove
diff on selection, exclusive-category rejection, dead-role healing, the
25-option truncation + mod alert, the admin permission gate, and command
registration.

Run inside the project venv: .venv/bin/python tests/tests_selfroles.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# quiet mode must be OFF for these tests (mod alerts are captured directly)
os.environ.pop("QUIET_RALLY", None)

import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed run)")

import selfroles

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


# ---- fake ddb store (stubbed before any model call; nothing hits AWS) ----
selfrole_store: list[dict] = []
selfrole_deletes: list[int] = []


def _fake_query(hash_key):
	rows = [
		selfroles.SelfRole(sort=r["sort"], category=r["category"], exclusive=r.get("exclusive"), added_by=r.get("added_by"))
		for r in selfrole_store
	]
	return iter(rows)


def _fake_save(self, *a, **kw):
	for r in selfrole_store:
		if r["sort"] == self.sort:
			r.update({"category": self.category, "exclusive": self.exclusive})
			return
	selfrole_store.append({"sort": self.sort, "category": self.category, "exclusive": self.exclusive})


def _fake_delete(self, *a, **kw):
	selfrole_deletes.append(int(self.sort))
	selfrole_store[:] = [r for r in selfrole_store if r["sort"] != self.sort]


selfroles.SelfRole.query = staticmethod(_fake_query)
selfroles.SelfRole.save = _fake_save
selfroles.SelfRole.delete = _fake_delete

# ---- fake discord objects ----
class FakeRole:
	def __init__(self, rid, name, position=0):
		self.id = rid
		self.name = name
		self.position = position
		self.mention = f"<@&{rid}>"
		self.managed = False

	def is_default(self):
		return self.id == 0


class FakeMember(discord.Member):
	"""Subclasses the real Member so isinstance() guards pass; shadows read-only
	properties with setters (expected Pyright noise on offline-test fakes)."""

	def __init__(self, role_ids, member_type="member"):
		self._roles = [FakeRole(rid, f"role{rid}") for rid in role_ids]
		self._perms = types.SimpleNamespace(administrator=False, manage_roles=False)
		self._id = 4242
		self._name = "tester"
		self.added: list[int] = []
		self.removed: list[int] = []
		self._type = member_type

	@property
	def id(self):
		return self._id

	@id.setter
	def id(self, v):
		self._id = v

	@property
	def name(self):
		return self._name

	@name.setter
	def name(self, v):
		self._name = v

	@property
	def roles(self):
		return self._roles

	@roles.setter
	def roles(self, v):
		self._roles = v

	@property
	def guild_permissions(self):
		return self._perms

	@guild_permissions.setter
	def guild_permissions(self, v):
		self._perms = v

	_x = types.SimpleNamespace(status=403, reason="Forbidden")

	async def add_roles(self, *roles, reason=None):
		if self._type == "forbidden":
			raise discord.Forbidden(self._x, "nope")
		self.added.extend(r.id for r in roles)

	async def remove_roles(self, *roles, reason=None):
		if self._type == "forbidden":
			raise discord.Forbidden(self._x, "nope")
		self.removed.extend(r.id for r in roles)


class FakeResponse:
	def __init__(self):
		self.edits: list[dict] = []
		self.sends: list[dict] = []
		self.done = False

	async def edit_message(self, **kwargs):
		self.edits.append(kwargs)

	async def send_message(self, *args, **kwargs):
		entry = dict(kwargs)
		if args:
			entry["content"] = args[0]
		self.sends.append(entry)


class FakeGuild:
	def __init__(self, roles):
		self._roles = {r.id: r for r in roles}

	def get_role(self, rid):
		return self._roles.get(rid)


mod_alerts: list[str] = []


async def _fake_message_channel(channel_name, message):
	mod_alerts.append((channel_name, message))
	return None


selfroles.shared.message_channel = _fake_message_channel


def make_interaction(member, guild):
	inter = types.SimpleNamespace()
	inter.user = member
	inter.guild = guild
	inter.response = FakeResponse()
	return inter


def reset_store():
	selfrole_store.clear()
	selfrole_deletes.clear()
	mod_alerts.clear()


# ================= 1. resolve_selfroles: grouping, sorting, dead-role healing =================
asyncio.get_event_loop().run_until_complete(asyncio.sleep(0))  # ensure a loop exists

reset_store()
selfrole_store.extend([
	{"sort": 2, "category": "colors", "exclusive": True},
	{"sort": 1, "category": "colors", "exclusive": False},
	{"sort": 3, "category": "games", "exclusive": False},
	{"sort": 999, "category": "ghosts", "exclusive": False},  # role deleted on discord
])
guild = FakeGuild([FakeRole(1, "Blue", 5), FakeRole(2, "Red", 6), FakeRole(3, "Game Night", 4)])
by_cat, exclusive = asyncio.run(selfroles.resolve_selfroles(guild))
check("dead role healed from ddb", selfrole_deletes == [999], f"deletes={selfrole_deletes}")
check("categories grouped", sorted(by_cat.keys()) == ["colors", "games"], f"{list(by_cat)}")
check("roles sorted by name", [r.name for r in by_cat["colors"]] == ["Blue", "Red"], f"{[r.name for r in by_cat['colors']]}")
check("exclusive flag propagates", exclusive == {"colors"}, f"{exclusive}")

# ================= 2. picker serialization =================
reset_store()
roles = [FakeRole(1, "Blue", 5), FakeRole(2, "Red", 6), FakeRole(3, "Green", 7)]
guild = FakeGuild(roles)
member = FakeMember([1])  # already wearing Blue
by_cat = {"colors": roles}
view = selfroles.SelfRolePicker(guild, member, by_cat, {"colors"}, {1})
payloads = [c.to_component_dict() for c in view.children]
check("one select rendered", len(payloads) == 1, f"{len(payloads)}")
sel = payloads[0]
check("placeholder marks exclusive", "pick one" in sel["placeholder"], sel["placeholder"])
check("max_values == option count", sel["max_values"] == 3, f"{sel['max_values']}")
defaults = [o["default"] for o in sel["options"]]
check("worn role is default-selected", defaults == [True, False, False], f"{defaults}")
check("option values are role ids", [o["value"] for o in sel["options"]] == ["1", "2", "3"], f"{sel['options'][0]['value']}")

# ================= 3. diff application on selection =================
select = view.children[0]
# discord.py 2.7.x: Select.values reads a per-interaction contextvar and falls
# back to the constructor-stored _values; the fallback is what a fake sets
select._values = ["2"]  # wants Red instead of Blue
inter = make_interaction(member, guild)
asyncio.run(view.apply_selection(inter, select))
check("worn role removed", member.removed == [1], f"{member.removed}")
check("new role added", member.added == [2], f"{member.added}")
check("no error content", "⚠️" not in (inter.response.edits[0]["content"] or ""), inter.response.edits[0]["content"])
refreshed = inter.response.edits[0]["view"]
check("view rebuilt with new defaults", [o["default"] for o in refreshed.children[0].to_component_dict()["options"]] == [False, True, False])

# ================= 4. exclusive category rejects multi-pick =================
reset_store()
member = FakeMember([])
view = selfroles.SelfRolePicker(guild, member, {"colors": roles}, {"colors"}, set())
select = view.children[0]
select._values = ["1", "2"]
inter = make_interaction(member, guild)
asyncio.run(view.apply_selection(inter, select))
check("exclusive multi-pick: nothing added", member.added == [], f"{member.added}")
check("exclusive multi-pick: nothing removed", member.removed == [], f"{member.removed}")
check("exclusive warning shown", "one" in inter.response.edits[0]["content"], inter.response.edits[0]["content"])

# ================= 5. non-exclusive category allows multiple =================
reset_store()
member = FakeMember([])
view = selfroles.SelfRolePicker(guild, member, {"games": roles}, set(), set())
select = view.children[0]
select._values = ["1", "2"]
inter = make_interaction(member, guild)
asyncio.run(view.apply_selection(inter, select))
check("non-exclusive multi-pick allowed", sorted(member.added) == [1, 2], f"{member.added}")

# ================= 6. discord Forbidden → warning content, partial state kept =================
reset_store()
member = FakeMember([], member_type="forbidden")
view = selfroles.SelfRolePicker(guild, member, {"colors": roles}, set(), set())
select = view.children[0]
select._values = ["2"]
inter = make_interaction(member, guild)
asyncio.run(view.apply_selection(inter, select))
check("forbidden surfaces a warning", "⚠️" in inter.response.edits[0]["content"], inter.response.edits[0]["content"])

# ================= 7. >25 roles in a category: truncate + mod alert =================
reset_store()
many = [FakeRole(100 + i, f"Color {i:02d}", 10 + i) for i in range(27)]
guild = FakeGuild(many)
picker_categories, overflowed = selfroles._categories_for_picker({"colors": many})
check("truncated to 25 options", len(picker_categories["colors"]) == 25, f"{len(picker_categories['colors'])}")
check("overflow flagged", overflowed == [("colors", 27)], f"{overflowed}")
view = selfroles.SelfRolePicker(guild, FakeMember([]), picker_categories, set(), set())
sel_payload = view.children[0].to_component_dict()
check("serialized select capped at 25", len(sel_payload["options"]) == 25, f"{len(sel_payload['options'])}")

# ================= 8. /roles command: empty whitelist, picker send =================
reset_store()
member = FakeMember([1, 2])
guild = FakeGuild([FakeRole(1, "Blue", 5), FakeRole(2, "Red", 6)])
inter = make_interaction(member, guild)
asyncio.run(selfroles.roles_command.callback(inter))
check("empty whitelist message", "no self-assignable roles" in inter.response.sends[0]["content"], inter.response.sends[0]["content"])
check("empty-whitelist reply is ephemeral", inter.response.sends[0]["ephemeral"] is True)

# now with roles whitelisted
guild = FakeGuild([FakeRole(1, "Blue", 5), FakeRole(2, "Red", 6), FakeRole(3, "Game Night", 4)])
selfrole_store.extend([
	{"sort": 1, "category": "colors", "exclusive": True},
	{"sort": 2, "category": "games", "exclusive": False},
])
inter = make_interaction(member, guild)
asyncio.run(selfroles.roles_command.callback(inter))
sent = inter.response.sends[0]
check("picker sent", sent.get("view") is not None, "no view")
check("picker is ephemeral", sent["ephemeral"] is True)
check("two category selects", len(sent["view"].children) == 2, f"{len(sent['view'].children)}")
check("select rows in order", [c.row for c in sent["view"].children] == [0, 1], f"{[c.row for c in sent['view'].children]}")

# ================= 9. admin gate =================
admin = FakeMember([])
admin.guild_permissions = types.SimpleNamespace(administrator=False, manage_roles=True)
plain = FakeMember([])
check("manage_roles passes", selfroles._can_manage(make_interaction(admin, guild)) is True)
check("plain member rejected", selfroles._can_manage(make_interaction(plain, guild)) is False)

# ================= 10. /selfrole add guards + save =================
reset_store()
admin.user = admin  # interaction.user is the member itself
everyone = FakeRole(0, "@everyone")
everyone_role = types.SimpleNamespace(id=0, name="@everyone", managed=False, is_default=lambda: True, position=0)
managed_role = types.SimpleNamespace(id=10, name="Booster", managed=True, is_default=lambda: False, position=20)
too_high = FakeRole(11, "Mod Role", 50)
guild.me = types.SimpleNamespace(top_role=FakeRole(99, "RallyBot", 30))
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_add.callback(inter, everyone_role, "colors"))
check("@everyone rejected", "can't whitelist @everyone" in inter.response.sends[0]["content"])
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_add.callback(inter, managed_role, "colors"))
check("managed role rejected", "managed by an integration" in inter.response.sends[0]["content"])
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_add.callback(inter, too_high, "colors"))
check("role above bot rejected", "highest role must be above" in inter.response.sends[0]["content"])
check("nothing saved on rejections", selfrole_store == [], f"{selfrole_store}")

ok_role = FakeRole(2, "Red", 6)
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_add.callback(inter, ok_role, "Colors", exclusive=True))
check("valid role saved", any(r["sort"] == 2 for r in selfrole_store), f"{selfrole_store}")
check("category normalized to lowercase", selfrole_store[0]["category"] == "colors", selfrole_store[0]["category"])
check("exclusive flag saved", selfrole_store[0]["exclusive"] is True)
check("admin confirm mentions role", "Red" in inter.response.sends[0]["content"])

# re-add overwrites instead of duplicating
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_add.callback(inter, ok_role, "pronouns"))
check("re-add updates in place", len(selfrole_store) == 1, f"{len(selfrole_store)}")
check("category updated on re-add", selfrole_store[0]["category"] == "pronouns", selfrole_store[0]["category"])

# ================= 11. /selfrole remove + list =================
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_remove.callback(inter, FakeRole(2, "Red")))
check("removed from whitelist", selfrole_store == [], f"{selfrole_store}")
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_remove.callback(inter, FakeRole(2, "Red")))
check("removing absent role reports it", "isn't on the whitelist" in inter.response.sends[0]["content"])

selfrole_store.extend([
	{"sort": 1, "category": "colors", "exclusive": True},
	{"sort": 3, "category": "games", "exclusive": False},
])
inter = make_interaction(admin, guild)
asyncio.run(selfroles.selfrole_list.callback(inter))
listing = inter.response.sends[0]["content"]
check("list shows both categories", "colors" in listing and "games" in listing, listing)
check("list marks exclusive category", "one at a time" in listing, listing)

# ================= 12. setup() registers the /rally group =================
registered = []


class FakeTree:
	def add_command(self, cmd, guild=None):
		registered.append(cmd)


selfroles.setup(FakeTree())
check("setup registers exactly the /rally group", registered == [selfroles.rally_group], f"{[getattr(c, 'name', None) for c in registered]}")
check("rally group is /rally", selfroles.rally_group.name == "rally")
import report  # binds /rally warn into the shared group at import time
walked = {c.name for c in selfroles.rally_group.walk_commands()}
# walk_commands yields the nested group object itself alongside its subcommands
# ('admin' is the admin-utilities subgroup from forum.py: /rally admin reannounce)
check("rally contains full command tree", walked == {"roles", "selfrole", "add", "remove", "list", "warn", "admin"}, f"{walked}")
check("selfrole subgroup parented under rally", selfroles.selfrole_group.parent is selfroles.rally_group)
# Group.command returns the raw function; the Command object lives in the group
warn_cmd = next(c for c in selfroles.rally_group.commands if getattr(c, "name", None) == "warn")
check("warn rides under /rally", warn_cmd.parent is selfroles.rally_group)

# ================= 13. overflow alert reaches #mod-organizer =================
reset_store()
selfrole_store.extend([{"sort": 100 + i, "category": "colors", "exclusive": False} for i in range(27)])
many = [FakeRole(100 + i, f"Color {i:02d}", 10 + i) for i in range(27)]
guild = FakeGuild(many)
member = FakeMember([100])
inter = make_interaction(member, guild)
asyncio.run(selfroles.roles_command.callback(inter))
check("overflow mod alert sent", any("colors" in msg and "27" in msg for _, msg in mod_alerts), f"{mod_alerts}")
check("mod alert goes to #mod-organizer", all(ch == selfroles.MOD_ORGANIZER_CHANNEL for ch, _ in mod_alerts), f"{mod_alerts}")
check("mod alert mentions moderator role", all(selfroles.MODERATOR_MENTION in msg for _, msg in mod_alerts), f"{mod_alerts}")
sent = inter.response.sends[0]
check("oversized picker still renders 25", len(sent["view"].children[0].to_component_dict()["options"]) == 25)

# ================= 14. timeout cleanup deletes the ephemeral reply =================
reset_store()
roles = [FakeRole(1, "Blue", 5), FakeRole(2, "Red", 6)]
guild = FakeGuild(roles)
member = FakeMember([1])

def make_origin():
	orig = make_interaction(member, guild)
	orig.deleted = []
	async def delete_original_response():
		orig.deleted.append(True)
	orig.delete_original_response = delete_original_response
	return orig

# a live picker times out -> message deleted
origin = make_origin()
view = selfroles.SelfRolePicker(guild, member, {"colors": roles}, set(), {1}, origin=origin)
asyncio.run(view.on_timeout())
check("timeout deletes the ephemeral message", len(origin.deleted) == 1, f"{origin.deleted}")

# a superseded picker's timeout must NOT delete its replacement's message
origin = make_origin()
view = selfroles.SelfRolePicker(guild, member, {"colors": roles}, set(), {1}, origin=origin)
select = view.children[0]
select._values = ["2"]
inter = make_interaction(member, guild)
asyncio.run(view.apply_selection(inter, select))  # re-renders -> new view in the chain
asyncio.run(view.on_timeout())  # old view expires late
check("superseded picker leaves message alone", len(origin.deleted) == 0, f"{origin.deleted}")
refreshed = inter.response.edits[0]["view"]
asyncio.run(refreshed.on_timeout())
check("current picker's timeout deletes", len(origin.deleted) == 1, f"{origin.deleted}")

# user already dismissed the message -> delete raises, swallowed
origin = make_origin()
async def boom():
	raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
origin.delete_original_response = boom
view = selfroles.SelfRolePicker(guild, member, {"colors": roles}, set(), {1}, origin=origin)
asyncio.run(view.on_timeout())
check("already-dismissed message: swallow NotFound", True)  # reaching here = no exception escaped

# pickers without an origin interaction (tests/edge) just do nothing
view = selfroles.SelfRolePicker(guild, member, {"colors": roles}, set(), {1})
asyncio.run(view.on_timeout())
check("origin-less picker no-ops on timeout", True)

# ================= done =================
print(f"\n{'ALL PASSED' if FAIL == 0 else 'FAILURES'}: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
