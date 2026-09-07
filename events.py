import asyncio

from aws import RallyBotModel
from shared import shared

import discord
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup, Tag
import datetime as dt
from apscheduler.util import astimezone
import re
import os
import json
from decimal import Decimal
from traceback import format_exc as get_stacktrace
from pynamodb.exceptions import AttributeDeserializationError, DeleteError
from pynamodb.attributes import UnicodeAttribute, NumberAttribute, UTCDateTimeAttribute, BooleanAttribute

guid_finder = re.compile("https://www.meetup.com/chicago-anime-hangouts/events/([0-9]+)/")

categories = ["book club", "conventions", "food", "gaming", "karaoke", "outdoor", "watch party", "volunteering", "other"]

class MeetupEvent(RallyBotModel):
	title = UnicodeAttribute(null=True)
	description = UnicodeAttribute(null=True)
	link = UnicodeAttribute(null=True)
	datetime = UTCDateTimeAttribute(null=True)
	endtime = UTCDateTimeAttribute(null=True)
	timestamp = NumberAttribute(null=True)
	location = UnicodeAttribute(null=True)
	snowflake_id = NumberAttribute(default=0)
	category = UnicodeAttribute(null=True)
	online = BooleanAttribute(default=False)
	# forum pilot fields (see forum.py): forum_thread_id links the event to its
	# #event-chat forum post; created_at records when the event was first
	# scheduled (for the weekly digest's "newly planned" section) and is never
	# updated after creation
	forum_thread_id = NumberAttribute(null=True)
	created_at = UTCDateTimeAttribute(null=True)
	# untruncated meetup description; `description` is capped at 999 chars for
	# discord scheduled events, but forum posts want the full text
	full_description = UnicodeAttribute(null=True)

	# timestamp properties for backward compatibility
	@property
	def timestamp_start(self):
		# unix timestamp in milliseconds
		if self.datetime is None:
			return None
		self.timestamp = int(self.datetime.timestamp() * 1000)
		return self.timestamp
	@timestamp_start.setter
	def timestamp_start(self, value):
		# convert from milliseconds to datetime
		# Handle Decimal values from DynamoDB
		if value:
			if isinstance(value, Decimal):
				value = int(value)
			self.datetime = dt.datetime.fromtimestamp(value / 1000, tz=astimezone("America/Chicago"))
			self.timestamp = value
		else:
			self.datetime = None
			self.timestamp = None
	
	@property
	def start_time(self) -> dt.datetime | None:
		# unix timestamp in milliseconds
		if self.datetime is None and self.timestamp is not None:
			self.datetime = dt.datetime.fromtimestamp(self.timestamp / 1000, tz=astimezone("America/Chicago"))
		return self.datetime
	@start_time.setter
	def start_time(self, value: dt.datetime):
		self.datetime = value
		if value is None:
			self.timestamp = None
		else:
			self.timestamp = int(value.timestamp() * 1000)

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = "event"
	
	def __str__(self) -> str:
		date_str = self.start_time.strftime("%Y-%m-%d %I:%M %p") if hasattr(self, 'datetime') and self.start_time else "No date set"
		location_str = getattr(self, 'location', 'No location')
		title_str = getattr(self, 'title', 'Untitled Event')
		return f"MeetupEvent: {title_str} at {location_str} on {date_str}"
	
	@staticmethod
	def from_discord_event(event: discord.ScheduledEvent):
		print(f"snowflake {event.id} ({event.name}) not found in ddb, creating...")
		ddb_event = MeetupEvent(sort=event.id)
		ddb_event.title = event.name
		ddb_event.description = event.description
		ddb_event.category = ai_categorize(f"{event.name}\n\n{event.description}")
		ddb_event.start_time = event.start_time
		ddb_event.location = event.location
		ddb_event.snowflake_id = event.id
		ddb_event.online = (event.entity_type != discord.EntityType.external)
		ddb_event.created_at = dt.datetime.now(shared.est)
		ddb_event.save()
		return ddb_event

	async def delete(self, condition = None, *, add_version_condition = True):
		res = None
		try:
			discord_id = int(self.snowflake_id) if self.snowflake_id else None
			res = super().delete(condition, add_version_condition=add_version_condition)
		except DeleteError:
			print(f"ERROR: Failed to delete event {self.sort} | {self.title} from dynamodb!")
			return None
		except Exception as e:
			print(f"ERROR: Exception while deleting event {self.sort} | {self.title}\n{get_stacktrace()}")
			return None
		# also delete the corresponding discord event, fetching it live instead
		# of relying on the cache: uncached events used to blow up on None here
		# and silently leave the discord event behind
		if discord_id:
			try:
				devent = await shared.guild.fetch_scheduled_event(discord_id)
				await devent.delete()
			except discord.errors.NotFound:
				print(f"discord event {discord_id} for {self.sort} | {self.title} already gone from discord")
			except Exception as e:
				print(f"ERROR: Exception while deleting discord event {discord_id} for event {self.sort} | {self.title}\n{get_stacktrace()}")
		# clean up the event's forum post as well (runs even with the pilot
		# disabled, so cancelled events don't leave orphaned posts behind);
		# lazy import avoids a circular import with forum.py
		import forum
		await forum.delete_forum_post(self)
		return res

def xml_to_dict(xml_string):
	"""
	Converts an XML string to a dictionary, placing all <item> tags into an 'item' array.

	Args:
		xml_string (str): The XML string to be converted.

	Returns:
		dict: A dictionary representation of the XML.
	"""
	def element_to_dict(element):
		# Convert an XML element and its children to a dictionary
		node = {}
		for child in element:
			if child.tag == "item":
				if "item" not in node:
					node["item"] = []
				node["item"].append(element_to_dict(child))
			else:
				node[child.tag] = element_to_dict(child) if list(child) else child.text
		return node

	root = ET.fromstring(xml_string)
	return {root.tag: element_to_dict(root)}

def _meetup_url_to_json(url: str) -> dict | list | int:
	response = requests.get(url)
	if response.status_code != 200:
		return response.status_code
	soup = BeautifulSoup(response.text, features="lxml")
	j_item: dict = json.loads(soup.select_one('script#__NEXT_DATA__').text)
	j_item = j_item['props']['pageProps']
	if 'event' in j_item:
		return j_item['event']
	if '__APOLLO_STATE__' in j_item:
		# the events list page embeds an apollo cache: a flat map of every
		# normalized entity ("Event:<id>", "Venue:<id>", ...) keyed by type.
		# collect the Event entities and inline their venue refs so the
		# payloads match the per-event pageProps.event shape.
		apollo = j_item['__APOLLO_STATE__']
		events = [v for v in apollo.values()
				if isinstance(v, dict) and v.get('__typename') == 'Event']
		for ev in events:
			venue = ev.get('venue')
			if isinstance(venue, dict) and '__ref' in venue and venue['__ref'] in apollo:
				ev['venue'] = apollo[venue['__ref']]
		return events
	return []

def update_event_from_json(event: MeetupEvent, j_item: dict):
	if not event.category:
		event.category = ai_categorize(f"{j_item['title'].strip()}\n\n{j_item['description'].strip()}")
	event.link = j_item['eventUrl']
	event.title = j_item['title'].strip()
	event.description = j_item['description'].strip()
	# keep the full text for forum posts before `description` gets truncated
	# to discord's 999-char scheduled-event limit below
	event.full_description = j_item['description'].strip()
	if len(event.description) > 999:
		append = f"... [full event]({event.link})"
		event.description = event.description[0:(999 - len(append))] + append

	event.online = j_item['eventType'] == "ONLINE"
	event.start_time = dt.datetime.fromisoformat(j_item['dateTime'])
	event.endtime = dt.datetime.fromisoformat(j_item['endTime'])
	if not event.online:
		event.location = f"{j_item['venue']['address']}, {j_item['venue']['city']} | {j_item['venue']['name']}"
		if len(event.location) > 99:
			event.location = event.location[0:96]+"..."
	else:
		event.location = "Online"


def fetch_meetup_events() -> list[MeetupEvent]:
	"""
	Fetches the event list from the Meetup URL and converts it to a list of objects.
	"""
	ret = []
	event_items = _meetup_url_to_json("https://www.meetup.com/chicago-anime-hangouts/events/")
	if not isinstance(event_items, list):
		# a non-200 status code (int) or an unexpected page shape
		print(f"meetup event list fetch failed or returned an unexpected shape: {event_items!r}")
		return ret
	for j_item in event_items:
		guid = "?"
		try:
			guid = int(j_item['id'], base=10)

			# unlike the RSS feed, the events page lists past and cancelled
			# events too; only upcoming ACTIVE events belong in the mirror.
			# skipping cancelled ones keeps them out of the tracked set so
			# update_events' reconciliation pass deletes them from ddb+discord
			if j_item['status'] != "ACTIVE":
				# don't do anything with a non-active event
				continue
			if dt.datetime.fromisoformat(j_item['dateTime']) < dt.datetime.now(shared.est):
				# past event: nothing to mirror
				continue

			try:
				event = MeetupEvent.get('event', guid)
			except MeetupEvent.DoesNotExist:
				event = MeetupEvent(sort=guid)
				# stamp when the event was first scheduled; used by the weekly
				# digest's "newly planned" section and never updated afterwards
				event.created_at = dt.datetime.now(shared.est)
			except AttributeDeserializationError:
				# this can happen if the data in ddb is corrupted or in an unexpected format
				print(f"data for event with guid {guid} is attempting to mitigate...")
				raw_item = shared.ddb.read_raw('event', guid)
				event = MeetupEvent(sort=guid)
				if not raw_item:
					print(f"no raw data found for event with guid {guid}, deleting and recreating...")
				else:
					if raw_item.get('snowflake_id'):
						event.snowflake_id = int(raw_item['snowflake_id'])
					if raw_item.get('category'):
						event.category = raw_item['category']
					shared.ddb.delete_raw('event', guid)

			update_event_from_json(event, j_item)

			event.save()
			ret.append(event)
		except Exception as e:
			print(f"Exception occured while processing event {guid}:\n{get_stacktrace()}")
	return ret

def fetch_meetup_events_rss() -> list[MeetupEvent]:
	"""
	Fetches the RSS feed from the Meetup URL and converts it to a list of objects.
	"""
	ret = []
	url = "https://www.meetup.com/chicago-anime-hangouts/events/rss"
	response = requests.get(url)
	response.raise_for_status()  # Raise an exception for HTTP errors
	rss_content = xml_to_dict(response.text)
	for rss_item in rss_content['rss']['channel']['item']:
		try:
			guid = int(guid_finder.match(rss_item['guid']).group(1))
			j_item = _meetup_url_to_json(rss_item['link'])

			try:
				event = MeetupEvent.get('event', guid)
			except MeetupEvent.DoesNotExist:
				if j_item['status'] != "ACTIVE":
					# don't do anything with a non-active event
					continue
				event = MeetupEvent(sort=guid)
				# stamp when the event was first scheduled; used by the weekly
				# digest's "newly planned" section and never updated afterwards
				event.created_at = dt.datetime.now(shared.est)
			except AttributeDeserializationError:
				# this can happen if the data in ddb is corrupted or in an unexpected format
				print(f"data for event with guid {guid} is attempting to mitigate...")
				raw_item = shared.ddb.read_raw('event', guid)
				event = MeetupEvent(sort=guid)
				if not raw_item:
					print(f"no raw data found for event with guid {guid}, deleting and recreating...")
				else:
					if raw_item['snowflake_id']:
						event.snowflake_id = int(raw_item['snowflake_id'])
					if raw_item['category']:
						event.category = raw_item['category']
					shared.ddb.delete_raw('event', guid)
			
			update_event_from_json(event, j_item)
			
			event.save()
			ret.append(event)
		except Exception as e:
			print(f"Exception occured while processing {rss_item}:\n{get_stacktrace()}")
	return ret

async def check_existing_event(event: MeetupEvent):
	url = f"https://www.meetup.com/chicago-anime-hangouts/events/{event.sort}/"
	# the meetup scrape (and the AI categorizer inside update_event_from_json)
	# are blocking requests; keep them off the event loop
	j_item = await asyncio.to_thread(_meetup_url_to_json, url)
	if j_item == 404:
		if int(event.sort) != int(event.snowflake_id):
			print(f"{url} returned 404, deleting event with guid {event.sort} from ddb...")
			await event.delete()
		return False
	if isinstance(j_item, list):
		# the single-event page may serve the events-list apollo layout
		# instead of pageProps.event; dig this event's entry out of the list
		matches = [e for e in j_item if isinstance(e, dict) and str(e.get('id')) == str(event.sort)]
		j_item = matches[0] if matches else None
	if not isinstance(j_item, dict) or 'status' not in j_item:
		# unexpected shape or a non-404 error status code (403 rate limit,
		# 5xx, ...): fail safe — keep the event and let a later run recheck it
		print(f"{url} returned an unexpected shape ({j_item!r}); skipping recheck of {event.sort} | {event.title}")
		return False
	if j_item['status'] != "ACTIVE":
		print(f"deleting event {event.sort} | {event.title} as status {j_item['status']} is no longer ACTIVE...")
		await event.delete()
		return False
	
	await asyncio.to_thread(update_event_from_json, event, j_item)
	event.save()
	return True

DO_AI_ENDPOINT = os.getenv('DO_AI_ENDPOINT')
DO_AI_SECRET = os.getenv('DO_AI_SECRET')

def ai_categorize(description: str) -> str:
	if not DO_AI_ENDPOINT or not DO_AI_SECRET:
		print("DigitalOcean AI endpoint/secret not set, defaulting to 'other' category.")
		return 'other'
	headers = {
		"Content-Type": "application/json",
		"Authorization": f"Bearer {DO_AI_SECRET}"
	}
	payload = {
		"messages": [
			{
				"role": "user",
				"content": f"{description}\n\nCategories: {', '.join(categories)}"
			}
		],
		"stream": False,
		"include_functions_info": False,
		"include_retrieval_info": False,
		"include_guardrails_info": False
	}
	response = requests.post(f"{DO_AI_ENDPOINT}/api/v1/chat/completions", json=payload, headers=headers)
	response.raise_for_status()  # Raise an exception for HTTP errors
	message = response.json()['choices'][0]['message']
	cat = message['content'].lower()
	if cat not in categories:
		payload["messages"].append(message)
		payload["messages"].append({
			"role": "user",
			"content": f"{cat} is not a valid answer. select the best category from the following list: {', '.join(categories)}"
		})
		print(f"invalid category {cat}, retrying...")
		response = requests.post(f"{DO_AI_ENDPOINT}/api/v1/chat/completions", json=payload, headers=headers)
		response.raise_for_status()  # Raise an exception for HTTP errors
		message = response.json()['choices'][0]['message']
		cat = message['content'].lower()
		if cat not in categories:
			# failed again, just use the "other" category
			cat = "other"
	return cat

if __name__ == "__main__":
	from time import sleep
	for event in MeetupEvent.scan(index_name="timestamp-index", filter_condition=MeetupEvent.timestamp > int(dt.datetime.now(shared.est).timestamp() * 1000)):
		j_item = _meetup_url_to_json(f"https://www.meetup.com/chicago-anime-hangouts/events/{event.sort}/")
		print(f"DEBUG: rechecked event {event.sort} | {event.title} status: {j_item['status']}")
		sleep(5)
		# check_existing_event(event)