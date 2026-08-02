import os
import time
import discord
from discord import app_commands
from aws import RallyBotModel
from shared import shared
from pynamodb.attributes import UnicodeAttribute, NumberAttribute

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
	embed.add_field(name="Comment", value=comment, inline=False)
	if message and message.content:
		embed.add_field(name="Message Content", value=message.content[:1024], inline=False)
	embed.set_footer(text=f"Report ID: {timestamp}")

	try:
		channel = await shared.get_channel_by_name(mod_channel_name)
		if channel:
			await channel.send(f"{MODERATOR_MENTION} New Report", embed=embed)
		else:
			print(f"ERROR: could not find mod channel '{mod_channel_name}' to send report notification.")
	except Exception as e:
		print(f"ERROR: failed to send report notification to mod channel: {e}")

	return report


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