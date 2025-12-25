import os
import aws
import discord
import asyncio
from collections import deque
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

class Singleton:
	client: discord.Client = None
	guild: discord.Guild = None
	est = ZoneInfo('America/Chicago')
	_channels: dict[str, discord.guild.TextChannel] = None
	recent_messages = deque(maxlen=5)

	def __init__(self):
		self._ddb: aws.DynamoDBClient = None
		self._loop: asyncio.AbstractEventLoop = None
		self._scheduler: AsyncIOScheduler = None
		self._quiet = not not os.getenv('QUIET_RALLY')
	
	@property
	def ddb(self):
		if not self._ddb:
			self._ddb = aws.DynamoDBClient()
		return self._ddb
	
	@property
	def loop(self):
		if not self._loop:
			self._loop = asyncio.get_running_loop()
		return self._loop
	
	@property
	def scheduler(self):
		if not self._scheduler:
			self._scheduler = AsyncIOScheduler(gconfig={'event_loop': self.loop})
		return self._scheduler
	
	async def message_channel(self, channel_name: str, message: str):
		channel = await self.get_channel_by_name(channel_name)
		if not channel:
			print(f"ERROR: invalid channel name {channel_name}")
			return None
		# prevent accidentally spamming channels with the same message
		msg_key = f"{channel_name}: {message}"
		if msg_key in self.recent_messages:
			print(f"Not sending message to prevent spam | {msg_key}")
			return None
		self.recent_messages.append(msg_key)
		print(f"sending message -> {msg_key}")
		if self._quiet:
			return None
		else:
			return await channel.send(message)

	async def get_channel_by_name(self, name: str):
		if not self._channels:
			self._channels = {}
			discord_channels = await self.guild.fetch_channels()
			for d_c in discord_channels:
				self._channels[d_c.name] = d_c
		name = name.replace(" ", "-")
		if name not in self._channels:
			print(f"couldn't find {name} in channels {self._channels}")
			return None
		else:
			return self._channels[name]
	
shared = Singleton()