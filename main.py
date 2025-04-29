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
intents.guild_messages = True

ddb: aws.DynamoDBClient = None
client = discord.Client(intents=intents)
guild: discord.Guild = None
loop: asyncio.AbstractEventLoop = None
scheduler: AsyncIOScheduler = None

channels: dict[str, discord.guild.TextChannel] = {}

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
				print(f"{discord_event.name} -> {table_item.title}")
				updates['name'] = table_item.title
			if discord_event.description != table_item.description:
				print(f"{discord_event.description} -> {table_item.description}")
				updates['description'] = table_item.description
			if discord_event.start_time != table_item.datetime:
				print(f"{discord_event.start_time} -> {table_item.datetime}")
				updates['start_time'] = table_item.datetime
				updates['end_time'] = table_item.datetime + datetime.timedelta(hours=1)
			if discord_event.location != table_item.location:
				print(f"{discord_event.location} -> {table_item.location}")
				updates['location'] = table_item.location
			if updates:
				await discord_event.edit(**updates)
				ddb.write_item(table_item)
				category = get_channel_for_ddb_event(table_item)
				target_role = 'online-events' if table_item.online else 'in-person-events'
				await message_channel(category, f"@{target_role} {table_item.title} has been updated.")
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
			notify_new_event(table_item)

def ddb_event_from_discord_event(event: discord.ScheduledEvent) -> events.MeetupEvent:
	print(f"snowflake {event.id} ({event.name}) not found in ddb, creating...")
	ddb_event = events.MeetupEvent(event.id)
	ddb_event.title = event.name
	ddb_event.description = event.description
	ddb_event.datetime = event.start_time
	ddb_event.location = event.location
	ddb_event.snowflake_id = event.id
	ddb_event.online = (event.entity_type == discord.EntityType.external)
	ddb.write_item(ddb_event)
	return ddb_event

def get_channel_for_ddb_event(event: events.MeetupEvent):
	if not event:
		return 'events-general'
	category = event.category
	if category not in events.categories:
		category = 'other'
	if category == 'other':
		category = 'events-general'
	return category

async def notify_new_event(event: events.MeetupEvent):
	category = get_channel_for_ddb_event(event)
	target_role = 'online-events' if event.online else 'in-person-events'
	message_channel(category, f"@{target_role} {event.title} has been scheduled for <t:{round(event.datetime.timestamp())}>.")

async def notify_events():
	discord_events = await guild.fetch_scheduled_events()
	now = datetime.datetime.now()
	for de in discord_events:
		ddb_event: events.MeetupEvent = ddb.scan_item("snowflake_id", de.id)
		if not ddb_event:
			ddb_event = ddb_event_from_discord_event(de)
			await notify_new_event(ddb_event)
			continue
		
		# handle categories
		category = get_channel_for_ddb_event(ddb_event)
		
		# event is in-person and starts sometime between 4 and 5 hours from now
		if not ddb_event.online and de.start_time > (now+datetime.timedelta(hours=4)) and de.start_time < (now+datetime.timedelta(hours=5)):
			await message_channel(category, f"@in-person-events {de.name} starts soon! (<t:{round(de.start_time.timestamp())}:t>)")
		# event is online and starts sometime between 1 and 2 hours from now
		elif ddb_event.online and de.start_time > (now+datetime.timedelta(hours=1)) and de.start_time < (now+datetime.timedelta(hours=2)):
			await message_channel(category, f"@online-events {de.name} starts soon! (<t:{round(de.start_time.timestamp())}:t>)")
	
async def message_channel(channel_name: str, message: str):
	channel = await get_channel_by_name(channel_name)
	if not channel:
		print(f"ERROR: invalid channel name {channel_name}")
		return None
	print(f"sending message -> {channel_name}: {message}")
	# return await channel.send(message)

async def get_channel_by_name(name: str):
	name = name.replace(" ", "-")
	if name not in channels:
		discord_channels = await guild.fetch_channels()
		for d_c in discord_channels:
			if d_c.name == name:
				channels[name] = d_c
				return d_c
		return None
	else:
		return channels[name]

@client.event
async def on_ready():
	await set_globals()
	print(f'We have logged in as {client.user}')
	
	# Run update_events once at startup
	await update_events()
	
	# Schedule update_events to run daily at 12:30 PM
	scheduler.add_job(update_events, CronTrigger(hour=12, minute=30))
	scheduler.add_job(notify_events, CronTrigger(minute=0))
	scheduler.start()
	print("Successfully scheduled jobs.")

client.run(os.getenv('DISCORD_TOKEN'))
