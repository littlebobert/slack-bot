# Slack Daily Summary Bot

A Python bot that automatically summarizes your Slack channel activity every day at 7:00 AM JST. It handles Japanese messages by translating them and provides an executive summary of the 3 most important points.

## Features

- 📅 Runs automatically at 7:00 AM JST every day
- 📊 Summarizes the last 24 hours of channel activity
- 🇯🇵 Translates Japanese messages to English
- 📝 Provides top 3 key points as an executive summary
- ⚡ Identifies action items and deadlines

## Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click "Create New App" > "From scratch"
3. Name your app (e.g., "Daily Summary Bot") and select your workspace

### 2. Configure Bot Permissions

1. In your app settings, go to "OAuth & Permissions"
2. Under "Scopes" > "Bot Token Scopes", add:
   - `channels:history` - Read messages in public channels
   - `channels:read` - View basic channel info
   - `chat:write` - Post messages
   - `users:read` - View user profiles (for display names)

3. If you need to read private channels, also add:
   - `groups:history`
   - `groups:read`

### 3. Install the App

1. Go to "Install App" in the sidebar
2. Click "Install to Workspace"
3. Copy the "Bot User OAuth Token" (starts with `xoxb-`)

### 4. Invite the Bot to Your Channel

In Slack, go to the channel you want to summarize and type:
```
/invite @YourBotName
```

### 5. Get the Channel ID

1. Right-click on the channel name in Slack
2. Click "View channel details"
3. Scroll to the bottom - the Channel ID starts with `C`

### 6. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_CHANNEL_ID=C0123456789
ANTHROPIC_API_KEY=sk-ant-your-api-key
```

### 7. Install Dependencies

```bash
pip install -r requirements.txt
```

### 8. Run the Bot

```bash
python bot.py
```

The bot will run continuously and post summaries at 7:00 AM JST every day.

## Testing

To test immediately without waiting for 7 AM, uncomment this line in `bot.py`:

```python
# run_daily_summary()
```

Or run a one-time summary manually:

```python
from bot import run_daily_summary
run_daily_summary()
```

## Running as a Service

### Using Heroku

1. **Install the Heroku CLI** and login:
   ```bash
   heroku login
   ```

2. **Create a new Heroku app**:
   ```bash
   heroku create your-slack-bot-name
   ```

3. **Set the timezone** (important for 7 AM JST scheduling):
   ```bash
   heroku config:set TZ=Asia/Tokyo
   ```

4. **Set your environment variables**:
   ```bash
   heroku config:set SLACK_BOT_TOKEN=xoxb-your-token
   heroku config:set SLACK_CHANNEL_ID=C0123456789
   heroku config:set ANTHROPIC_API_KEY=sk-ant-your-key
   ```

5. **Deploy**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git push heroku main
   ```

6. **Scale up the worker dyno**:
   ```bash
   heroku ps:scale worker=1
   ```

7. **Check logs**:
   ```bash
   heroku logs --tail
   ```

> **Note**: Heroku's free tier has been discontinued. The "Eco" dyno ($5/month) works well for this bot. The worker dyno runs 24/7 and won't sleep like web dynos.

### Using systemd (Linux)

Create `/etc/systemd/system/slack-summary-bot.service`:

```ini
[Unit]
Description=Slack Daily Summary Bot
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/slack-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10
Environment=TZ=Asia/Tokyo

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable slack-summary-bot
sudo systemctl start slack-summary-bot
```

### Using Docker

Create a `Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TZ=Asia/Tokyo
CMD ["python", "bot.py"]
```

Build and run:
```bash
docker build -t slack-summary-bot .
docker run -d --env-file .env slack-summary-bot
```

## Example Output

```
📊 **Daily Channel Summary**
_Last 24 hours (47 messages)_

**Top 3 Key Points:**

1. **Product Launch**: The team confirmed the v2.0 release date for next Friday. Marketing materials are ready and the staging environment passed QA.

2. **API Migration**: Backend team completed the migration to the new authentication system. All services are now using OAuth 2.0.

3. **Customer Feedback**: Three priority bugs were identified from user feedback. Assigned to the frontend team for resolution by Wednesday.

**⚡ Action Items:**
- @sarah: Finalize release notes by Thursday
- @dev-team: Fix the checkout flow bug (P1)
- @product: Schedule customer demo for next week
```

## Troubleshooting

### "not_in_channel" error
Make sure you've invited the bot to the channel with `/invite @BotName`

### "missing_scope" error
Check that all required OAuth scopes are added and the app is reinstalled

### No messages found
- Verify the channel ID is correct
- Check that messages exist within the last 24 hours
- Ensure bot has `channels:history` scope

## License

MIT
