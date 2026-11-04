"""Import smoke test: every module must import with Client.run stubbed (no
token, no network). Per the discord-bot-development skill this catches
class-level breakage py_compile misses."""
import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed run)")

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aws
import shared
import events
import forum
import report
import onboarding
import main

assert main.client is not None
print("SMOKE TEST PASSED: all modules import, main wired")
