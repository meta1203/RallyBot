from aws import TableItem
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
from traceback import format_exc as get_stacktrace

guid_finder = re.compile("https://www.meetup.com/chicago-anime-hangouts/events/([0-9]+)/")

categories = ["book club", "conventions", "food", "gaming", "karaoke", "outdoor", "watch party", "volunteering", "other"]

class MeetupEvent(TableItem):
	title: str
	description: str
	link: str
	datetime: dt.datetime # primary storage as datetime object
	endtime: dt.datetime
	location: str
	snowflake_id: int = 0
	category: str
	online: bool

	# timestamp properties for backward compatibility
	@property
	def timestamp(self):
		# unix timestamp in milliseconds
		if not hasattr(self, 'datetime') or self.datetime is None:
			return None
		return int(self.datetime.timestamp() * 1000)
	@timestamp.setter
	def timestamp(self, value: int):
		# convert from milliseconds to datetime
		if value:
			self.datetime = dt.datetime.fromtimestamp(value / 1000, tz=astimezone("America/Chicago"))
		else:
			self.datetime = None
	
	@property
	def timestamp_end(self):
		# unix timestamp in milliseconds
		if not hasattr(self, 'endtime') or self.endtime is None:
			return None
		return int(self.endtime.timestamp() * 1000)
	@timestamp_end.setter
	def timestamp_end(self, value: int):
		# convert from milliseconds to datetime
		if value:
			self.endtime = dt.datetime.fromtimestamp(value / 1000, tz=astimezone("America/Chicago"))
		else:
			self.endtime = None

	def __init__(self, meetup_id: int) -> None:
		self.id = "event"
		self.sort = meetup_id
		self.online = False
		self.category = None
	
	def __getstate__(self):
		"""Custom serialization to ensure both datetime and timestamp are saved to DynamoDB"""
		state = self.__dict__.copy()
		# Add computed timestamp fields for backward compatibility
		if hasattr(self, 'datetime') and self.datetime is not None:
			state['timestamp'] = int(self.datetime.timestamp() * 1000)
		if hasattr(self, 'endtime') and self.endtime is not None:
			state['timestamp_end'] = int(self.endtime.timestamp() * 1000)
		return state
	
	def __setstate__(self, state):
		"""Custom deserialization to handle both old (timestamp) and new (datetime) formats"""
		# If we have timestamp but no datetime, convert it
		if 'timestamp' in state and ('datetime' not in state or state.get('datetime') is None):
			if state['timestamp']:
				state['datetime'] = dt.datetime.fromtimestamp(state['timestamp'] / 1000, tz=astimezone("America/Chicago"))
		if 'timestamp_end' in state and ('endtime' not in state or state.get('endtime') is None):
			if state['timestamp_end']:
				state['endtime'] = dt.datetime.fromtimestamp(state['timestamp_end'] / 1000, tz=astimezone("America/Chicago"))
		# Remove timestamp from state since they're now properties
		state.pop('timestamp', None)
		state.pop('timestamp_end', None)
		self.__dict__.update(state)
	
	def __str__(self) -> str:
		date_str = self.datetime.strftime("%Y-%m-%d %I:%M %p") if hasattr(self, 'datetime') and self.datetime else "No date set"
		location_str = getattr(self, 'location', 'No location')
		title_str = getattr(self, 'title', 'Untitled Event')
		return f"MeetupEvent: {title_str} at {location_str} on {date_str}"
	
	def from_discord_event(event: discord.ScheduledEvent):
		print(f"snowflake {event.id} ({event.name}) not found in ddb, creating...")
		ddb_event = MeetupEvent(event.id)
		ddb_event.title = event.name
		ddb_event.description = event.description
		ddb_event.category = ai_categorize(f"{event.name}\n\n{event.description}")
		ddb_event.datetime = event.start_time
		ddb_event.location = event.location
		ddb_event.snowflake_id = event.id
		ddb_event.online = (event.entity_type != discord.EntityType.external)
		shared.ddb.write_item(ddb_event)
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
			event: MeetupEvent = shared.ddb.read_item('event', guid)
			if not event:
				event = MeetupEvent(guid)
			if not hasattr(event, 'category') or not event.category:
				event.category = ai_categorize(f"{rss_item['title'].strip()}\n\n{rss_item['description'].strip()}")
			event.link = rss_item['link']
			event.title = rss_item['title'].strip()
			event.description = rss_item['description'].strip()
			if len(event.description) > 999:
				append = f"... [full event]({event.link})"
				event.description = event.description[0:(999 - len(append))] + append
			
			response = requests.get(event.link)
			soup = BeautifulSoup(response.text, features="lxml")
			j_item: dict = json.loads(soup.select_one('script#__NEXT_DATA__').text)
			j_item = j_item['props']['pageProps']['event']

			event.online = j_item['eventType'] == "ONLINE"
			event.datetime = dt.datetime.fromisoformat(j_item['dateTime'])
			event.endtime = dt.datetime.fromisoformat(j_item['endTime'])
			if not event.online:
				event.location = f"{j_item['venue']['address']}, {j_item['venue']['city']} | {j_item['venue']['name']}"
				if len(event.location) > 99:
					event.location = event.location[0:96]+"..."
			else:
				event.location = "Online"
			shared.ddb.write_item(event)
			ret.append(event)
		except Exception as e:
			print(f"Exception occured while processing {rss_item}:\n{get_stacktrace()}")
	return ret

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
