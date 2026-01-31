#!/usr/bin/env python3
"""
Slack Bot that sends daily channel summaries at 7am JST.
Summarizes the last 24 hours of messages, translating Japanese content,
and provides an executive summary of the 3 most important points.
"""

import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import schedule
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Load environment variables
load_dotenv()

# Configuration
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Timezone
JST = ZoneInfo("Asia/Tokyo")


def get_slack_client() -> WebClient:
    """Initialize and return Slack client."""
    if not SLACK_BOT_TOKEN:
        raise ValueError("SLACK_BOT_TOKEN environment variable is not set")
    return WebClient(token=SLACK_BOT_TOKEN)


def get_anthropic_client() -> anthropic.Anthropic:
    """Initialize and return Anthropic client."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def fetch_messages_last_24h(client: WebClient, channel_id: str) -> list[dict]:
    """
    Fetch all messages from the last 24 hours in the specified channel,
    including thread replies.
    
    Args:
        client: Slack WebClient instance
        channel_id: The Slack channel ID to fetch messages from
        
    Returns:
        List of message dictionaries with timestamp and text
    """
    messages = []
    now = datetime.now(JST)
    oldest = (now - timedelta(hours=24)).timestamp()
    
    def parse_message(msg: dict, thread_id: str | None = None) -> dict | None:
        """Parse a single message into our format."""
        # Skip bot messages and system messages
        if msg.get("subtype") in ["bot_message", "channel_join", "channel_leave"]:
            return None
            
        user_id = msg.get("user", "Unknown")
        text = msg.get("text", "")
        ts = msg.get("ts", "")
        
        if not text:
            return None
            
        msg_time = datetime.fromtimestamp(float(ts), tz=JST)
        return {
            "user": user_id,
            "text": text,
            "timestamp": msg_time.strftime("%Y-%m-%d %H:%M JST"),
            "thread_id": thread_id,
            "ts": ts  # Keep for sorting
        }
    
    def fetch_thread_replies(thread_ts: str) -> list[dict]:
        """Fetch all replies in a thread."""
        replies = []
        cursor = None
        
        while True:
            response = client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                oldest=str(oldest),
                limit=200,
                cursor=cursor
            )
            
            for msg in response.get("messages", []):
                # Skip the parent message (it's included in replies response)
                if msg.get("ts") == thread_ts:
                    continue
                    
                parsed = parse_message(msg, thread_id=thread_ts)
                if parsed:
                    replies.append(parsed)
            
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
                
        return replies
    
    try:
        cursor = None
        threads_to_fetch = []
        
        while True:
            response = client.conversations_history(
                channel=channel_id,
                oldest=str(oldest),
                limit=200,
                cursor=cursor
            )
            
            for msg in response.get("messages", []):
                parsed = parse_message(msg)
                if parsed:
                    messages.append(parsed)
                
                # Check if this message has thread replies
                if msg.get("reply_count", 0) > 0:
                    threads_to_fetch.append(msg.get("ts"))
            
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        
        # Fetch all thread replies
        for thread_ts in threads_to_fetch:
            replies = fetch_thread_replies(thread_ts)
            messages.extend(replies)
                
    except SlackApiError as e:
        print(f"Error fetching messages: {e.response['error']}")
        raise
    
    # Sort by timestamp and return in chronological order
    messages.sort(key=lambda m: m["ts"])
    
    # Remove the ts field (only needed for sorting)
    for msg in messages:
        del msg["ts"]
    
    return messages


def resolve_user_names(client: WebClient, messages: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """
    Replace user IDs with display names for better readability.
    Also replaces <@USER_ID> mentions in message text with display names.
    
    Args:
        client: Slack WebClient instance
        messages: List of message dictionaries
        
    Returns:
        Tuple of (messages with user IDs replaced by names, name-to-ID mapping)
    """
    user_cache = {}  # user_id -> display_name
    
    def get_display_name(user_id: str) -> str:
        """Look up and cache a user's display name."""
        if user_id not in user_cache:
            try:
                response = client.users_info(user=user_id)
                user_info = response.get("user", {})
                # Prefer display name, fall back to real name, then username
                user_cache[user_id] = (
                    user_info.get("profile", {}).get("display_name") or
                    user_info.get("real_name") or
                    user_info.get("name") or
                    user_id
                )
            except SlackApiError:
                user_cache[user_id] = user_id
        return user_cache[user_id]
    
    def replace_user_mentions(text: str) -> str:
        """Replace <@USER_ID> mentions in text with display names."""
        def replace_match(match):
            user_id = match.group(1)
            return f"@{get_display_name(user_id)}"
        return re.sub(r'<@([A-Z0-9]+)>', replace_match, text)
    
    for msg in messages:
        # Replace the user field with display name
        msg["user"] = get_display_name(msg["user"])
        # Replace any <@USER_ID> mentions in the message text
        msg["text"] = replace_user_mentions(msg["text"])
    
    # Create reverse mapping: name -> user_id (for @mention replacement in output)
    # Include lowercase versions for case-insensitive matching
    name_to_id = {}
    for user_id, name in user_cache.items():
        name_to_id[name.lower()] = user_id
        # Also add first name only for partial matches
        first_name = name.split()[0] if name else ""
        if first_name and first_name.lower() not in name_to_id:
            name_to_id[first_name.lower()] = user_id
    
    return messages, name_to_id


def replace_mentions(text: str, name_to_id: dict[str, str]) -> str:
    """
    Replace @mentions in text with proper Slack user mentions.
    
    Args:
        text: The text containing @mentions
        name_to_id: Mapping of lowercase names to user IDs
        
    Returns:
        Text with @mentions replaced by <@USER_ID> format
    """
    def replace_match(match):
        # Get the name after @ (without the @)
        name = match.group(1)
        name_lower = name.lower()
        
        # Try to find a matching user ID
        if name_lower in name_to_id:
            return f"<@{name_to_id[name_lower]}>"
        
        # No match found, keep original
        return match.group(0)
    
    # Match @Name or @"Name with spaces" patterns
    # This handles: @Justin, @justin, @Justin Garcia (as @Justin)
    pattern = r'@(\w+)'
    return re.sub(pattern, replace_match, text)


def generate_summary(client: anthropic.Anthropic, messages: list[dict]) -> str:
    """
    Use Claude to generate an executive summary of the messages.
    Handles translation of Japanese content automatically.
    
    Args:
        client: Anthropic client instance
        messages: List of message dictionaries
        
    Returns:
        Executive summary string
    """
    if not messages:
        return "📭 No messages in the last 24 hours."
    
    # Format messages for the prompt
    formatted_messages = "\n".join([
        f"[{msg['timestamp']}] {msg['user']}{' (in thread)' if msg.get('thread_id') else ''}: {msg['text']}"
        for msg in messages
    ])
    
    prompt = f"""Analyze these Slack messages from the last 24 hours. Translate any Japanese to English.

{formatted_messages}

Write a tight executive summary: the 3 most important things discussed/decided.

Use Slack mrkdwn format (NOT standard Markdown):
- Bold: *text* (single asterisks)
- Italic: _text_

Format exactly like this:

*Daily Summary* ({len(messages)} messages)

*1. Topic* — One sentence summary
*2. Topic* — One sentence summary
*3. Topic* — One sentence summary

If action items exist, add:
*Action Items:* @person: task; @person: task

Keep it brief. No extra line breaks. English only. Use first names only for @mentions (e.g., @Justin not @Justin Garcia)."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    return response.content[0].text


def post_summary(client: WebClient, channel_id: str, summary: str) -> None:
    """
    Post the summary to the Slack channel.
    
    Args:
        client: Slack WebClient instance
        channel_id: The channel to post to
        summary: The summary text to post
    """
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=summary,
            mrkdwn=True
        )
        print(f"Summary posted successfully at {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    except SlackApiError as e:
        print(f"Error posting message: {e.response['error']}")
        raise


def run_daily_summary() -> None:
    """Main function to fetch messages and post summary."""
    now = datetime.now(JST)
    
    # Skip Sunday (weekday() returns 6 for Sunday)
    if now.weekday() == 6:
        print(f"Skipping summary on Sunday ({now.strftime('%Y-%m-%d %H:%M JST')})")
        return
    
    print(f"Running daily summary at {now.strftime('%Y-%m-%d %H:%M JST')}")
    
    try:
        slack_client = get_slack_client()
        anthropic_client = get_anthropic_client()
        
        if not SLACK_CHANNEL_ID:
            raise ValueError("SLACK_CHANNEL_ID environment variable is not set")
        
        # Fetch messages
        print("Fetching messages from the last 24 hours...")
        messages = fetch_messages_last_24h(slack_client, SLACK_CHANNEL_ID)
        print(f"Found {len(messages)} messages")
        
        # Resolve user names
        name_to_id = {}
        if messages:
            print("Resolving user names...")
            messages, name_to_id = resolve_user_names(slack_client, messages)
        
        # Generate summary
        print("Generating summary with Claude...")
        summary = generate_summary(anthropic_client, messages)
        
        # Replace @mentions with real Slack mentions
        if name_to_id:
            summary = replace_mentions(summary, name_to_id)
        
        # Post to channel
        print("Posting summary to channel...")
        post_summary(slack_client, SLACK_CHANNEL_ID, summary)
        
    except Exception as e:
        print(f"Error running daily summary: {e}")
        raise


def main():
    """Main entry point - schedules the daily summary job."""
    print("🤖 Slack Summary Bot starting...")
    print(f"Current time (JST): {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    print(f"Channel ID: {SLACK_CHANNEL_ID}")
    print("Scheduled to run daily at 07:00 JST")
    print("-" * 40)
    
    # Schedule the job for 7:00 AM JST
    # The schedule library uses the system's local time, so we need to handle timezone conversion
    schedule.every().day.at("07:00").do(run_daily_summary)
    
    # For testing: uncomment the next line to run immediately on startup
    # run_daily_summary()
    
    # Keep the script running
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute


if __name__ == "__main__":
    main()
