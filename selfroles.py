"""Self-assignable roles: an ephemeral /roles picker backed by an admin-managed whitelist.

Design (agreed with the owner):
- Everything is namespaced under /rally (other bots flood the command list):
	/rally roles is the user picker; /rally selfrole add|remove|list is the
	admin whitelist.
- Admins whitelist roles with /rally selfrole add <role> <category> [exclusive]; the
	whitelist lives in ddb (partition id="selfrole", sort = role snowflake).
- Users pick roles with the ephemeral /rally roles command: one multi-select per
	category (max 25 options, max 5 categories per message). Selecting applies
	the diff immediately (add + remove) and the picker re-renders, so menus
	behave like live checkboxes.
- Exclusive categories (currently color roles): at most one role may be worn.
- A category that grows past 25 roles is truncated (first 25 by name) and the
	mod team is alerted in #mod-organizer.
- Rows whose role was deleted on Discord are healed (removed) whenever the
	picker is built.
"""
import asyncio
import datetime
import discord
from discord import app_commands
from aws import RallyBotModel
from shared import shared
from pynamodb.attributes import BooleanAttribute, NumberAttribute, UnicodeAttribute

MODERATOR_MENTION = "<@&1225935511785570425>"
MOD_ORGANIZER_CHANNEL = "mod-organizer"
MAX_OPTIONS_PER_SELECT = 25  # discord caps a select at 25 options
MAX_CATEGORIES = 5  # each select takes a full component row; a message has 5 rows
DEFAULT_CATEGORY = "misc"


class SelfRole(RallyBotModel):
	"""A whitelisted self-assignable role (id="selfrole", sort = role snowflake)."""
	category = UnicodeAttribute(null=True)
	exclusive = BooleanAttribute(null=True)
	added_by = NumberAttribute(null=True)

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = "selfrole"


def _query_selfroles() -> list[SelfRole]:
	return list(SelfRole.query("selfrole"))


async def load_selfroles() -> list[SelfRole]:
	"""All whitelisted roles. PynamoDB is blocking network I/O: worker thread."""
	return await asyncio.to_thread(_query_selfroles)


async def resolve_selfroles(guild: discord.Guild) -> tuple[dict[str, list[discord.Role]], set[str]]:
	"""Whitelist grouped by category, restricted to roles that still exist.

	Returns ({category: [roles sorted by name]}, {exclusive category names}).
	Rows pointing at deleted roles are deleted from ddb (self-healing) — the
	whitelist is a snapshot, Discord is the truth.
	"""
	rows = await load_selfroles()
	by_category: dict[str, list[discord.Role]] = {}
	exclusive: set[str] = set()
	dead_rows: list[SelfRole] = []
	for row in rows:
		role = guild.get_role(int(row.sort))
		if role is None:
			# role was deleted on discord; drop the stale whitelist entry
			dead_rows.append(row)
			continue
		category = (row.category or DEFAULT_CATEGORY).strip().lower()
		by_category.setdefault(category, []).append(role)
		if row.exclusive:
			exclusive.add(category)
	for row in dead_rows:
		print(f"self-assignable role {row.sort} no longer exists; removing from whitelist")
		await asyncio.to_thread(row.delete)
	for roles in by_category.values():
		roles.sort(key=lambda r: r.name.lower())
	return by_category, exclusive


class SelfRoleSelect(discord.ui.Select):
	"""One category's multi-select; the full selection (defaults included) is the desired state."""
	def __init__(self, category: str, roles: list[discord.Role], exclusive: bool, member_role_ids: set[int]):
		self.category = category
		self.exclusive = exclusive
		options = [
			discord.SelectOption(label=role.name[:100], value=str(role.id), default=role.id in member_role_ids)
			for role in roles
		]
		super().__init__(
			placeholder=f"{category} — pick one" if exclusive else category,
			min_values=0,
			max_values=len(options),
			options=options,
		)

	async def callback(self, interaction: discord.Interaction):
		view = self.view
		if view is not None:
			await view.apply_selection(interaction, self)


class SelfRolePicker(discord.ui.View):
	"""Ephemeral role picker; applies diffs on every selection, then re-renders."""
	def __init__(
		self,
		guild: discord.Guild,
		member: discord.Member,
		by_category: dict[str, list[discord.Role]],
		exclusive: set[str],
		member_role_ids: set[int],
	):
		super().__init__(timeout=300)
		self.guild = guild
		self.member = member
		self.by_category = by_category
		self.exclusive = exclusive
		self.member_role_ids = set(member_role_ids)
		for i, (category, roles) in enumerate(by_category.items()):
			if i >= MAX_CATEGORIES:
				break
			select = SelfRoleSelect(category, roles, category in exclusive, self.member_role_ids)
			select.row = i
			self.add_item(select)

	async def apply_selection(self, interaction: discord.Interaction, select: SelfRoleSelect):
		category_ids = {role.id for role in self.by_category.get(select.category, [])}
		selected = {int(v) for v in select.values}
		if select.exclusive and len(selected) > 1:
			await interaction.response.edit_message(
				content=f"⚠️ Only **one** {select.category} role can be worn at a time — pick a single role.",
				view=self,
			)
			return
		current_in_category = category_ids & self.member_role_ids
		to_add = [self.guild.get_role(rid) for rid in selected - current_in_category]
		to_remove = [self.guild.get_role(rid) for rid in current_in_category - selected]
		to_add = [role for role in to_add if role is not None]
		to_remove = [role for role in to_remove if role is not None]
		member = self.member
		if not isinstance(member, discord.Member):
			await interaction.response.edit_message(content="⚠️ Something went wrong — please run /roles again.", view=self)
			return
		errors: list[str] = []
		try:
			if to_add:
				await member.add_roles(*to_add, reason="Self-service role picker")
		except discord.Forbidden:
			errors.append("I don't have permission to assign one of those roles.")
		except discord.HTTPException as e:
			errors.append(f"Discord rejected the role update: {e}")
			print(f"ERROR: /roles add failed for {member}: {e}")
		try:
			if to_remove:
				await member.remove_roles(*to_remove, reason="Self-service role picker")
		except discord.Forbidden:
			errors.append("I don't have permission to remove one of those roles.")
		except discord.HTTPException as e:
			errors.append(f"Discord rejected the role update: {e}")
			print(f"ERROR: /roles remove failed for {member}: {e}")
		# track the new state locally instead of trusting the cache to have caught up
		self.member_role_ids = (self.member_role_ids - {r.id for r in to_remove}) | {r.id for r in to_add}
		refreshed = self._rebuild()
		if errors:
			await interaction.response.edit_message(content="⚠️ " + " ".join(errors), view=refreshed)
		else:
			await interaction.response.edit_message(content=self._content(), view=refreshed)

	def _rebuild(self) -> "SelfRolePicker":
		"""A fresh picker reflecting the member's (updated) roles."""
		return SelfRolePicker(self.guild, self.member, self.by_category, self.exclusive, self.member_role_ids)

	def _content(self) -> str:
		return "Your roles are up to date. Adjust the menus any time — changes apply as soon as you pick."


async def _warn_category_overflow(category: str, count: int):
	# shared.message_channel dedupes (5-message deque) and honors QUIET_RALLY
	await shared.message_channel(
		MOD_ORGANIZER_CHANNEL,
		f"{MODERATOR_MENTION} Self-assignable role category `{category}` now has {count} roles — "
		f"only the first {MAX_OPTIONS_PER_SELECT} fit in the /roles picker. Please trim the category.",
	)


def _categories_for_picker(by_category: dict[str, list[discord.Role]]) -> tuple[dict[str, list[discord.Role]], list[tuple[str, int]]]:
	"""Truncate oversized categories (first 25 by name) and flag the overflow for the mod alert."""
	picker_categories: dict[str, list[discord.Role]] = {}
	overflowed: list[tuple[str, int]] = []
	for category, roles in by_category.items():
		if len(roles) > MAX_OPTIONS_PER_SELECT:
			overflowed.append((category, len(roles)))
		picker_categories[category] = roles[:MAX_OPTIONS_PER_SELECT]
	return picker_categories, overflowed


# Discord allows exactly one level of subcommand nesting, so everything RallyBot
# offers lives under a single /rally group: /rally roles + /rally selfrole ...
rally_group = app_commands.Group(
	name="rally",
	description="RallyBot commands",
	guild_only=True,
)

selfrole_group = app_commands.Group(
	name="selfrole",
	description="Manage the self-assignable role whitelist",
	parent=rally_group,
	default_permissions=discord.Permissions(manage_roles=True),
)


@rally_group.command(name="roles", description="Pick your optional roles (colors, pronouns, games, ...)")
@app_commands.guild_only
async def roles_command(interaction: discord.Interaction):
	"""Ephemeral self-service role picker."""
	guild = interaction.guild
	if guild is None or not isinstance(interaction.user, discord.Member):
		await interaction.response.send_message("This command only works in the server.", ephemeral=True)
		return
	by_category, exclusive = await resolve_selfroles(guild)
	if not by_category:
		await interaction.response.send_message("There are no self-assignable roles configured yet.", ephemeral=True)
		return
	# the picker is capped by what fits on one message (5 selects)
	display_categories = dict(list(sorted(by_category.items()))[:MAX_CATEGORIES])
	if len(by_category) > MAX_CATEGORIES:
		await shared.message_channel(
			MOD_ORGANIZER_CHANNEL,
			f"{MODERATOR_MENTION} The /roles picker fits {MAX_CATEGORIES} categories, but {len(by_category)} are "
			f"configured — only the first {MAX_CATEGORIES} (alphabetically) are shown. Merge or trim categories.",
		)
	picker_categories, overflowed = _categories_for_picker(display_categories)
	for category, count in overflowed:
		await _warn_category_overflow(category, count)
	view = SelfRolePicker(guild, interaction.user, picker_categories, exclusive, {r.id for r in interaction.user.roles})
	await interaction.response.send_message(content=view._content(), view=view, ephemeral=True)


def _can_manage(interaction: discord.Interaction) -> bool:
	"""Admins and members with Manage Roles may edit the whitelist."""
	member = interaction.user
	if not isinstance(member, discord.Member):
		return False
	perms = member.guild_permissions
	return perms.administrator or perms.manage_roles


@selfrole_group.command(name="add", description="Whitelist a role users can give themselves via /rally roles")
@app_commands.check(_can_manage)
@app_commands.describe(
	role="The role to make self-assignable",
	category="Picker category, e.g. colors, pronouns, games, hobbies",
	exclusive="Only one role from this category can be worn at a time (e.g. color roles)",
)
async def selfrole_add(interaction: discord.Interaction, role: discord.Role, category: str, exclusive: bool = False):
	guild = interaction.guild
	if guild is None:
		await interaction.response.send_message("This command only works in the server.", ephemeral=True)
		return
	category = category.strip().lower()
	if not category:
		await interaction.response.send_message("Category can't be empty.", ephemeral=True)
		return
	if role.is_default():
		await interaction.response.send_message("You can't whitelist @everyone.", ephemeral=True)
		return
	if role.managed:
		await interaction.response.send_message("That role is managed by an integration and can't be self-assigned.", ephemeral=True)
		return
	if guild.me.top_role.position <= role.position:
		await interaction.response.send_message(
			f"My highest role must be above **{role.name}** for me to assign it — move my role up in Server Settings.",
			ephemeral=True,
		)
		return
	existing = [row for row in await load_selfroles() if row.sort == role.id]
	row = existing[0] if existing else SelfRole(sort=role.id)
	row.category = category
	row.exclusive = exclusive
	row.added_by = interaction.user.id
	await asyncio.to_thread(row.save)
	await interaction.response.send_message(
		f"**{role.name}** is now self-assignable under `{category}`" + (" (exclusive — one at a time)." if exclusive else "."),
		ephemeral=True,
	)
	count = len([r for r in await load_selfroles() if (r.category or DEFAULT_CATEGORY).strip().lower() == category])
	if count > MAX_OPTIONS_PER_SELECT:
		await _warn_category_overflow(category, count)


@selfrole_add.error
async def selfrole_add_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	"""Surface moderator-check failures (otherwise they fail silently)."""
	if isinstance(error, app_commands.CheckFailure):
		if not interaction.response.is_done():
			await interaction.response.send_message("You need the Manage Roles permission to edit the whitelist.", ephemeral=True)
		return
	print(f"ERROR: exception in /selfrole add: {error}")
	if not interaction.response.is_done():
		await interaction.response.send_message("Something went wrong while whitelisting the role.", ephemeral=True)


@selfrole_group.command(name="remove", description="Remove a role from the /roles picker")
@app_commands.check(_can_manage)
@app_commands.describe(role="The role to stop offering")
async def selfrole_remove(interaction: discord.Interaction, role: discord.Role):
	existing = [row for row in await load_selfroles() if row.sort == role.id]
	if not existing:
		await interaction.response.send_message(f"**{role.name}** isn't on the whitelist.", ephemeral=True)
		return
	await asyncio.to_thread(existing[0].delete)
	await interaction.response.send_message(f"**{role.name}** removed from the /roles picker.", ephemeral=True)


@selfrole_remove.error
async def selfrole_remove_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	if isinstance(error, app_commands.CheckFailure):
		if not interaction.response.is_done():
			await interaction.response.send_message("You need the Manage Roles permission to edit the whitelist.", ephemeral=True)
		return
	print(f"ERROR: exception in /selfrole remove: {error}")
	if not interaction.response.is_done():
		await interaction.response.send_message("Something went wrong while removing the role.", ephemeral=True)


@selfrole_group.command(name="list", description="Show the self-assignable role whitelist")
@app_commands.check(_can_manage)
async def selfrole_list(interaction: discord.Interaction):
	by_category, exclusive = await resolve_selfroles(interaction.guild)
	if not by_category:
		await interaction.response.send_message("The whitelist is empty — add roles with /selfrole add.", ephemeral=True)
		return
	lines = []
	for category, roles in sorted(by_category.items()):
		flag = " *(one at a time)*" if category in exclusive else ""
		mentions = " ".join(role.mention for role in roles)
		lines.append(f"**{category}**{flag}: {mentions}")
	await interaction.response.send_message("\n".join(lines), ephemeral=True)


@selfrole_list.error
async def selfrole_list_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	if isinstance(error, app_commands.CheckFailure):
		if not interaction.response.is_done():
			await interaction.response.send_message("You need the Manage Roles permission to view the whitelist.", ephemeral=True)
		return
	print(f"ERROR: exception in /selfrole list: {error}")
	if not interaction.response.is_done():
		await interaction.response.send_message("Something went wrong while listing the whitelist.", ephemeral=True)


def setup(tree: app_commands.CommandTree, guild: discord.Guild | None = None):
	"""Register the /rally command group (roles picker + selfrole admin) on the given tree."""
	tree.add_command(rally_group, guild=guild)
