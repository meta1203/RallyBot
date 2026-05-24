import discord
from aws import TableItem
from shared import shared

@shared.client.event
async def on_message(message: discord.Message):
	# remove user's permission to send messages in the intro channel
	if message.channel.name == "intro":
		message.author.id
		message.author.remove_roles()

class DiscordUser(TableItem):
	def __init__(self, snowflake_id):
		self.id = "user"
		self.sort = snowflake_id