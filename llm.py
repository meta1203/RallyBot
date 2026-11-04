import os
import json
import requests
from traceback import format_exc as get_stacktrace

# single-tenant hardcoded constants: one OpenRouter account, one default model;
# the model is env-overridable so it can be swapped without a code change
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'deepseek/deepseek-v4.1-flash')
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# carried over verbatim from the DigitalOcean endpoint's agent config
SYSTEM_PROMPT = "You are an event categorization bot. You will receive an event description, then you will be given a list of categories that the event might fall into. Anything that doesn't explicitly mention a convention is not to be considered a convention event. All \"social\" events are to be considered \"other\" events (this includes \"Acquaintances and Alcohol\" and presentation night). An event is only to be considered \"social\" if it doesn't fit a better category. Reply only with the given category that best fits the event."

categories = ["book club", "conventions", "food", "gaming", "karaoke", "outdoor", "watch party", "volunteering", "other"]

# JSON-mode event extraction for ad hoc forum posts (see adhoc.py). The
# category list is interpolated from `categories` at call time, so it can
# never drift from the categorizer's list.
EXTRACT_SYSTEM_PROMPT = (
	"You are an event extraction bot for a Chicago anime meetup community. "
	"You will be given a Discord forum post (a title line and a body). Extract the event it describes. "
	"Reply ONLY with a single JSON object, no markdown fences, with exactly these keys:\n"
	'{"is_event": <bool — false if the post does not describe an actual event happening at a stated or inferable date/time>, '
	'"title": <string — the event title; use the original post title as-is when it reads as an event title, otherwise derive one>, '
	'"start": <string|null — start date/time as ISO 8601 WITH timezone offset; when the post gives no timezone, assume America/Chicago; null if unknown>, '
	'"end": <string|null — end date/time, same format, null if unknown>, '
	'"duration_minutes": <int|null — event duration in minutes, null if unknown>, '
	'"location": <string|null — real-world venue/address, or "Online"/"Discord" for online events, null if unknown>, '
	'"online": <bool>, '
	'"description": <string — the event description; prefer the post\'s own wording>, '
	'"category": <one of: {CATEGORIES}>, '
	'"missing": <list — which of "start" and/or "location" could not be determined; empty list when both are known>}'
)
# the extraction floor: a record is only created when both are known
EXTRACT_REQUIRED_FIELDS = ("start", "location")


def _parse_json_object(raw: str) -> dict | None:
	"""Best-effort parse of a JSON object out of an LLM reply (strips code
	fences and surrounding prose). Returns None when nothing parses."""
	text = (raw or "").strip()
	if text.startswith("```"):
		first_newline = text.find("\n")
		if first_newline != -1:
			text = text[first_newline + 1:]
		if text.rstrip().endswith("```"):
			text = text.rstrip()[:-3]
	text = text.strip()
	try:
		parsed = json.loads(text)
		return parsed if isinstance(parsed, dict) else None
	except Exception:
		pass
	# fallback: slice the outermost {...} out of surrounding prose
	start, end = text.find("{"), text.rfind("}")
	if start != -1 and end > start:
		try:
			parsed = json.loads(text[start:end + 1])
			return parsed if isinstance(parsed, dict) else None
		except Exception:
			return None
	return None


def ai_extract_event(post_title: str, post_body: str) -> dict | None:
	"""Blocking extraction of event info from an ad hoc forum post. Returns
	the parsed JSON dict, or None when the AI call or JSON parsing fails —
	that is a TRANSIENT failure; callers must not treat it as missing
	information. A returned dict may still lack the floor fields: validate
	start/location (EXTRACT_REQUIRED_FIELDS) before trusting it. Always call
	via asyncio.to_thread from async code."""
	if not OPENROUTER_API_KEY:
		print("OPENROUTER_API_KEY not set; cannot extract event info from forum posts.")
		return None
	messages = [
		{
			"role": "system",
			"content": EXTRACT_SYSTEM_PROMPT.replace("{CATEGORIES}", ", ".join(categories)),
		},
		{
			"role": "user",
			"content": f"Post title: {post_title}\n\nPost body:\n{post_body}",
		},
	]
	for _attempt in range(2):
		try:
			raw = chat(messages)
		except Exception:
			print(f"ERROR: AI extraction call failed:\n{get_stacktrace()}")
			return None
		parsed = _parse_json_object(raw)
		if parsed is not None:
			return parsed
		print("AI extraction returned unparseable output, retrying...")
		messages.append({"role": "assistant", "content": raw})
		messages.append({
			"role": "user",
			"content": "That is not valid JSON. Reply again with ONLY the JSON object, no markdown fences or other text.",
		})
	return None


def chat(messages: list[dict], timeout: int = 60) -> str:
	"""Single blocking chat completion against OpenRouter; returns the
	assistant message content. Always call via asyncio.to_thread from async
	code — this stalls the discord.py heartbeat if run on the event loop.
	Raises on missing key / HTTP errors (unlike ai_categorize, which degrades)."""
	if not OPENROUTER_API_KEY:
		raise RuntimeError("OPENROUTER_API_KEY is not set")
	headers = {
		"Content-Type": "application/json",
		"Authorization": f"Bearer {OPENROUTER_API_KEY}",
		"X-Title": "RallyBot",
	}
	payload = {
		"model": OPENROUTER_MODEL,
		"messages": messages,
	}
	response = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=timeout)
	response.raise_for_status()
	return response.json()['choices'][0]['message']['content']


def ai_categorize(description: str) -> str:
	if not OPENROUTER_API_KEY:
		print("OPENROUTER_API_KEY not set, defaulting to 'other' category.")
		return 'other'
	messages = [
		{
			"role": "system",
			"content": SYSTEM_PROMPT
		},
		{
			"role": "user",
			"content": f"{description}\n\nCategories: {', '.join(categories)}"
		}
	]
	cat = chat(messages).strip().lower()
	if cat not in categories:
		messages.append({"role": "assistant", "content": cat})
		messages.append({
			"role": "user",
			"content": f"{cat} is not a valid answer. select the best category from the following list: {', '.join(categories)}"
		})
		print(f"invalid category {cat}, retrying...")
		cat = chat(messages).strip().lower()
		if cat not in categories:
			# failed again, just use the "other" category
			cat = "other"
	return cat
