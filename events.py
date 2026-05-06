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
from pynamodb.exceptions import AttributeDeserializationError
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
		ddb_event = MeetupEvent(event.id)
		ddb_event.title = event.name
		ddb_event.description = event.description
		ddb_event.category = ai_categorize(f"{event.name}\n\n{event.description}")
		ddb_event.start_time = event.start_time
		ddb_event.location = event.location
		ddb_event.snowflake_id = event.id
		ddb_event.online = (event.entity_type != discord.EntityType.external)
		ddb_event.save()
		return ddb_event

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

def update_event_from_json(event: MeetupEvent, j_item: dict):
	if not event.category:
		event.category = ai_categorize(f"{j_item['title'].strip()}\n\n{j_item['description'].strip()}")
	event.link = j_item['eventUrl']
	event.title = j_item['title'].strip()
	event.description = j_item['description'].strip()
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
			response = requests.get(rss_item['link'])
			soup = BeautifulSoup(response.text, features="lxml")
			j_item: dict = json.loads(soup.select_one('script#__NEXT_DATA__').text)
			j_item = j_item['props']['pageProps']['event']

			try:
				event = MeetupEvent.get('event', guid)
			except MeetupEvent.DoesNotExist:
				event = MeetupEvent(sort=guid)
			except AttributeDeserializationError:
				# this can happen if the data in ddb is corrupted or in an unexpected format, delete the item and start fresh
				print(f"data for event with guid {guid} is corrupted, deleting and starting fresh...")
				MeetupEvent.delete('event', guid)
				event = MeetupEvent(sort=guid)
			
			update_event_from_json(event, j_item)
			
			event.save()
			ret.append(event)
		except Exception as e:
			print(f"Exception occured while processing {rss_item}:\n{get_stacktrace()}")
	return ret

def check_existing_event(event: MeetupEvent):
	response = requests.get(f"https://www.meetup.com/chicago-anime-hangouts/events/{event.sort}/")
	if response.status_code == 404:
		event.delete()
		return False
	soup = BeautifulSoup(response.text, features="lxml")
	j_item: dict = json.loads(soup.select_one('script#__NEXT_DATA__').text)['props']['pageProps']['event']
	if j_item['status'] != "ACTIVE":
		event.delete()
		return False
	
	update_event_from_json(event, j_item)
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