FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create a non-root user
RUN adduser --disabled-password --gecos "" appuser

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install lxml parser for BeautifulSoup
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev \
    libxslt-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY main.py events.py aws.py ./

# Switch to non-root user
USER appuser

# Command to run the application
CMD ["python", "main.py"]

# Document required environment variables
# Required environment variables:
# - DISCORD_TOKEN: Discord bot token
# - AWS_ACCESS_KEY_ID: AWS access key for DynamoDB
# - AWS_SECRET_ACCESS_KEY: AWS secret key for DynamoDB
# Optional:
# - AWS_SESSION_TOKEN: AWS session token (if using temporary credentials)
