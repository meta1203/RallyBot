import discord

import events
import aws

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import os
import asyncio
import datetime

intents = discord.Intents.default()
# required intents for the bot to function
intents.guild_scheduled_events = True

ddb: aws.DynamoDBClient = None
client = discord.Client(intents=intents)
guild: discord.Guild = None
loop: asyncio.AbstractEventLoop = None
scheduler: AsyncIOScheduler = None

async def set_globals():
	global ddb, loop, guild, scheduler
	print("setting globals...")
	ddb = aws.DynamoDBClient()
	loop = asyncio.get_running_loop()
	scheduler = AsyncIOScheduler(gconfig={'event_loop': loop})
	guild = await client.fetch_guild("1219601473948614737")

async def update_events():
	on_meetup = events.fetch_meetup_events()
	for e in on_meetup:
		table_item: events.MeetupEvent = ddb.read_item(e.id, e.sort)
		if not table_item:
			ddb.write_item(e)
			table_item = ddb.read_item(e.id, e.sort)
		discord_event: (discord.ScheduledEvent | None) = None
		if table_item.snowflake_id:
			print(f"reading in snowflake id: {table_item.snowflake_id}")
			discord_event = await guild.fetch_scheduled_event(table_item.snowflake_id)
			e.snowflake_id = table_item.snowflake_id
			table_item = e
		if discord_event:
			# check if the event needs updating
			updates = {}
			if discord_event.name != table_item.title:
				updates['name'] = table_item.title
			if discord_event.description != table_item.description:
				updates['description'] = table_item.description
			if discord_event.start_time != table_item.datetime:
				updates['start_time'] = table_item.datetime
				updates['end_time'] = table_item.datetime + datetime.timedelta(hours=1)
			if discord_event.location != table_item.location:
				updates['location'] = table_item.location
			if updates:
				await discord_event.edit(**updates)
				print(f"Updated {table_item.title}!")
			else:
				print(f"{table_item.title} already exists.")
		else:
			# has not been created, so create it
			discord_event = await guild.create_scheduled_event(
				name=table_item.title,
				description=table_item.description,
				start_time=table_item.datetime,
				end_time=table_item.datetime + datetime.timedelta(hours=1),
				location=table_item.location,
				entity_type=discord.EntityType.external,
				privacy_level=discord.PrivacyLevel.guild_only
			)
			table_item.snowflake_id = discord_event.id
			ddb.write_item(table_item)
			print(f"Created new event w/ snowflake id: {table_item.snowflake_id}")

@client.event
async def on_ready():
	await set_globals()
	print(f'We have logged in as {client.user}')
	
	# Run update_events once at startup
	await update_events()
	
	# Schedule update_events to run daily at midnight
	scheduler.add_job(update_events, CronTrigger(hour=0, minute=0))
	scheduler.start()
	print("Scheduled daily update_events job at midnight")

client.run(os.getenv('DISCORD_TOKEN'))
