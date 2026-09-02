import time
import discord
from aws import RallyBotModel
from shared import shared
from pynamodb.attributes import NumberAttribute

# single-tenant hardcoded ids (see AGENTS.md): intro flow channels + role
RULES_CHANNEL_ID = 1223404781440208977
INTRO_CHANNEL_ID = 1219601598255464528
INTRO_ROLE_ID = 1475608039301054687
ACCEPT_BUTTON_ID = "rallybot:accept_rules"

RULES_MESSAGE = (
	"**Please read the rules above, then click the button below to agree.**\n"
	f"You won't be able to post in <#{INTRO_CHANNEL_ID}> until you do."
)


def _now_ms() -> int:
	return int(time.time() * 1000)


class Intro(RallyBotModel):
	"""Records the single intro post per user (partition 'intro', sort = user snowflake)."""
	message_id = NumberAttribute(null=True)
	timestamp = NumberAttribute(null=True)

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = "intro"


class Welcome(RallyBotModel):
	"""Pending welcome message in #intro (partition 'welcome', sort = user snowflake)."""
	message_id = NumberAttribute(null=True)

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = "welcome"


async def _get_intro_role(guild: discord.Guild) -> discord.Role | None:
	role = guild.get_role(INTRO_ROLE_ID)
	if role is None:
		try:
			role = await guild.fetch_role(INTRO_ROLE_ID)
		except Exception as e:
			print(f"ERROR: could not fetch intro role {INTRO_ROLE_ID}: {e}")
			return None
	return role


def _has_intro_record(user_id: int) -> bool:
	"""True if this user has already posted their one intro (source of truth: ddb)."""
	try:
		Intro.get("intro", user_id)
		return True
	except Intro.DoesNotExist:
		return False
	except Exception as e:
		print(f"ERROR: failed to look up intro record for {user_id}: {e}")
		return False


async def _notify_author(member: discord.Member, text: str) -> None:
	"""DM the author of a rejected/deleted intro post. Non-fatal on failure."""
	try:
		await member.send(text)
	except discord.Forbidden:
		print(f"WARN: couldn't DM {member.id} (DMs closed)")
	except Exception as e:
		print(f"ERROR: failed to DM {member.id}: {e}")


async def _delete_welcome(user_id: int) -> None:
	"""Delete the user's welcome message in #intro (auto-cleanup after accepting rules).

	The ddb row is deleted even if the discord-side deletion fails, so a stale
	row can't block future welcomes.
	"""
	try:
		welcome = Welcome.get("welcome", user_id)
	except Welcome.DoesNotExist:
		return
	except Exception as e:
		print(f"ERROR: failed to look up welcome record for {user_id}: {e}")
		return
	try:
		welcome.delete()
	except Exception as e:
		print(f"ERROR: failed to delete welcome record for {user_id}: {e}")
	if not welcome.message_id:
		return
	if shared.quiet:
		print(f"(quiet) would delete welcome message {welcome.message_id} for {user_id}")
		return
	try:
		channel = shared.client.get_channel(INTRO_CHANNEL_ID) or await shared.client.fetch_channel(INTRO_CHANNEL_ID)
		message = await channel.fetch_message(int(welcome.message_id))
		await message.delete()
	except discord.NotFound:
		pass  # already deleted, nothing to do
	except Exception as e:
		print(f"ERROR: failed to delete welcome message {welcome.message_id} for {user_id}: {e}")


async def handle_accept(interaction: discord.Interaction) -> None:
	"""Grant the intro role when a user clicks the rules-acceptance button."""
	guild = interaction.guild
	member = interaction.user
	if guild is None or not isinstance(member, discord.Member):
		await interaction.response.send_message("This button only works inside the server.", ephemeral=True)
		return
	role = await _get_intro_role(guild)
	if role is None:
		await interaction.response.send_message("Something went wrong (intro role missing) — please contact a moderator.", ephemeral=True)
		return
	if role in member.roles:
		# double click / role already present: clean up any stale welcome and confirm
		await _delete_welcome(member.id)
		await interaction.response.send_message(
			f"You've already agreed to the rules — go ahead and post your introduction in <#{INTRO_CHANNEL_ID}>!",
			ephemeral=True,
		)
		return
	if _has_intro_record(member.id):
		# rejoined member who already used their one intro: no role, no second intro
		await _delete_welcome(member.id)
		await interaction.response.send_message(
			"Welcome back! You've already posted your introduction, so the intro channel stays locked for you — everything else is open. Enjoy!",
			ephemeral=True,
		)
		return
	try:
		await member.add_roles(role, reason="Accepted the rules")
	except discord.Forbidden:
		print(f"ERROR: forbidden while adding intro role to {member.id} — is the bot's role above the intro role?")
		await interaction.response.send_message("I couldn't assign your intro role — please contact a moderator.", ephemeral=True)
		return
	except Exception as e:
		print(f"ERROR: failed to add intro role to {member.id}: {e}")
		await interaction.response.send_message("Something went wrong — please try again in a moment.", ephemeral=True)
		return
	await interaction.response.send_message(
		f"Thanks for agreeing to the rules! You can now post **one** introduction in <#{INTRO_CHANNEL_ID}> — "
		"the posting permission is removed automatically right after your post.",
		ephemeral=True,
	)
	# the welcome message has done its job once the rules are accepted
	await _delete_welcome(member.id)


class RulesAcceptView(discord.ui.View):
	"""Persistent view under the rules message; survives restarts via custom_id."""
	def __init__(self):
		super().__init__(timeout=None)

	@discord.ui.button(label="I have read and agree to follow the rules", style=discord.ButtonStyle.success, custom_id=ACCEPT_BUTTON_ID)
	async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		await handle_accept(interaction)


def _has_accept_button(message: discord.Message) -> bool:
	for row in message.components:
		children = getattr(row, "children", None)
		if children is None:
			continue
		for component in children:
			if getattr(component, "custom_id", None) == ACCEPT_BUTTON_ID:
				return True
	return False


async def ensure_rules_message(client: discord.Client, guild: discord.Guild) -> None:
	"""Make sure a rules-acceptance button message is pinned in #rules (idempotent)."""
	if shared.quiet:
		print("(quiet) skipping rules-acceptance message check")
		return
	try:
		channel = client.get_channel(RULES_CHANNEL_ID) or await client.fetch_channel(RULES_CHANNEL_ID)
	except Exception as e:
		print(f"ERROR: could not resolve rules channel {RULES_CHANNEL_ID}: {e}")
		return
	try:
		for message in await channel.pins():
			if message.author.id == client.user.id and _has_accept_button(message):
				return  # already in place
	except Exception as e:
		print(f"WARN: failed to check pinned messages in rules channel: {e}")
	try:
		message = await channel.send(RULES_MESSAGE, view=RulesAcceptView())
		await message.pin(reason="RallyBot: rules acceptance button")
		print(f"Posted rules acceptance message: {message.jump_url}")
	except Exception as e:
		print(f"ERROR: failed to post rules acceptance message: {e}")


async def ensure_intro_permissions(client: discord.Client, guild: discord.Guild) -> None:
	"""Lock #intro to the intro role: deny @everyone send, allow the intro role (idempotent)."""
	if shared.quiet:
		print("(quiet) skipping intro channel permission check")
		return
	try:
		channel = client.get_channel(INTRO_CHANNEL_ID) or await client.fetch_channel(INTRO_CHANNEL_ID)
	except Exception as e:
		print(f"ERROR: could not resolve intro channel {INTRO_CHANNEL_ID}: {e}")
		return
	try:
		everyone = channel.overwrites_for(guild.default_role)
		if everyone.send_messages is not False:
			everyone.send_messages = False
			await channel.set_permissions(guild.default_role, overwrite=everyone, reason="RallyBot: intro channel requires rules acceptance")
			print("Locked #intro: denied Send Messages for @everyone.")
	except Exception as e:
		print(f"ERROR: failed to set @everyone overwrite on intro channel: {e}")
	try:
		role = await _get_intro_role(guild)
		if role is None:
			return
		role_overwrite = channel.overwrites_for(role)
		if role_overwrite.send_messages is not True:
			role_overwrite.send_messages = True
			await channel.set_permissions(role, overwrite=role_overwrite, reason="RallyBot: intro role may post in #intro until their first post")
			print("Granted the intro role Send Messages in #intro.")
	except Exception as e:
		print(f"ERROR: failed to set intro role overwrite on intro channel: {e}")


async def on_member_join(member: discord.Member):
	"""Welcome the new member in #intro and remember the message for later cleanup."""
	if member.bot:
		return
	if _has_intro_record(member.id):
		# one intro per lifetime: no welcome needed for rejoining members
		print(f"{member} rejoined and has already introduced themselves; skipping welcome")
		return
	# remove a stale welcome from a previous join before sending a fresh one
	await _delete_welcome(member.id)
	if shared.quiet:
		print(f"(quiet) would send welcome message in #intro for {member.id}")
		return
	embed = discord.Embed(
		title="Welcome to the Chicago Anime Hangouts Discord!",
		color=discord.Color.blurple(),
		timestamp=discord.utils.utcnow(),
	)
	embed.add_field(
		name="1 · Read the rules",
		value=f"Head over to <#{RULES_CHANNEL_ID}>, read the rules, then click **\"I have read and agree to follow the rules\"** at the bottom of that channel.",
		inline=False,
	)
	embed.add_field(
		name="2 · Introduce yourself",
		value=f"After agreeing, post your introduction in <#{INTRO_CHANNEL_ID}>. You get one intro post — the permission to post there is removed automatically right after.",
		inline=False,
	)
	embed.add_field(
		name="That's it!",
		value="The rest of the server is open to you right away. Have fun!",
		inline=False,
	)
	embed.set_footer(text="You must accept the rules before you can post in this channel.")
	try:
		channel = member.guild.get_channel(INTRO_CHANNEL_ID) or await shared.client.fetch_channel(INTRO_CHANNEL_ID)
	except Exception as e:
		print(f"ERROR: could not resolve intro channel {INTRO_CHANNEL_ID} for welcome: {e}")
		return
	try:
		welcome_message = await channel.send(
			content=f"Welcome to the server, <@{member.id}>! 👋",
			embed=embed,
		)
	except discord.Forbidden:
		print(f"WARN: no permission to send welcome message in intro channel for {member.id}")
		return
	except Exception as e:
		print(f"ERROR: failed to send welcome message for {member.id}: {e}")
		return
	# discord-side send succeeded; now persist the pointer so the message can be
	# auto-deleted when they accept the rules (failure here is logged, not fatal)
	try:
		Welcome(sort=member.id, message_id=welcome_message.id).save()
	except Exception as e:
		print(f"ERROR: failed to save welcome record for {member.id}: {e}")
	print(f"Sent welcome message for {member.id} -> {welcome_message.id}")


async def handle_intro_message(message: discord.Message):
	"""Enforce the one-intro-post rule in #intro.

	The channel permission overwrite (intro role allowed, @everyone denied) is
	the primary gate; this guard backs it up and removes the role after the
	user's single intro post.
	"""
	if message.author.bot:
		return
	member = message.author
	if not isinstance(message.author, discord.Member):
		return
	# staff bypass: admins and moderators manage the space, they don't get
	# their messages deleted
	perms = message.author.guild_permissions
	if perms.administrator or perms.manage_messages or perms.moderate_members:
		return

	already_posted = _has_intro_record(message.author.id)
	if already_posted:
		# they already used their one intro: delete the repeat and explain
		try:
			await message.delete()
		except discord.NotFound:
			pass
		except Exception as e:
			print(f"ERROR: failed to delete repeat intro message {message.id} from {message.author.id}: {e}")
		await _notify_author(message.author, "You've already posted your introduction in the intro channel — only one intro per person, so this one was removed. If you'd like to share an update, feel free to post elsewhere in the server!")
		return

	# first (valid) intro: record it, then remove the intro role
	try:
		Intro(sort=message.author.id, message_id=message.id, timestamp=_now_ms()).save()
	except Exception as e:
		print(f"ERROR: failed to save intro record for {message.author.id}: {e}")
		# don't strip the role if we couldn't record the post — otherwise the
		# user could end up unable to post at all
		return

	guild = message.guild
	if guild:
		role = await _get_intro_role(message.guild)
		if role is not None and role in message.author.roles:
			try:
				await message.author.remove_roles(role, reason="Posted their intro; one-intro rule")
				print(f"Removed intro role from {message.author.id} after their intro post")
			except discord.Forbidden:
				print(f"ERROR: forbidden while removing intro role from {message.author.id} — is the bot's role above the intro role?")
			except Exception as e:
				print(f"ERROR: failed to remove intro role from {message.author.id}: {e}")
	print(f"Recorded intro post {message.id} from {message.author.id}; intro role removed.")


def setup(client: discord.Client, guild: discord.Guild) -> None:
	"""Register persistent UI listeners for the onboarding flow."""
	# persistent view: discord.py replays button clicks after restarts because
	# the custom_id is registered on the client
	client.add_view(RulesAcceptView())


async def startup_checks(client: discord.Client, guild: discord.Guild) -> None:
	"""Idempotent onboarding maintenance run once at startup."""
	await ensure_rules_message(client, guild)
	await ensure_intro_permissions(client, guild)
