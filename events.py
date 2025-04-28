import xml.etree.ElementTree as ET
from aws import TableItem
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re
import os

guid_finder = re.compile("https://www.meetup.com/chicago-anime-hangouts/events/([0-9]+)/")

categories = ["book club", "conventions", "food", "gaming", "karaoke", "outdoor", "watch party", "other"]

class MeetupEvent(TableItem):
	title: str
	description: str
	link: str
	datetime: datetime
	location: str
	snowflake_id: int = 0
	category: str = "other"
	online: bool = False

	def __init__(self, meetup_id: int) -> None:
		self.id = "event"
		self.sort = meetup_id

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
	for item in rss_content['rss']['channel']['item']:
		event = MeetupEvent(int(guid_finder.match(item['guid']).group(1)))
		event.link = item['link']
		event.title = item['title'].strip()
		event.description = item['description'].strip()
		event.category = ai_categorize(event.description)
		if len(event.description) > 999:
			append = f"... [full event]({event.link})"
			event.description = event.description[0:(999 - len(append))] + append
		response = requests.get(event.link)
		soup = BeautifulSoup(response.text, features="lxml")
		event.datetime = datetime.fromisoformat(soup.select_one("time.block")['datetime'])
		event.location = soup.select_one('[data-testid="location-info"]').text.strip()
		ret.append(event)
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
        "content": f"{description}\n\n{', '.join(categories)}?"
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