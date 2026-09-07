"""Import smoke test: every module must import with Client.run stubbed (no
token, no network). Per the discord-bot-development skill this catches
class-level breakage py_compile misses."""
import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed run)")

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FORUM_PILOT"] = "1"  # exercise the pilot-enabled import path

import aws
import shared
import events
import forum
import report
import onboarding
import main

# pilot-off import path is covered by re-importing forum helpers with the env
# var deleted (forum.forum_pilot_enabled reads it at call time, not import time)
del os.environ["FORUM_PILOT"]
assert forum.forum_pilot_enabled() is False
os.environ["FORUM_PILOT"] = "1"
assert forum.forum_pilot_enabled() is True

assert main.FORUM_PILOT is True
print("SMOKE TEST PASSED: all modules import, kill-switch toggles, main wired")
