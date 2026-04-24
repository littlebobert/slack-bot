#!/usr/bin/env python3
"""
Slack Bot that sends daily channel summaries at 7am JST.
Summarizes messages since the previous scheduled summary in Japanese
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


def get_summary_window_start(now: datetime) -> datetime:
    """
    Return the start of the summary window in JST.

    Monday summaries include Friday plus the weekend because Saturday and
    Sunday posts are skipped.
    """
    if now.weekday() == 0:
        return now - timedelta(days=3)
    return now - timedelta(days=1)


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


def fetch_messages_for_window(
    client: WebClient,
    channel_id: str,
    start_time: datetime,
) -> list[dict]:
    """
    Fetch all messages from the specified start time in the channel,
    including thread replies.
    
    Args:
        client: Slack WebClient instance
        channel_id: The Slack channel ID to fetch messages from
        start_time: Start of the summary window in JST
        
    Returns:
        List of message dictionaries with timestamp and text
    """
    messages = []
    oldest = start_time.timestamp()
    
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


def generate_summary(
    client: anthropic.Anthropic,
    messages: list[dict],
    window_label: str,
) -> str:
    """
    Use Claude to generate an executive summary of the messages in Japanese.
    
    Args:
        client: Anthropic client instance
        messages: List of message dictionaries
        window_label: Human-readable summary window label
        
    Returns:
        Executive summary string
    """
    if not messages:
        return f"📭 {window_label} のメッセージはありませんでした。"
    
    # Format messages for the prompt
    formatted_messages = "\n".join([
        f"[{msg['timestamp']}] {msg['user']}{' (in thread)' if msg.get('thread_id') else ''}: {msg['text']}"
        for msg in messages
    ])
    
    prompt = f"""以下は {window_label} のSlackメッセージです。

英語や他言語が含まれていても内容を理解し、出力は必ず自然な日本語だけにしてください。翻訳セクションは出さないでください。メッセージ一覧の列挙や引用も禁止です。意味だけを使って要約してください。

{formatted_messages}

議論・決定された内容のうち、重要な3点を簡潔なエグゼクティブサマリーにしてください。

ルール:
- 各項目は必ず1つの独立したトピックだけを扱うこと。無関係な話題を1つにまとめないこと。
- 最も重要な3トピックを選ぶこと。重要度の低い内容は省略してよい。

Slack mrkdwn形式を使うこと:
- 太字: *text* （アスタリスク1つ）
- 斜体: _text_

出力は要約本文のみとし、必ず次の形式に厳密に従ってください（前置きや余計な見出しは禁止）:

*日次サマリー* ({len(messages)}件)

*1. トピック* 1文の要約
*2. トピック* 1文の要約
*3. トピック* 1文の要約

アクションアイテムがある場合のみ、最後に次を追加:
*アクションアイテム:* 人名: タスク; 人名: タスク

簡潔にしてください。余計な改行は禁止。人名はファーストネームのみで書いてください。Slackで通知が飛ばないよう、名前の前に@は絶対につけないでください。"""

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    text = response.content[0].text

    # Defensive cleanup: if the model still outputs a translation/message-log section,
    # drop everything before the actual summary header.
    marker = "*日次サマリー*"
    idx = text.find(marker)
    if idx > 0:
        text = text[idx:]

    return text


def post_summary(client: WebClient, channel_id: str, summary: str) -> str:
    """
    Post the summary to the Slack channel.
    
    Args:
        client: Slack WebClient instance
        channel_id: The channel to post to
        summary: The summary text to post
        
    Returns:
        The timestamp of the posted message (used as thread_ts for replies)
    """
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text=summary,
            mrkdwn=True
        )
        print(f"Summary posted successfully at {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
        return response["ts"]
    except SlackApiError as e:
        print(f"Error posting message: {e.response['error']}")
        raise


def post_english_translation(
    anthropic_client: anthropic.Anthropic,
    slack_client: WebClient,
    channel_id: str,
    summary: str,
    thread_ts: str,
) -> None:
    """Translate the Japanese summary to English and post it as a thread reply."""
    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": (
                    "Translate the following Japanese Slack summary to natural English. "
                    "Keep the same Slack mrkdwn formatting (*bold*, _italic_). "
                    "Output ONLY the translated summary, nothing else.\n\n"
                    f"{summary}"
                ),
            }],
        )
        translation = response.content[0].text.strip()

        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=translation,
            mrkdwn=True,
        )
        print("English translation posted as thread reply")
    except Exception as e:
        print(f"Error posting English translation: {e}")


def run_daily_summary() -> None:
    """Main function to fetch messages and post summary."""
    now = datetime.now(JST)
    
    # Skip weekends (weekday() returns 5 for Saturday, 6 for Sunday)
    if now.weekday() >= 5:
        print(f"Skipping summary on weekend ({now.strftime('%Y-%m-%d %H:%M JST')})")
        return

    window_start = get_summary_window_start(now)
    window_label = (
        f"{window_start.strftime('%Y-%m-%d %H:%M JST')}"
        f"〜{now.strftime('%Y-%m-%d %H:%M JST')}"
    )
    
    print(f"Running daily summary at {now.strftime('%Y-%m-%d %H:%M JST')}")
    
    try:
        slack_client = get_slack_client()
        anthropic_client = get_anthropic_client()
        
        if not SLACK_CHANNEL_ID:
            raise ValueError("SLACK_CHANNEL_ID environment variable is not set")
        
        # Fetch messages
        print(f"Fetching messages from {window_label}...")
        messages = fetch_messages_for_window(
            slack_client,
            SLACK_CHANNEL_ID,
            window_start,
        )
        print(f"Found {len(messages)} messages")
        
        # Resolve user names
        name_to_id = {}
        if messages:
            print("Resolving user names...")
            messages, name_to_id = resolve_user_names(slack_client, messages)
        
        # Generate summary
        print("Generating summary with Claude...")
        summary = generate_summary(anthropic_client, messages, window_label)
        
        # Post to channel
        print("Posting summary to channel...")
        summary_ts = post_summary(slack_client, SLACK_CHANNEL_ID, summary)

        # Post English translation as a thread reply
        print("Posting English translation...")
        post_english_translation(
            anthropic_client, slack_client, SLACK_CHANNEL_ID, summary, summary_ts
        )
        
    except Exception as e:
        print(f"Error running daily summary: {e}")
        raise


def main():
    """Main entry point - schedules the daily summary job."""
    print("🤖 Slack Summary Bot starting...")
    print(f"Current time (JST): {datetime.now(JST).strftime('%Y-%m-%d %H:%M')}")
    print(f"Channel ID: {SLACK_CHANNEL_ID}")
    print("Scheduled to check daily at 07:00 JST (weekends skipped)")
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
