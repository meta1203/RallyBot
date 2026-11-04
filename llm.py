import os
import requests

# single-tenant hardcoded constants: one OpenRouter account, one default model;
# the model is env-overridable so it can be swapped without a code change
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'deepseek/deepseek-v4.1-flash')
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# carried over verbatim from the DigitalOcean endpoint's agent config
SYSTEM_PROMPT = "You are an event categorization bot. You will receive an event description, then you will be given a list of categories that the event might fall into. Anything that doesn't explicitly mention a convention is not to be considered a convention event. All \"social\" events are to be considered \"other\" events (this includes \"Acquaintances and Alcohol\" and presentation night). An event is only to be considered \"social\" if it doesn't fit a better category. Reply only with the given category that best fits the event."

categories = ["book club", "conventions", "food", "gaming", "karaoke", "outdoor", "watch party", "volunteering", "other"]


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
