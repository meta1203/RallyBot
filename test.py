import requests
import shared
import datetime as dt
from events import xml_to_dict, MeetupEvent, guid_finder
from bs4 import BeautifulSoup, Tag
from traceback import format_exc as get_stacktrace

import json

if __name__ == "__main__":
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
			event = MeetupEvent(guid)

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
			event.start_time = dt.datetime.fromisoformat(j_item['dateTime'])
			event.endtime = dt.datetime.fromisoformat(j_item['endTime'])
			if not event.online:
				event.location = f"{j_item['venue']['address']}, {j_item['venue']['city']} | {j_item['venue']['name']}"
				if len(event.location) > 100:
					event.location = event.location[0:97]+"..."
			else:
				event.location = "Online"
			print(f'final results: {event.title} (online: {event.online}) | {event.start_time} -> {event.endtime} @ {event.location}\n{event.description}')
			ret.append(event)
		except Exception as e:
			print(f"Exception occured while processing {rss_item}:\n{get_stacktrace()}")