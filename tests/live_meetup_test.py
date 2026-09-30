"""LIVE smoke test: the Meetup event fetch still works in production.

Unlike the offline tests next to it (apollo_fetch_test.py feeds a committed
sample through the scrapers with the network mocked out), this one talks to
the real meetup.com and exercises the same code paths the bot uses:

	1. events.fetch_meetup_events()  -- the production fetch used by
	   main.update_events (events list page -> embedded __NEXT_DATA__
	   apollo state), with all persistence stubbed out
	2. events._meetup_url_to_json()  -- single-event page scrape, the path
	   check_existing_event uses for cancellation rechecks
	3. the events/rss feed           -- fetch + xml_to_dict + a per-item page

Persistence is hard-stubbed before events.py is imported: RallyBotModel.save
records into an in-memory store and MeetupEvent.get raises DoesNotExist, so
no DynamoDB table can be touched and no Discord call is made. The
DigitalOcean categorizer is disabled for the same reason.

Run it with the project venv, from anywhere:

	.venv/bin/python tests/live_meetup_test.py

Exit codes:
	0  all checks passed (ALL PASSED)
	1  the fetch/parse pipeline is broken and needs a code fix (SOME FAILED)
	2  inconclusive: meetup.com unreachable from this box, or no upcoming
	   events to validate (INCONCLUSIVE -- investigate, don't assume code
	   breakage; only exit 1 should trigger a fix attempt)

Every network call is retried RETRIES times with backoff before its stage is
marked failed, and every request gets REQUEST_TIMEOUT seconds (events.py's
own requests.get calls have no timeout).
"""
import datetime as dt
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['QUIET_RALLY'] = '1'

import requests  # noqa: E402  (exception classes for network-vs-shape triage)

import aws  # noqa: E402

# ---- stub ALL persistence before importing events (prod-table safety) ------
store = {}


def fake_save(self, *args, **kwargs):
	store[(self.id, self.sort)] = dict(self.attribute_values)
	return True


def fake_scan(*args, **kwargs):
	return iter(())


aws.RallyBotModel.save = fake_save
aws.RallyBotModel.scan = fake_scan

import events  # noqa: E402
import shared  # noqa: E402


def fake_get(*args, **kwargs):
	raise events.MeetupEvent.DoesNotExist


events.MeetupEvent.get = fake_get

# hermetic + quiet: stub the categorizer (not part of the fetch path) so an
# AI call is impossible even if DO_AI_* env vars are set on this box, and the
# output stays free of one "defaulting to 'other'" line per event
events.DO_AI_ENDPOINT = None
events.DO_AI_SECRET = None


def _stub_categorize(description):
	return 'other'


events.ai_categorize = _stub_categorize

# events.py's requests.get() calls carry no timeout; cap every request this
# script makes so a stalled connection can never hang the weekly check
REQUEST_TIMEOUT = 30
_orig_get = events.requests.get


def _get_with_timeout(url, *args, **kwargs):
	kwargs.setdefault('timeout', REQUEST_TIMEOUT)
	return _orig_get(url, *args, **kwargs)


events.requests.get = _get_with_timeout

# keep in sync with events.py (the group slug is hardcoded there too)
EVENTS_URL = "https://www.meetup.com/chicago-anime-hangouts/events/"
RSS_URL = "https://www.meetup.com/chicago-anime-hangouts/events/rss"
EVENT_PAGE_URL = "https://www.meetup.com/chicago-anime-hangouts/events/{}/"

RETRIES = 3
RETRY_DELAY = 8  # seconds; multiplied by the attempt number (8, then 16)

START = time.monotonic()
PASS = []
FAIL = []
INCONCLUSIVE = []


def check(name, cond, detail=''):
	(PASS if cond else FAIL).append((name, detail))
	print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" | {detail}" if detail else ''))
	return bool(cond)


def record_fetch_failure(kind, detail):
	"""Record a failed fetch attempt ('broken' -> FAIL, else INCONCLUSIVE)."""
	if kind == 'broken':
		FAIL.append((detail, ''))
	else:
		INCONCLUSIVE.append(detail)


def _short(value, limit=140):
	text = repr(value)
	return text if len(text) <= limit else text[:limit] + '...'


def fetch_retry(label, fn, retries=None):
	"""Run fn() up to `retries` times with backoff (defaults to RETRIES).

	_meetup_url_to_json returns an HTTP status int (instead of raising) on a
	non-200 response, so that shape is treated as a failed attempt too.
	Returns (value, None) once fn succeeds, else (last_value, last_error).
	"""
	if retries is None:
		retries = RETRIES
	value, error = None, None
	for attempt in range(1, retries + 1):
		try:
			value = fn()
			error = None
			if not isinstance(value, int):
				return value, None
			error = RuntimeError(f"HTTP {value}")
		except Exception as ex:
			value, error = None, ex
		print(f"  [{label}] attempt {attempt}/{retries} failed: {error!r}")
		if attempt < retries:
			time.sleep(RETRY_DELAY * attempt)
	return value, error


def classify(label, value, error):
	"""Map a failed fetch to ('inconclusive'|'broken', detail).

	Network-level failures and upstream 5xx are inconclusive (this box or
	meetup.com having a bad day); non-200 statuses and parse errors mean the
	scrape no longer matches the live site and need a code fix.
	"""
	status = value if isinstance(value, int) else None
	if status is None and isinstance(error, requests.exceptions.HTTPError) \
			and getattr(error, 'response', None) is not None:
		status = error.response.status_code
	if status is not None:
		if 500 <= status < 600:
			return 'inconclusive', f"{label}: HTTP {status} (upstream error)"
		return 'broken', f"{label}: HTTP {status}"
	if isinstance(error, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
		return 'inconclusive', f"{label}: {error!r}"
	return 'broken', f"{label}: {error!r}"


def parse_dt(value):
	try:
		return dt.datetime.fromisoformat(value)
	except (TypeError, ValueError):
		return None


def _aware_dt(value):
	"""Parse a meetup ISO datetime; None unless it is timezone-aware."""
	parsed = parse_dt(value)
	if parsed is not None and parsed.tzinfo is not None:
		return parsed
	return None


def _clean_id(value):
	"""Event id as int, matching production's int(id, base=10) parsing."""
	try:
		return int(value, base=10)
	except (TypeError, ValueError):
		return None


def _is_upcoming(entry, now):
	if entry.get('status') != 'ACTIVE':
		return False
	when = _aware_dt(entry.get('dateTime'))
	return when is not None and when > now


def upcoming_ids(payload, now):
	ids = []
	for entry in payload:
		if not _is_upcoming(entry, now):
			continue
		entry_id = _clean_id(entry.get('id'))
		if entry_id is not None:
			ids.append(entry_id)
	return sorted(ids)


def validate_event_obj(event):
	"""List of problems for an event built by update_event_from_json.

	These are exactly the fields main.update_events feeds to Discord, so
	anything listed here would corrupt or block the production sync.
	"""
	probs = []
	if not (isinstance(event.title, str) and event.title.strip()):
		probs.append('title')
	start = event.start_time
	if not start or start <= dt.datetime.now(shared.shared.central_time):
		probs.append('start_time')
	if not event.endtime or (start and event.endtime < start):
		probs.append('endtime')
	if not (isinstance(event.link, str) and event.link.startswith('https://www.meetup.com/')):
		probs.append('link')
	if not event.full_description:
		probs.append('full_description')
	if not (isinstance(event.description, str) and 0 < len(event.description) <= 999):
		probs.append('description')
	if event.online:
		if event.location != 'Online':
			probs.append('location')
	else:
		if not (isinstance(event.location, str) and 0 < len(event.location) <= 99):
			probs.append('location')
	if start and event.timestamp != int(start.timestamp() * 1000):
		probs.append('timestamp_ms')
	return probs


def stage_list_page(now):
	"""1/4: the raw events list payload fetch_meetup_events consumes.

	Returns (payload or None, expected upcoming ids, status) where status is
	ok / broken / inconclusive.
	"""
	print("== 1/4  events list page (the payload production consumes)")
	payload, error = fetch_retry("events list page", lambda: events._meetup_url_to_json(EVENTS_URL))
	if error is not None:
		kind, detail = classify("events list page", payload, error)
		record_fetch_failure(kind, detail)
		print(f"  {'FAIL' if kind == 'broken' else 'NOTE'}  {detail}")
		return None, [], kind

	results = [
		check("events page returns a list of entries", isinstance(payload, list),
			f"got {type(payload).__name__}"),
	]
	if not all(results):
		return None, [], 'broken'

	bad_ids = [e.get('id') for e in payload if _clean_id(e.get('id')) is None]
	bad_dates = [e.get('id') for e in payload if _aware_dt(e.get('dateTime')) is None]
	missing_keys = [e.get('id') for e in payload
			if not {'title', 'eventUrl', 'eventType', 'dateTime', 'endTime', 'status', 'description'} <= set(e)]
	missing_venue = [e.get('id') for e in payload
			if _is_upcoming(e, now) and e.get('eventType') != 'ONLINE'
			and not (isinstance(e.get('venue'), dict) and {'name', 'address', 'city'} <= set(e['venue']))]
	checks = [
		check("all entries have numeric string ids", not bad_ids, f"{len(bad_ids)} bad: {_short(bad_ids)}"),
		check("all entries have timezone-aware dateTime values", not bad_dates, f"{len(bad_dates)} bad: {_short(bad_dates)}"),
		check("all entries carry the keys update_event_from_json reads", not missing_keys,
			f"missing on: {_short(missing_keys)}"),
		check("upcoming in-person entries have venue name/address/city", not missing_venue,
			_short(missing_venue)),
	]
	if not all(checks):
		return payload, [], 'broken'

	expected_ids = upcoming_ids(payload, now)
	if not expected_ids:
		INCONCLUSIVE.append("no upcoming ACTIVE events on the page to fetch -- either none are "
			"scheduled or the filter no longer matches the payload")
		return payload, [], 'inconclusive'
	print(f"  (page lists {len(payload)} entries; {len(expected_ids)} upcoming ACTIVE)")
	return payload, expected_ids, 'ok'


def stage_production_fetch(now, expected_ids, expected_known):
	"""2/4: fetch_meetup_events() over the live site (persistence stubbed)."""
	print("== 2/4  fetch_meetup_events() -- the production path, persistence stubbed")
	fetched, error = fetch_retry("fetch_meetup_events", events.fetch_meetup_events)
	if error is not None:
		kind, detail = classify("fetch_meetup_events", fetched, error)
		record_fetch_failure(kind, detail)
		print(f"  {'FAIL' if kind == 'broken' else 'NOTE'}  {detail}")
		return []

	ids = sorted(e.sort for e in fetched)
	if not expected_known:
		print("  (skipping expected-set comparison: the list page checks above already failed)")
	elif ids != expected_ids:
		# either an event changed between the two live fetches (rare) or a
		# processing bug dropped events; re-derive the expectation from a
		# fresh page fetch before declaring failure
		fresh, ferror = fetch_retry("events list page (recheck)",
				lambda: events._meetup_url_to_json(EVENTS_URL))
		if ferror is None and isinstance(fresh, list):
			fresh_ids = upcoming_ids(fresh, now)
			if ids == fresh_ids:
				print("  NOTE  event set changed between the two fetches; the returned set matches the fresh page")
			else:
				check("fetch_meetup_events returns the upcoming ACTIVE set", False,
					f"returned {len(ids)}, expected {len(fresh_ids)}; "
					f"missing={sorted(set(fresh_ids) - set(ids))} extra={sorted(set(ids) - set(fresh_ids))}")
		else:
			check("fetch_meetup_events returns the upcoming ACTIVE set", False,
				f"returned {len(ids)}, expected {len(expected_ids)}; "
				f"missing={sorted(set(expected_ids) - set(ids))} extra={sorted(set(ids) - set(expected_ids))}")
	else:
		check("fetch_meetup_events returns the upcoming ACTIVE set", True,
			f"{len(ids)} events, exact match")

	check("every returned event went through the stubbed save", len(store) == len(fetched),
		f"store={len(store)} returned={len(fetched)}")
	bad = {}
	for event in fetched:
		probs = validate_event_obj(event)
		if probs:
			bad[event.sort] = probs
	check("every returned event maps to valid Discord/ddb fields", not bad, _short(bad))
	check("created_at stamped on new events", all(e.created_at is not None for e in fetched))
	print(f"  (fetched {len(fetched)} events, {len(store)} stubbed saves, 0 real writes)")
	return fetched


def stage_event_pages(now, expected_ids, list_payload):
	"""3/4: single-event page scrape (the cancellation-recheck path)."""
	print("== 3/4  single-event page scrape (check_existing_event path)")
	upcoming = set(expected_ids)
	sample_ids = list(dict.fromkeys(expected_ids[:1] + expected_ids[-1:]))
	if not sample_ids and isinstance(list_payload, list):
		# the list payload failed its shape checks; still validate the page
		# scrape against any entry we have
		for entry in list_payload:
			fallback = _clean_id(entry.get('id'))
			if fallback is not None:
				sample_ids = [fallback]
				break
	if not sample_ids:
		check("an event page could be sampled", False, "no usable event ids in the list payload")
		return

	for entry_id in sample_ids:
		url = EVENT_PAGE_URL.format(entry_id)
		payload, error = fetch_retry(f"event {entry_id} page", lambda u=url: events._meetup_url_to_json(u))
		if error is not None:
			kind, detail = classify(f"event {entry_id} page", payload, error)
			record_fetch_failure(kind, detail)
			print(f"  {'FAIL' if kind == 'broken' else 'NOTE'}  {detail}")
			continue
		entry = payload
		if isinstance(entry, list):
			# the single-event page may serve the events-list apollo layout
			# instead of pageProps.event (see check_existing_event)
			matches = [e for e in entry if isinstance(e, dict) and str(e.get('id')) == str(entry_id)]
			entry = matches[0] if matches else None
		if not check(f"event {entry_id} page parses to an event payload",
				isinstance(entry, dict) and 'status' in entry, _short(entry)):
			continue
		if entry_id in upcoming:
			check(f"event {entry_id} is still ACTIVE on its page",
				entry.get('status') == 'ACTIVE', f"status={entry.get('status')!r}")
		try:
			event = events.MeetupEvent(sort=entry_id)
			events.update_event_from_json(event, entry)
			probs = validate_event_obj(event)
			check(f"event {entry_id} maps to a valid payload", not probs, _short(probs))
		except Exception as ex:
			check(f"event {entry_id} maps to a valid payload", False, repr(ex))


def stage_rss():
	"""4/4: the events/rss feed + one per-item page (fetch_meetup_events_rss path)."""
	print("== 4/4  RSS feed + per-item page")
	def rss_get():
		resp = events.requests.get(RSS_URL)
		resp.raise_for_status()
		return resp
	resp, error = fetch_retry("rss feed", rss_get)
	if error is not None:
		kind, detail = classify("rss feed", resp, error)
		record_fetch_failure(kind, detail)
		print(f"  {'FAIL' if kind == 'broken' else 'NOTE'}  {detail}")
		return
	try:
		items = events.xml_to_dict(resp.text)['rss']['channel']['item']
	except Exception as ex:
		check("rss feed parses to rss/channel/item", False, repr(ex))
		return
	if not check("rss feed parses to rss/channel/item", isinstance(items, list), _short(items)):
		return
	check("rss feed lists at least one item", len(items) >= 1, f"{len(items)} items")
	bad_guids = [it.get('guid') for it in items if not events.guid_finder.match(str(it.get('guid')))]
	if not check("every rss item guid matches the event-URL pattern", not bad_guids, _short(bad_guids)):
		return

	guid = int(events.guid_finder.match(items[0]['guid']).group(1))
	link = items[0]['link']
	payload, error = fetch_retry("rss item page", lambda: events._meetup_url_to_json(link))
	if error is not None:
		kind, detail = classify("rss item page", payload, error)
		record_fetch_failure(kind, detail)
		print(f"  {'FAIL' if kind == 'broken' else 'NOTE'}  {detail}")
		return
	entry = payload
	if isinstance(entry, list):
		matches = [e for e in entry if isinstance(e, dict) and str(e.get('id')) == str(guid)]
		entry = matches[0] if matches else None
	check("rss item link scrapes to its event payload", isinstance(entry, dict) and 'status' in entry,
		_short(entry))


def finish():
	print("\n==== RESULTS ====")
	print(f"{len(PASS)} passed, {len(FAIL)} failed"
		+ (f", {len(INCONCLUSIVE)} inconclusive" if INCONCLUSIVE else ""))
	for name, detail in FAIL:
		print(f"FAIL  {name}" + (f" | {detail}" if detail else ""))
	if FAIL:
		print(f"RESULT: SOME FAILED -- the live meetup fetch is broken (run took {time.monotonic() - START:.1f}s)")
		return 1
	if INCONCLUSIVE:
		for detail in INCONCLUSIVE:
			print(f"INCONCLUSIVE  {detail}")
		print(f"RESULT: INCONCLUSIVE -- check this box / meetup.com before assuming code breakage (run took {time.monotonic() - START:.1f}s)")
		return 2
	print(f"RESULT: ALL PASSED (run took {time.monotonic() - START:.1f}s)")
	return 0


def main():
	print(f"live_meetup_test: LIVE run against {EVENTS_URL}")
	print(f"(retries: {RETRIES} per request, backoff {RETRY_DELAY}s, timeout {REQUEST_TIMEOUT}s)\n")
	now = dt.datetime.now(shared.shared.central_time)
	list_payload, expected_ids, status = stage_list_page(now)
	if status == 'inconclusive':
		print("\n  (skipping the remaining stages)")
		return finish()
	stage_production_fetch(now, expected_ids, status == 'ok')
	stage_event_pages(now, expected_ids, list_payload)
	stage_rss()
	return finish()


if __name__ == '__main__':
	sys.exit(main())
