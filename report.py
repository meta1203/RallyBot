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
			await interaction.response.send_message("Ban cancelled — confirmation text did not match.", ephemeral=True)
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
		await _finalize_report(
			interaction=interaction,
			report_sort=self._report_sort,
			outcome="ignored",
			action_description=f"Ignored report for <@{self._snowflake_id}> (`{self._snowflake_id}`)",
		)

	@discord.ui.button(label="Timeout", style=discord.ButtonStyle.primary)
	async def timeout_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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


def setup(tree: app_commands.CommandTree, guild: discord.Guild | None = None):
	"""Register the report context menu command on the given command tree."""
	tree.add_command(report_command, guild=guild)