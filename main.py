import discord

from shared import shared
import events

from apscheduler.triggers.cron import CronTrigger
import os
import datetime
from traceback import format_exc as get_stacktrace

intents = discord.Intents.default()
# required intents for the bot to function
intents.guild_scheduled_events = True
intents.guild_messages = True

client = discord.Client(intents=intents)
IN_PERSON_MENTION = "<@&1366086187906895923>"
ONLINE_MENTION = "<@&1366085997917638826>"

async def set_globals():
	print("setting globals...")
	shared.client = client
	shared.guild = await client.fetch_guild("1219601473948614737")

async def update_events():
	on_meetup = events.fetch_meetup_events()
	for event in on_meetup:
		discord_event: (discord.ScheduledEvent | None) = None
		if event.snowflake_id:
			print(f"reading in snowflake id: {event.snowflake_id}")
			discord_event = None
			try:
				discord_event = await shared.guild.fetch_scheduled_event(event.snowflake_id)
			except discord.errors.NotFound as e:
				print(f"Invalid snowflake value for {event.title}, this has likely been deleted from discord, recreating...")
			except Exception as e:
				print(f"Exception occured while processing {event.title} ({event.id} | {event.snowflake_id}):\n{get_stacktrace()}")
				continue
		if discord_event:
			# check if the event needs updating
			updates = {}
			if discord_event.name != event.title:
				print(f"{discord_event.name} -> {event.title}")
				updates['name'] = event.title
			if discord_event.description != event.description:
				print(f"{discord_event.description} -> {event.description}")
				updates['description'] = event.description
			if discord_event.start_time != event.datetime:
				print(f"{discord_event.start_time} -> {event.datetime}")
				updates['start_time'] = event.datetime
			# use the explicit endtime if it exists and is set, otherwise its implicitly an hour long
			if hasattr(event, 'endtime') and event.endtime and discord_event.end_time != event.endtime:
				updates['end_time'] = event.endtime
				if discord_event.start_time > updates['end_time']:
					# this is weird, the end time is after the start time???
					print(f"this is weird, this is weird, the end time is after the start time???\n{discord_event.start_time} -> {updates['end_time']}\n{event.datetime} -> {event.endtime}")
					updates['start_time'] = event.datetime
			if discord_event.location != event.location:
				print(f"{discord_event.location} -> {event.location}")
				updates['location'] = event.location
			if len(updates) > 0:
				try:
					await discord_event.edit(**updates)
					shared.ddb.write_item(event)
				except Exception as e:
					print(f"Exception occured while updating {event} <- {updates}:\n{get_stacktrace()}")
					continue
				category = get_channel_for_ddb_event(event)
				target_role = ONLINE_MENTION if event.online else IN_PERSON_MENTION
				await shared.message_channel(category, f"{target_role} {event.title} has been updated.")
				print(f"Updated {event.title}!")
			else:
				print(f"{event.title} already exists.")
		else:
			# has not been created, so create it
			discord_event = await shared.guild.create_scheduled_event(
				name=event.title,
				description=event.description,
				start_time=event.datetime,
				end_time=event.endtime if hasattr(event, 'endtime') and event.endtime else (event.datetime + datetime.timedelta(hours=1)),
				location=event.location,
				entity_type=discord.EntityType.external,
				privacy_level=discord.PrivacyLevel.guild_only
			)
			event.snowflake_id = discord_event.id
			shared.ddb.write_item(event)
			print(f"Created new event w/ snowflake id: {event.snowflake_id}")
			await notify_new_event(event)

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
	target_role = ONLINE_MENTION if event.online else IN_PERSON_MENTION
	await shared.message_channel(category, f"{target_role} {event.title} has been scheduled for <t:{round(event.datetime.timestamp())}>.")

async def notify_events():
	discord_events = await shared.guild.fetch_scheduled_events()
	now = datetime.datetime.now(shared.est)
	for de in discord_events:
		ddb_event: events.MeetupEvent = shared.ddb.scan_item("snowflake_id", de.id)
		if not ddb_event:
			ddb_event = events.MeetupEvent.from_discord_event(de)
			await notify_new_event(ddb_event)
			continue
		
		# handle categories
		category = get_channel_for_ddb_event(ddb_event)
		
		# event is in-person and starts sometime between 24 and 25 hours from now
		if not ddb_event.online and de.start_time > (now+datetime.timedelta(hours=24)) and de.start_time < (now+datetime.timedelta(hours=25)):
			await shared.message_channel(category, f"{IN_PERSON_MENTION} {de.name} is tomorrow! (<t:{round(de.start_time.timestamp())}>)")
		# event is online and starts sometime between 1 and 2 hours from now
		elif ddb_event.online and de.start_time > (now+datetime.timedelta(hours=1)) and de.start_time < (now+datetime.timedelta(hours=2)):
			await shared.message_channel(category, f"{ONLINE_MENTION} {de.name} starts soon! (<t:{round(de.start_time.timestamp())}:t>)")

@client.event
async def on_ready():
	await set_globals()
	print(f'We have logged in as {client.user}')
	
	# Run update_events once at startup
	await update_events()

	# Schedule update_events to run daily at 12:30 PM
	shared.scheduler.add_job(update_events, CronTrigger(hour=12, minute=30, timezone="America/Chicago"), max_instances=1)
	shared.scheduler.add_job(notify_events, CronTrigger(minute=0), max_instances=1)
	shared.scheduler.start()
	print("Successfully scheduled jobs.")

client.run(os.getenv('DISCORD_TOKEN'))
