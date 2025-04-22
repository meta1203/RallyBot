# RallyBot

A Discord bot that automatically syncs events from Meetup to Discord scheduled events. This bot helps community managers keep their Discord server's event calendar in sync with their Meetup group's events without manual intervention.

## Features

- **Automatic Event Synchronization**: Fetches events from a Meetup group's RSS feed and creates corresponding scheduled events in Discord
- **Persistent Storage**: Uses AWS DynamoDB to store event data and track the mapping between Meetup events and Discord events
- **Automatic Updates**: When events on Meetup are updated, the bot automatically updates the corresponding Discord events
- **Scheduled Execution**: Runs daily at midnight to check for new or updated events
- **Event Details Preservation**: Maintains event titles, descriptions, start times, and locations between platforms

## Docker Setup

### Building the Docker Image

To build the Docker image:

```bash
docker build -t rallybot .
```

### Running the Container

The bot requires several environment variables to function properly:

- `DISCORD_TOKEN`: Your Discord bot token
- `AWS_ACCESS_KEY_ID`: AWS access key for DynamoDB access
- `AWS_SECRET_ACCESS_KEY`: AWS secret key for DynamoDB access
- `AWS_SESSION_TOKEN`: (Optional) AWS session token if using temporary credentials

Run the container with:

```bash
docker run -d \
  -e DISCORD_TOKEN=your_discord_token \
  -e AWS_ACCESS_KEY_ID=your_aws_access_key \
  -e AWS_SECRET_ACCESS_KEY=your_aws_secret_key \
  --name rallybot \
  rallybot
```

### Viewing Logs

To view the logs from the running container:

```bash
docker logs -f rallybot
```

### Stopping the Container

To stop the running container:

```bash
docker stop rallybot
```

## How It Works

1. **Event Fetching**: The bot fetches events from the Meetup RSS feed for the configured group (currently set to "chicago-anime-hangouts")
2. **Data Processing**: For each event, it:
   - Extracts event details (title, description, link, date/time, location)
   - Truncates long descriptions to fit Discord's limits, adding a link to the full event
   - Stores the event data in DynamoDB
3. **Discord Integration**: The bot then:
   - Creates new Discord scheduled events for new Meetup events
   - Updates existing Discord events if details have changed on Meetup
   - Tracks the relationship between Meetup event IDs and Discord event IDs
4. **Scheduling**: The bot runs an update check:
   - Once at startup
   - Daily at midnight via a scheduled job

## Requirements

- Discord Bot Token with proper permissions
- AWS account with DynamoDB access
- Python 3.8+
- Dependencies listed in requirements.txt:
  - discord.py
  - requests
  - beautifulsoup4
  - apscheduler
  - boto3
  - jsonpickle

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

### Configuration

The bot is currently configured to:
- Connect to the Discord guild with ID "1219601473948614737"
- Fetch events from the "chicago-anime-hangouts" Meetup group
- Store data in a DynamoDB table named "RallyBot"

To modify these settings, you'll need to update the relevant values in the code.
