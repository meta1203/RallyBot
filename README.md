# RallyBot

A Discord bot that automatically syncs events from Meetup to Discord scheduled events, plus a lightweight user-report / moderation workflow. Built for (and hardcoded to) the [chicago-anime-hangouts](https://www.meetup.com/chicago-anime-hangouts) Meetup group.

## Features

### Event sync
- **Automatic event synchronization**: fetches events from the Meetup group's RSS feed, scrapes each event page for its embedded JSON payload, and creates corresponding scheduled events in Discord
- **Automatic updates**: when event details change on Meetup, the corresponding Discord event is updated (title, description, start/end times, location)
- **Cancellation handling**: events that disappear from Meetup (or are no longer active) are removed from both DynamoDB and Discord; events deleted on Discord are detected and recreated
- **AI categorization**: new events are classified into one of nine categories (book club, conventions, food, gaming, karaoke, outdoor, watch party, volunteering, other) using a DigitalOcean AI inference endpoint; the category decides which channel gets the announcement
- **Announcements & reminders**: new and updated events are announced in the matching category channel with the appropriate role ping (online vs. in-person). In-person events get a "tomorrow!" reminder ~24h before start, online events a "starts soon!" reminder ~1-2h before start
- **Long descriptions** are truncated to fit Discord's limits, with a link back to the full Meetup event

### Moderation
- **Report context menu**: a "Report..." message command lets any user report a message with a reason
- **Moderator action buttons**: reports are posted to the mod channel with Ignore / Timeout / Ban buttons, restricted to members with timeout/ban permissions
- **Escalating timeouts**: timeout duration doubles with each prior actioned report (`2 * 2^n` hours, capped at Discord's 28-day maximum)
- **Confirm-to-ban**: banning requires typing CONFIRM in a follow-up modal

## How It Works

1. **Event fetching** (`events.py`): the bot fetches the RSS feed for the configured Meetup group, then for each item fetches the event page and extracts the full event JSON from the embedded `__NEXT_DATA__` script tag
2. **Persistence** (`aws.py`): events and reports live in a single DynamoDB table, accessed through the PynamoDB ORM. Each Meetup event is stored under its Meetup guid (sort key) alongside the Discord snowflake id it was published as, so updates and cancellations can be reconciled across both platforms
3. **Discord integration** (`main.py`): creates new scheduled events, edits changed ones, deletes cancelled ones, and posts announcements to the right channels
4. **Reporting** (`report.py`): context-menu reports are stored in DynamoDB and surfaced to moderators with action buttons

### Schedules
- Event sync runs once at startup, then daily at 12:30 PM (America/Chicago)
- Notification/reminder checks run hourly, on the hour

## Requirements

- Python 3.10+ (the Docker image uses 3.13)
- A Discord bot token with permission to manage scheduled events, send messages, and moderate members
- AWS credentials with DynamoDB access
- A DynamoDB table named `RallyBot` in `us-east-2` with:
  - partition key `id` (string) and sort key `sort` (number)
  - a global secondary index `timestamp-index` keyed on `timestamp` (number)
  - a global secondary index `snowflake_id-index` keyed on `snowflake_id` (number)
- Dependencies from requirements.txt: discord.py, requests, beautifulsoup4, lxml, apscheduler, boto3, pynamodb

## Docker Setup

### Building the Docker Image

```bash
docker build -t rallybot .
```

### Running the Container

Run the container with (see the environment variables section below for the full list):

```bash
docker run -d \
  -e DISCORD_TOKEN=your_discord_token \
  -e AWS_ACCESS_KEY_ID=your_aws_access_key \
  -e AWS_SECRET_ACCESS_KEY=your_aws_secret_key \
  --name rallybot \
  rallybot
```

### Viewing Logs

```bash
docker logs -f rallybot
```

### Stopping the Container

```bash
docker stop rallybot
```

## Environment Variables

Required:

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Discord bot token |
| `AWS_ACCESS_KEY_ID` | AWS access key for DynamoDB access |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key for DynamoDB access |

Optional:

| Variable | Purpose |
| --- | --- |
| `AWS_SESSION_TOKEN` | AWS session token, if using temporary credentials |
| `DO_AI_ENDPOINT` | DigitalOcean AI inference endpoint URL, used for event categorization |
| `DO_AI_SECRET` | Secret for the DigitalOcean AI endpoint |
| `MOD_CHANNEL` | Channel name for report notifications (default: `moderator-only`) |
| `QUIET_RALLY` | Set to any non-empty value to run in silent mode (everything logs, but nothing is actually sent to Discord) |

If `DO_AI_ENDPOINT`/`DO_AI_SECRET` are unset, events are simply categorized as `other`.

## Development

### Local Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables:
   ```bash
   export DISCORD_TOKEN=your_discord_token
   export AWS_ACCESS_KEY_ID=your_aws_access_key
   export AWS_SECRET_ACCESS_KEY=your_aws_secret_key
   ```

3. Run the bot:
   ```bash
   python main.py
   ```

`events.py` can also be run directly (`python events.py`) as a debug harness that rechecks every tracked event against Meetup.

### Configuration

The bot is currently hardcoded to:

- Connect to the Discord guild with ID `1219601473948614737` (`main.py`)
- Fetch events from the `chicago-anime-hangouts` Meetup group (`events.py`)
- Store data in a DynamoDB table named `RallyBot` in `us-east-2` (`aws.py`)
- Ping the in-person / online / moderator roles by ID (`main.py`, `report.py`)

To modify these settings, update the relevant values in the code.
