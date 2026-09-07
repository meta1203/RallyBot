"""Offline test for the rewritten fetch_meetup_events + _meetup_url_to_json.

Feeds the committed Apollo sample through the real functions with PynamoDB
stubbed out (no AWS calls, no production table touched), then asserts exactly
which events fetch_meetup_events returns and which it skips.
"""
import os
import sys
import asyncio
import unittest.mock as mock
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['QUIET_RALLY'] = '1'

import aws

# ---- stub ALL persistence before importing events (prod-table safety) ----
store = {}

def fake_save(self, *args, **kwargs):
	store[(self.id, self.sort)] = dict(self.attribute_values)
	return True

aws.RallyBotModel.save = fake_save
aws.RallyBotModel.scan = mock.Mock(return_value=iter([]))

import events
import shared

# PynamoDB builds a *per-class* DoesNotExist subclass; events.py catches
# MeetupEvent.DoesNotExist, so the stub must raise exactly that exception.
events.MeetupEvent.get = mock.Mock(side_effect=events.MeetupEvent.DoesNotExist)

PASS = []
FAIL = []

def check(name, cond, detail=''):
	(PASS if cond else FAIL).append((name, detail))

# ---- 1. parse the committed Apollo sample through the real helper ----
sample_text = open(os.path.join(ROOT, 'sample_apollo_state.json')).read()

class FakeResp:
	status_code = 200
	text = sample_text

class FakeScriptTag:
	text = sample_text

with mock.patch.object(events.requests, 'get', return_value=FakeResp()), \
		mock.patch.object(events.BeautifulSoup, 'select_one', return_value=FakeScriptTag()):
	parsed = events._meetup_url_to_json('https://meetup.example/events/')

check("sample parses to a list", isinstance(parsed, list))
check("sample yields 30 events", isinstance(parsed, list) and len(parsed) == 30,
		f"got {len(parsed) if isinstance(parsed, list) else parsed!r}")

sample_events = parsed if isinstance(parsed, list) else []
sample_by_id = {e['id']: e for e in sample_events}
ven = sample_by_id['314490702']['venue']
check("venue __ref inlined (name/address/city present)",
		isinstance(ven, dict) and all(k in ven for k in ('name', 'address', 'city')), repr(ven))

# ---- 2. fetch_meetup_events over the sample ----
now = datetime.now(timezone.utc).astimezone(shared.shared.est)
active_future_ids = sorted(int(e['id']) for e in sample_events
		if e['status'] == 'ACTIVE'
		and datetime.fromisoformat(e['dateTime']) >= now)

with mock.patch.object(events, '_meetup_url_to_json', return_value=sample_events):
	result = events.fetch_meetup_events()

got_ids = sorted(e.sort for e in result)
check("returns exactly the ACTIVE+future events", got_ids == active_future_ids,
		f"expected {active_future_ids} got {got_ids}")
check("one DDB save per returned event", len(store) == len(result),
		f"store={len(store)} result={len(result)}")
check("created_at stamped on all new events",
		all(e.created_at is not None for e in result),
		str([e.sort for e in result if e.created_at is None]))

past_ids = [int(e['id']) for e in sample_events
		if e['status'] == 'ACTIVE' and datetime.fromisoformat(e['dateTime']) < now]
check("sample contains past ACTIVE events (test validity)", len(past_ids) > 0, str(past_ids))
check("past ACTIVE events skipped",
		all(p not in got_ids for p in past_ids),
		str([p for p in past_ids if p in got_ids]))
check("cancelled event 312553841 skipped", 312553841 not in got_ids)

saved314 = store.get(('event', 314490702))
check("event 314490702 skipped (past) so not saved", saved314 is None)
future_id = active_future_ids[0] if active_future_ids else None
saved_future = store.get(('event', future_id)) if future_id else None
if saved_future:
	src = sample_by_id[str(future_id)]
	check("title set from json", saved_future['title'] == src['title'].strip())
	check("location built from inlined venue",
			src['venue']['name'] in saved_future['location'], repr(saved_future['location']))
	check("link set", saved_future['link'] == src['eventUrl'])
	check("online flag False for PHYSICAL", saved_future['online'] is False)
else:
	check("at least one future event saved to inspect", False, "no future event in sample store")

# ---- 3. shape guards ----
with mock.patch.object(events, '_meetup_url_to_json', return_value=403):
	r = events.fetch_meetup_events()
check("non-list fetch (403) returns [] without raising", r == [])

fake_evt = events.MeetupEvent(sort=314490702)
fake_evt.snowflake_id = 999
fake_evt.title = "T"
fake_evt.datetime = now
fake_evt.timestamp = int(now.timestamp() * 1000)

async def run_list_shape_test():
	with mock.patch.object(events, '_meetup_url_to_json', return_value=sample_events):
		return await events.check_existing_event(fake_evt)

try:
	res = asyncio.run(run_list_shape_test())
	check("check_existing_event survives list-shaped payload", isinstance(res, bool), f"got {res!r}")
except Exception as ex:
	check("check_existing_event survives list-shaped payload", False, repr(ex))

async def run_scalar_test():
	with mock.patch.object(events, '_meetup_url_to_json', return_value=403):
		return await events.check_existing_event(fake_evt)

try:
	res2 = asyncio.run(run_scalar_test())
	check("check_existing_event fail-safe on 403 (False, no delete)", res2 is False, f"got {res2!r}")
except Exception as ex:
	check("check_existing_event fail-safe on 403", False, repr(ex))

# ---- 4. import smoke test with Client.run stubbed ----
import discord.client
discord.client.Client.run = lambda self, *a, **k: print("(stubbed)")
for mod_name in ['main', 'forum', 'report', 'onboarding']:
	try:
		__import__(mod_name)
		PASS.append((f"import {mod_name}", ''))
	except Exception as ex:
		FAIL.append((f"import {mod_name}", repr(ex)))

print("\n==== RESULTS ====")
for name, detail in PASS:
	print(f"PASS  {name}" + (f" | {detail}" if detail else ''))
for name, detail in FAIL:
	print(f"FAIL  {name}" + (f" | {detail}" if detail else ''))
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
print("ALL PASSED" if not FAIL else "SOME FAILED")
sys.exit(1 if FAIL else 0)
