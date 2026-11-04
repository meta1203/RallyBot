import os
import time
import datetime
import discord
from discord import app_commands
from aws import RallyBotModel
from shared import shared
from pynamodb.attributes import UnicodeAttribute, NumberAttribute
from pynamodb.exceptions import UpdateError

MODERATOR_MENTION = "<@&1225935511785570425>"
GENERAL_CHANNEL_ID = 1219601474661912668


def _is_moderator(interaction: discord.Interaction) -> bool:
	"""Moderators are members who can timeout or ban users (admins included)."""
	member = interaction.user
	if not isinstance(member, discord.Member):
		return False
	perms = member.guild_permissions
	return perms.administrator or perms.moderate_members or perms.ban_members


async def _reject_non_moderator(interaction: discord.Interaction):
	await interaction.response.send_message("You don't have permission to action reports.", ephemeral=True)

class Report(RallyBotModel):
	"""DynamoDB model for a user-submitted report."""
	timestamp = NumberAttribute(null=True)
	snowflake_id = NumberAttribute(null=True)
	reporter = NumberAttribute(null=True)
	comment = UnicodeAttribute(null=True)
	message = UnicodeAttribute(null=True)
	message_id = NumberAttribute(null=True)
	outcome = UnicodeAttribute(default="pending")

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = "report"


class ReportModal(discord.ui.Modal, title="Report Message"):
	comment: discord.ui.TextInput = discord.ui.TextInput(
		label="Why should this message be reported?",
		style=discord.TextStyle.paragraph,
		placeholder="Please describe why you are reporting this message...",
		required=True,
		max_length=1000,
	)

	def __init__(self, message: discord.Message, reporter: discord.User | discord.Member):
		super().__init__()
		self._message = message
		self._reporter = reporter

	async def on_submit(self, interaction: discord.Interaction):
		await submit_report(
			snowflake_id=self._message.author.id,
			reporter=self._reporter.id,
			comment=self.comment.value,
			message=self._message,
		)
		await interaction.response.send_message("Report submitted. Thanks!", ephemeral=True)


async def submit_report(
	snowflake_id: int,
	reporter: int,
	comment: str,
	message: discord.Message | None = None,
) -> Report:
	"""Submit a report to the database and notify moderators. Returns the saved Report object."""
	timestamp = int(time.time()*1000) # current time in milliseconds
	report = Report(sort=timestamp)
	report.timestamp = timestamp
	report.snowflake_id = snowflake_id
	report.reporter = reporter
	report.comment = comment
	if message:
		if message.content:
			report.message = message.content
		report.message_id = message.id
	report.outcome = "pending"
	report.save()

	# notify moderators
	mod_channel_name = os.getenv("MOD_CHANNEL", "moderator-only")
	embed = discord.Embed(
		title="New Report",
		color=discord.Color.orange(),
		timestamp=discord.utils.utcnow(),
	)
	embed.add_field(name="Reported User", value=f"<@{snowflake_id}> (`{snowflake_id}`)", inline=False)
	embed.add_field(name="Reported By", value=f"<@{reporter}> (`{reporter}`)", inline=False)
	if message:
		embed.add_field(name="Message", value=message.jump_url, inline=False)
	if message and message.content:
		embed.add_field(name="Message Content", value=message.content[:1024], inline=False)
	embed.add_field(name="Reason", value=comment, inline=False)
	embed.set_footer(text=f"Report ID: {timestamp}")

	view = ReportActionView(report_sort=timestamp, snowflake_id=snowflake_id)

	try:
		channel = await shared.get_channel_by_name(mod_channel_name)
		if channel:
			await channel.send(f"{MODERATOR_MENTION} Report received", embed=embed, view=view)
		else:
			print(f"ERROR: could not find mod channel '{mod_channel_name}' to send report notification.")
	except Exception as e:
		print(f"ERROR: failed to send report notification to mod channel: {e}")

	return report


def _count_actioned_reports(snowflake_id: int) -> int:
	"""Count the number of previously actioned reports for a given user."""
	try:
		return sum(
			1 for _ in Report.scan(
				index_name="snowflake_id-index",
				filter_condition=(Report.snowflake_id == snowflake_id) & (Report.outcome == "actioned"),
			)
		)
	except Exception as e:
		print(f"ERROR: failed to count actioned reports for {snowflake_id}: {e}")
		return 0


async def _finalize_report(
	interaction: discord.Interaction,
	report_sort: int,
	outcome: str,
	action_description: str,
):
	"""Update the report outcome, send a followup to the mod channel, and remove buttons from the original message."""
	# update the report in the database
	try:
		report = Report.get("report", report_sort)
		if report.outcome != "pending":
			await interaction.response.send_message("This report has already been actioned.", ephemeral=True)
			return
		report.update(actions=[Report.outcome.set(outcome)])
	except UpdateError:
		await interaction.response.send_message("Failed to update report in database.", ephemeral=True)
		return
	except Exception as e:
		print(f"ERROR: failed to update report {report_sort}: {e}")
		await interaction.response.send_message("Failed to update report in database.", ephemeral=True)
		return

	# remove buttons from the original message
	try:
		await interaction.response.edit_message(view=None)
	except Exception as e:
		print(f"ERROR: failed to edit original message: {e}")

	# send followup to the mod channel
	mod_channel_name = os.getenv("MOD_CHANNEL", "moderator-only")
	moderator = interaction.user
	followup_embed = discord.Embed(
		title="Report Actioned",
		color=discord.Color.green() if outcome == "actioned" else discord.Color.greyple(),
		timestamp=discord.utils.utcnow(),
	)
	followup_embed.add_field(name="Moderator", value=f"<@{moderator.id}> (`{moderator.id}`)", inline=False)
	followup_embed.add_field(name="Action", value=action_description, inline=False)
	followup_embed.add_field(name="Report ID", value=str(report_sort), inline=False)

	try:
		channel = await shared.get_channel_by_name(mod_channel_name)
		if channel:
			await channel.send(embed=followup_embed)
		else:
			print(f"ERROR: could not find mod channel '{mod_channel_name}' for followup.")
	except Exception as e:
		print(f"ERROR: failed to send followup to mod channel: {e}")


async def issue_warning(
	interaction: discord.Interaction,
	target_id: int,
	warning_message: str,
	report_sort: int | None = None,
) -> None:
	"""Send a warning notice to the target user and log it as a report record.

	Writes an actioned Report row so warnings appear in the running tally
	(see _count_actioned_reports), notifies the mod channel, and marks the
	linked report (if any) as actioned.
	"""
	moderator = interaction.user
	timestamp = int(time.time() * 1000)

	# if this warning came from a report, make sure it hasn't been actioned
	# already (mirrors BanConfirmModal's pre-check; prevents double-warnings
	# when two moderators act on the same report)
	if report_sort is not None:
		try:
			report = Report.get("report", report_sort)
			if report.outcome != "pending":
				await interaction.response.send_message("This report has already been actioned.", ephemeral=True)
				return
		except Exception as e:
			print(f"ERROR: failed to fetch report {report_sort} for warning: {e}")
			await interaction.response.send_message("Failed to fetch report from database.", ephemeral=True)
			return

	# log the warning to the database (as an actioned report record)
	try:
		warning_record = Report(sort=timestamp)
		warning_record.timestamp = timestamp
		warning_record.snowflake_id = target_id
		warning_record.reporter = moderator.id
		warning_record.comment = f"Warning issued by <@{moderator.id}>: {warning_message}"
		warning_record.message_id = report_sort  # link to the source report, if any
		warning_record.outcome = "actioned"
		warning_record.save()
	except Exception as e:
		print(f"ERROR: failed to save warning record for user {target_id}: {e}")
		await interaction.response.send_message("Failed to save the warning to the database. Warning not sent.", ephemeral=True)
		return

	# post the warning in #general with an @ mention of the warned user so
	# they are notified. (Discord has no way to send a truly ephemeral message
	# to another user - ephemeral responses only go to the interaction author -
	# so the notice is a normal channel message that mentions them.)
	warning_post = None
	warning_post_failed = False
	try:
		general = interaction.client.get_channel(GENERAL_CHANNEL_ID) or await interaction.client.fetch_channel(GENERAL_CHANNEL_ID)
		server_line = interaction.guild.name if interaction.guild else "the server"
		warning_post = await general.send(
			content=f"<@{target_id}>",
			embed=discord.Embed(
				title="⚠️ You have received a moderation warning",
				description=f"**Server:** {server_line}\n\n{warning_message}",
				color=discord.Color.gold(),
				timestamp=discord.utils.utcnow(),
			),
		)
	except discord.Forbidden:
		# bot lacks permission to post in #general; warning still counts
		warning_post_failed = True
		print(f"WARN: no permission to post warning in channel {GENERAL_CHANNEL_ID} for user {target_id}")
	except Exception as e:
		warning_post_failed = True
		print(f"ERROR: failed to post warning in channel {GENERAL_CHANNEL_ID} for user {target_id}: {e}")

	# mark the source report actioned and clear its buttons
	if report_sort is not None:
		try:
			report.update(actions=[Report.outcome.set("actioned")])
		except Exception as e:
			print(f"ERROR: failed to update report {report_sort} after warning: {e}")
		try:
			await interaction.response.edit_message(view=None)
		except Exception as e:
			print(f"ERROR: failed to edit original report message: {e}")
	else:
		await interaction.response.send_message("Warning issued.", ephemeral=True)

	# notify the mod channel
	mod_channel_name = os.getenv("MOD_CHANNEL", "moderator-only")
	followup_embed = discord.Embed(
		title="User Warned",
		color=discord.Color.gold(),
		timestamp=discord.utils.utcnow(),
	)
	followup_embed.add_field(name="Warned User", value=f"<@{target_id}> (`{target_id}`)", inline=False)
	followup_embed.add_field(name="Moderator", value=f"<@{moderator.id}> (`{moderator.id}`)", inline=False)
	followup_embed.add_field(name="Warning", value=warning_message[:1024], inline=False)
	if report_sort is not None:
		followup_embed.add_field(name="Report ID", value=str(report_sort), inline=False)
	if warning_post_failed:
		followup_embed.add_field(name="Notice", value="⚠️ Could not post the warning in #general - warning still logged.", inline=False)
	elif warning_post is not None:
		followup_embed.add_field(name="Posted", value=warning_post.jump_url, inline=False)

	try:
		channel = await shared.get_channel_by_name(mod_channel_name)
		if channel:
			await channel.send(embed=followup_embed)
		else:
			print(f"ERROR: could not find mod channel '{mod_channel_name}' for warning followup.")
	except Exception as e:
		print(f"ERROR: failed to send warning followup to mod channel: {e}")


class WarnModal(discord.ui.Modal, title="Warn User"):
	"""Modal for issuing a moderation warning.

	Contains a user select (who to warn) and a text field for the warning
	message. Both are required before the modal can be submitted.
	"""
	reason: discord.ui.TextInput = discord.ui.TextInput(
		label="Warning message",
		style=discord.TextStyle.paragraph,
		placeholder="Describe the rule that was broken and what happens if it continues...",
		required=True,
		max_length=1000,
	)

	def __init__(
		self,
		user_id: int | None = None,
		preset_message: str | None = None,
		report_sort: int | None = None,
	):
		super().__init__()
		self._report_sort = report_sort
		if preset_message:
			self.reason.default = preset_message
		self.user_select = discord.ui.UserSelect(
			placeholder="Select the user to warn...",
			min_values=1,
			max_values=1,
			required=True,
			default_values=[discord.Object(id=user_id, type=discord.User)] if user_id else None,
			custom_id="rallybot_warn_user_select",
		)
		self.add_item(self.user_select)

	async def on_submit(self, interaction: discord.Interaction):
		if not _is_moderator(interaction):
			await _reject_non_moderator(interaction)
			return
		selected = self.user_select.values
		if not selected:
			await interaction.response.send_message("No user was selected.", ephemeral=True)
			return
		target = selected[0]
		await issue_warning(
			interaction=interaction,
			target_id=target.id,
			warning_message=self.reason.value.strip(),
			report_sort=self._report_sort,
		)

	async def on_error(self, interaction: discord.Interaction, error: Exception, /):
		print(f"ERROR: exception in warn modal: {error}")
		if not interaction.response.is_done():
			await interaction.response.send_message("Something went wrong while issuing the warning.", ephemeral=True)


class BanConfirmModal(discord.ui.Modal, title="Confirm Ban"):
	confirm: discord.ui.TextInput = discord.ui.TextInput(
		label='Type "CONFIRM" to ban this user',
		style=discord.TextStyle.short,
		placeholder="CONFIRM",
		required=True,
		max_length=7,
	)

	def __init__(self, report_sort: int, snowflake_id: int, original_message: discord.Message):
		super().__init__()
		self._report_sort = report_sort
		self._snowflake_id = snowflake_id
		self._original_message = original_message

	async def on_submit(self, interaction: discord.Interaction):
		if self.confirm.value.strip().upper() != "CONFIRM":
			await interaction.response.send_message("Ban cancelled - confirmation text did not match.", ephemeral=True)
			return

		# check if report is still pending
		try:
			report = Report.get("report", self._report_sort)
			if report.outcome != "pending":
				await interaction.response.send_message("This report has already been actioned.", ephemeral=True)
				return
		except Exception as e:
			print(f"ERROR: failed to fetch report {self._report_sort}: {e}")
			await interaction.response.send_message("Failed to fetch report from database.", ephemeral=True)
			return

		# ban the user
		try:
			await shared.guild.ban(discord.Object(id=self._snowflake_id), reason=f"Banned via report {self._report_sort} by {interaction.user}")
		except discord.NotFound:
			await interaction.response.send_message("User not found.", ephemeral=True)
			return
		except discord.Forbidden:
			await interaction.response.send_message("I don't have permission to ban this user.", ephemeral=True)
			return
		except Exception as e:
			print(f"ERROR: failed to ban user {self._snowflake_id}: {e}")
			await interaction.response.send_message("Failed to ban user.", ephemeral=True)
			return

		# update report outcome
		try:
			report.update(actions=[Report.outcome.set("actioned")])
		except Exception as e:
			print(f"ERROR: failed to update report {self._report_sort}: {e}")

		# remove buttons from the original message
		try:
			await self._original_message.edit(view=None)
		except Exception as e:
			print(f"ERROR: failed to edit original message: {e}")

		# respond to the interaction
		await interaction.response.send_message("User has been banned.", ephemeral=True)

		# send followup to the mod channel
		mod_channel_name = os.getenv("MOD_CHANNEL", "moderator-only")
		moderator = interaction.user
		followup_embed = discord.Embed(
			title="Report Actioned",
			color=discord.Color.red(),
			timestamp=discord.utils.utcnow(),
		)
		followup_embed.add_field(name="Moderator", value=f"<@{moderator.id}> (`{moderator.id}`)", inline=False)
		followup_embed.add_field(name="Action", value=f"Banned <@{self._snowflake_id}> (`{self._snowflake_id}`)", inline=False)
		followup_embed.add_field(name="Report ID", value=str(self._report_sort), inline=False)

		try:
			channel = await shared.get_channel_by_name(mod_channel_name)
			if channel:
				await channel.send(embed=followup_embed)
		except Exception as e:
			print(f"ERROR: failed to send followup to mod channel: {e}")


class ReportActionView(discord.ui.View):
	def __init__(self, report_sort: int, snowflake_id: int):
		super().__init__(timeout=None)
		self._report_sort = report_sort
		self._snowflake_id = snowflake_id

	@discord.ui.button(label="Ignore", style=discord.ButtonStyle.secondary)
	async def ignore_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not _is_moderator(interaction):
			await _reject_non_moderator(interaction)
			return
		await _finalize_report(
			interaction=interaction,
			report_sort=self._report_sort,
			outcome="ignored",
			action_description=f"Ignored report for <@{self._snowflake_id}> (`{self._snowflake_id}`)",
		)

	@discord.ui.button(label="Warn", style=discord.ButtonStyle.secondary, row=0)
	async def warn_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not _is_moderator(interaction):
			await _reject_non_moderator(interaction)
			return
		# pre-populate the modal with the reported user and the report reason;
		# the moderator can edit the warning message before sending
		preset = None
		try:
			report = Report.get("report", self._report_sort)
			if report.comment:
				preset = report.comment
		except Exception as e:
			print(f"ERROR: failed to fetch report {self._report_sort} for warn preset: {e}")
		modal = WarnModal(
			user_id=self._snowflake_id,
			preset_message=preset,
			report_sort=self._report_sort,
		)
		await interaction.response.send_modal(modal)

	@discord.ui.button(label="Timeout", style=discord.ButtonStyle.primary)
	async def timeout_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not _is_moderator(interaction):
			await _reject_non_moderator(interaction)
			return
		# check if report is still pending
		try:
			report = Report.get("report", self._report_sort)
			if report.outcome != "pending":
				await interaction.response.send_message("This report has already been actioned.", ephemeral=True)
				return
		except Exception as e:
			print(f"ERROR: failed to fetch report {self._report_sort}: {e}")
			await interaction.response.send_message("Failed to fetch report from database.", ephemeral=True)
			return

		# calculate timeout duration: 2 * 2^n hours where n = previous actioned count
		n = _count_actioned_reports(self._snowflake_id)
		duration_hours = 2 * (2 ** n)
		max_hours = 28 * 24  # 28 days in hours (Discord max timeout)
		if duration_hours > max_hours:
			duration_hours = max_hours
		duration = datetime.timedelta(hours=duration_hours)

		# fetch member and apply timeout
		try:
			member = await shared.guild.fetch_member(self._snowflake_id)
			await member.timeout(duration, reason=f"Timed out via report {self._report_sort} by {interaction.user}")
		except discord.NotFound:
			await interaction.response.send_message("User is no longer in this server.", ephemeral=True)
			return
		except discord.Forbidden:
			await interaction.response.send_message("I don't have permission to timeout this user.", ephemeral=True)
			return
		except Exception as e:
			print(f"ERROR: failed to timeout user {self._snowflake_id}: {e}")
			await interaction.response.send_message("Failed to timeout user.", ephemeral=True)
			return

		await _finalize_report(
			interaction=interaction,
			report_sort=self._report_sort,
			outcome="actioned",
			action_description=f"Timed out <@{self._snowflake_id}> (`{self._snowflake_id}`) for {duration_hours} hours (n={n})",
		)

	@discord.ui.button(label="Ban", style=discord.ButtonStyle.danger)
	async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
		if not _is_moderator(interaction):
			await _reject_non_moderator(interaction)
			return
		modal = BanConfirmModal(
			report_sort=self._report_sort,
			snowflake_id=self._snowflake_id,
			original_message=interaction.message,
		)
		await interaction.response.send_modal(modal)


async def report_message(interaction: discord.Interaction, message: discord.Message):
	"""Context menu callback for the Report... message command."""
	await interaction.response.send_modal(ReportModal(message=message, reporter=interaction.user))


report_command = app_commands.ContextMenu(
	name="Report...",
	callback=report_message,
)

warn_group = app_commands.Group(
	name="rb",
	description="RallyBot moderator commands",
	default_permissions=discord.Permissions(
		administrator=True,
		moderate_members=True,
		ban_members=True,
	),
	guild_only=True,
)


@warn_group.command(name="warn", description="Issue a moderation warning to a user")
@app_commands.check(_is_moderator)
async def rb_warn(interaction: discord.Interaction):
	"""Open the warn modal (slash-command entry point)."""
	await interaction.response.send_modal(WarnModal())


@rb_warn.error
async def rb_warn_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
	"""Surface moderator-check failures to the user (otherwise they fail silently)."""
	if isinstance(error, app_commands.CheckFailure):
		if not interaction.response.is_done():
			await interaction.response.send_message("You don't have permission to warn users.", ephemeral=True)
		return
	print(f"ERROR: exception in /rb warn: {error}")
	if not interaction.response.is_done():
		await interaction.response.send_message("Something went wrong while opening the warn dialog.", ephemeral=True)


def setup(tree: app_commands.CommandTree, guild: discord.Guild | None = None):
	"""Register the report context menu and warn commands on the given command tree."""
	tree.add_command(report_command, guild=guild)
	tree.add_command(warn_group, guild=guild)