import discord
import events
import aws

intents = discord.Intents.default()
# required intents for the bot to function
intents.guild_scheduled_events = True

client = discord.Client(intents=intents)
guild: discord.Guild = None

@client.event
async def on_ready():
	global guild
	print(f'We have logged in as {client.user}')
	guild = client.get_guild("the_guild_id")
	update_events()

# client.run(os.getenv('DISCORD_TOKEN'))

ddb = aws.DynamoDBClient()

async def update_events():
	print(guild.scheduled_events)
	on_meetup = events.fetch_meetup_events()
	for e in on_meetup:
		table_item = ddb.read_item(e.id, e.sort)
		if not table_item:
			ddb.write_item(e)
			table_item = ddb.read_item(e.id, e.sort)
