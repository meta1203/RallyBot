"""Ad hoc forum events: user-created event posts in #event-chat -> cah.moe.

Every user post in the forum channel is tracked with a ForumEvent row
(id="forum_event", sort = thread snowflake). A real-time pipeline (see
process_thread) extracts event info from the post via the AI extractor
(llm.ai_extract_event) and writes an ad hoc MeetupEvent record
(source="forum", sort = thread snowflake, link = the forum post URL) that
cah.moe renders alongside the Meetup-mirrored events. No discord scheduled
event is created and the bot never rewrites the user's post.

Lifecycle, all real-time (a startup sweep backfills anything missed while
the bot was offline):
- post created  -> analyze; valid -> event record + status reply;
  missing start/location -> public reply listing what's missing
- post edited   -> reanalyze + recompute (unless the event already started)
- `nonpublic` tag added   -> event record deleted + once-per-application reply
- `nonpublic` tag removed -> record recreated from the last extraction
- post deleted  -> ForumEvent row + event record deleted

Status notices are a single replaceable reply per thread: posting a new one
deletes the previous bot reply (reply_message_id is tracked on the row).
"""
import asyncio
import datetime as dt
import hashlib
import json
import traceback

import discord

import events
import forum
import llm
from aws import RallyBotModel
from shared import shared

from pynamodb.attributes import UnicodeAttribute, NumberAttribute, UTCDateTimeAttribute, BooleanAttribute

NONPUBLIC_TAG_NAME = "nonpublic"
FORUM_SOURCE = "forum"


class ForumEvent(RallyBotModel):
	"""Tracking row for one user-created forum post (see module docstring)."""
	thread_id = NumberAttribute()  # == sort; the forum thread's snowflake
	event_sort = NumberAttribute(null=True)  # linked MeetupEvent sort key; None = no record
	state = UnicodeAttribute(null=True)  # event / missing_info / not_event / nonpublic
	missing_fields = UnicodeAttribute(null=True)  # comma-joined floor fields for the missing-info reply
	last_extraction = UnicodeAttribute(null=True)  # JSON of the last resolved extraction (enables no-AI recreation)
	last_analyzed_hash = UnicodeAttribute(null=True)  # sha256 of name + starter content + sorted tag ids
	last_edited_at = UTCDateTimeAttribute(null=True)  # discord's own edited_timestamp at last analysis
	last_analyzed_at = UTCDateTimeAttribute(null=True)
	reply_message_id = NumberAttribute(null=True)  # the bot's current status reply in the thread
	nonpublic_seen = BooleanAttribute(default=False)  # True once the skip reply fired for the current tag application
	created_at = UTCDateTimeAttribute(null=True)

	def __init__(self, **kwargs) -> None:
		super().__init__(**kwargs)
		self.id = "forum_event"


def _post_link(thread_id) -> str:
	"""Web URL of the forum post (also used as the ad hoc event's `link`)."""
	return f"https://discord.com/channels/{shared.guild.id}/{int(thread_id)}"


def _starter_content_hash(thread, starter) -> str:
	"""Change-detection fingerprint: post title + starter body + applied tags."""
	tag_ids = ",".join(sorted(str(t.id) for t in (thread.applied_tags or [])))
	raw = f"{thread.name}\x00{starter.content}\x00{tag_ids}"
	return hashlib.sha256(raw.encode()).hexdigest()


async def _fetch_starter(thread) -> discord.Message:
	"""REST-fetch the post's starter message (id == thread id). REST returns
	full content even without the message_content intent, unlike the cache."""
	return await thread.fetch_message(int(thread.id))


def _has_nonpublic_tag(thread) -> bool:
	names = {t.name.lower() for t in (thread.applied_tags or [])}
	return NONPUBLIC_TAG_NAME in names


def _norm_dt(value):
	"""Normalize a datetime for equality comparison (naive -> UTC)."""
	if value is None:
		return None
	if isinstance(value, dt.datetime) and value.tzinfo is None:
		return value.replace(tzinfo=dt.timezone.utc)
	return value
