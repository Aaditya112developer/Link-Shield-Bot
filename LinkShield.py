import re
import time
import json
import logging
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
from os import path

from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import (
    ChannelParticipantCreator,
    ChannelParticipantAdmin,
    MessageEntityUrl,
    MessageEntityTextUrl,
    PeerChannel,
    User
)

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# For Security Reason i have hidden my id,hash,token! you can replace with your own!
api_id = "************"
api_hash = "************"
bot_token = "************"

client = TelegramClient("bot", api_id, api_hash).start(bot_token=bot_token)

# Regex patterns
link_regex = re.compile(
    r"(https?:\/\/\S+|www\.\S+|t\.me\/\S+|\b[a-zA-Z0-9.-]+\."
    r"(com|net|org|io|gov|edu|mil|xyz|info|biz|me|tv|cc|us|in|uk|ly|co|ua|ru|de|fr|es|it|nl|pl|cz|se|no|fi|dk|eu)\b)"
)
mention_regex = re.compile(r"(?<!\S)@[a-zA-Z0-9_]+")

# LRU Cache
MAX_CACHE_SIZE = 500
CACHE_TTL = 3600

class LRUCache(OrderedDict):
    def __init__(self, max_size=MAX_CACHE_SIZE, ttl=CACHE_TTL):
        super().__init__()
        self.max_size = max_size
        self.ttl = ttl

    def get(self, key):
        if key in self:
            value, timestamp = self[key]
            if time.time() - timestamp < self.ttl:
                self.move_to_end(key)
                return value
            else:
                del self[key]
        return None

    def put(self, key, value):
        if len(self) >= self.max_size:
            self.popitem(last=False)
        self[key] = (value, time.time())

entity_cache = LRUCache()

# -----------------------------------------------------------------------------
# 1) Persistent Usage Tracking
# -----------------------------------------------------------------------------
USAGE_FILE = "usage_data.json"
GROUP_USAGE_FILE = "group_usage_data.json"

usage_data = {}
group_usage_data = {}
group_member_counts = {}
USAGE_CUTOFF = 30 * 24 * 60 * 60

def load_usage_data():
    global usage_data, group_usage_data
    if path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, "r") as f:
                usage_data = json.load(f)
            usage_data = {int(k): float(v) for k, v in usage_data.items()}
            logging.info("User usage data loaded.")
        except Exception as e:
            logging.exception(f"Error loading usage data: {e}")
            usage_data = {}
    else:
        usage_data = {}
    if path.exists(GROUP_USAGE_FILE):
        try:
            with open(GROUP_USAGE_FILE, "r") as f:
                group_usage_data = json.load(f)
            group_usage_data = {int(k): float(v) for k, v in group_usage_data.items()}
            logging.info("Group usage data loaded.")
        except Exception as e:
            logging.exception(f"Error loading group usage data: {e}")
            group_usage_data = {}
    else:
        group_usage_data = {}

def save_usage_data():
    try:
        with open(USAGE_FILE, "w") as f:
            json.dump(usage_data, f)
        with open(GROUP_USAGE_FILE, "w") as f:
            json.dump(group_usage_data, f)
        logging.info("Usage data saved.")
    except Exception as e:
        logging.exception(f"Error saving usage data: {e}")

def clean_old_usage():
    now = time.time()
    to_remove_users = [uid for uid, last_ts in usage_data.items() if now - last_ts > USAGE_CUTOFF]
    for uid in to_remove_users:
        del usage_data[uid]
    to_remove_groups = [gid for gid, last_ts in group_usage_data.items() if now - last_ts > USAGE_CUTOFF]
    for gid in to_remove_groups:
        del group_usage_data[gid]
        if gid in group_member_counts:
            del group_member_counts[gid]

def update_usage(user_id: int, group_id: int = None):
    usage_data[user_id] = time.time()
    if group_id:
        group_usage_data[group_id] = time.time()

def get_monthly_user_count() -> int:
    clean_old_usage()
    return len(usage_data)

def get_protecting_group_count() -> int:
    clean_old_usage()
    return len(group_usage_data)

async def update_group_member_count(group_id: int):
    try:
        entity = await client.get_entity(group_id)
        if hasattr(entity, "participants_count"):
            member_count = entity.participants_count
        else:
            full_info = await client.get_full_channel(entity)
            member_count = full_info.full_chat.participants_count
        group_member_counts[group_id] = member_count
    except Exception as e:
        logging.exception(f"Error getting member count for group {group_id}: {e}")
        group_member_counts[group_id] = 0

async def get_total_members_monitored() -> int:
    total = 0
    for gid in group_usage_data.keys():
        if gid not in group_member_counts:
            await update_group_member_count(gid)
        total += group_member_counts.get(gid, 0)
    return total

async def periodic_usage_cleanup():
    while True:
        clean_old_usage()
        await asyncio.sleep(600)

async def periodic_usage_save():
    while True:
        save_usage_data()
        await asyncio.sleep(300)

load_usage_data()

# -----------------------------------------------------------------------------
# 2) Helper Functions
# -----------------------------------------------------------------------------
ADMIN_CACHE_TTL = 300
admin_cache = {}

async def is_admin(event) -> bool:
    key = (event.chat_id, event.sender_id)
    now = time.time()
    if key in admin_cache:
        is_admin_cached, ts = admin_cache[key]
        if now - ts < ADMIN_CACHE_TTL:
            return is_admin_cached
    try:
        participant = await client(GetParticipantRequest(event.chat_id, event.sender_id))
        result = isinstance(participant.participant, (ChannelParticipantCreator, ChannelParticipantAdmin))
        admin_cache[key] = (result, now)
        return result
    except Exception as e:
        logging.exception(f"Error checking admin status: {e}")
        admin_cache[key] = (False, now)
        return False

async def block_and_warn(event, warning: str):
    try:
        await event.delete()
    except Exception as e:
        logging.exception(f"Error deleting message: {e}")
    try:
        await event.respond(warning)
    except Exception as e:
        logging.exception(f"Error sending warning message: {e}")

def has_links(event) -> bool:
    if link_regex.search(event.raw_text):
        return True
    if event.message.entities and any(isinstance(e, (MessageEntityUrl, MessageEntityTextUrl)) for e in event.message.entities):
        return True
    return False

async def is_dangerous_mention(username: str) -> bool:
    cached_result = entity_cache.get(username)
    if cached_result is not None:
        return not isinstance(cached_result, User)
    try:
        entity = await client.get_entity(username)
        entity_cache.put(username, entity)
        return not isinstance(entity, User)
    except Exception as e:
        logging.exception(f"Error getting entity for username {username}: {e}")
        entity_cache.put(username, None)
        return True

BOT_OWNER_USERNAME = "@Aaditya_Kr_Sah"

async def is_group_owner_or_admin(event) -> bool:
    if event.is_group:
        if hasattr(event.sender, "username") and event.sender.username == BOT_OWNER_USERNAME:
            return True
        return await is_admin(event)
    return False

# -----------------------------------------------------------------------------
# 3) Commands
# -----------------------------------------------------------------------------
@client.on(events.NewMessage(pattern=r'^/start(@\w+)?$'))
async def start_command(event):
    if event.is_group:
        return
    update_usage(event.sender_id)
    intro_message = (
        "Hello! I’m **Link Shield_bot**.\n\n"
        "🔹 **Step 1:** Add me to your group.\n"
        "🔹 **Step 2:** Promote me to Admin with **Delete messages** permission.\n\n"
        "I automatically delete:\n"
        "• Links or embedded links\n"
        "• Forwarded messages from channels\n"
        "• @username mentions of groups/channels\n\n"
        "Admins are allowed to bypass these rules.\n\n"
        "Use /help or /stats.\n\n"
        "📩 owner: @Aaditya_Kr_Sah\n"
        "🔔 Updates: [Link Shield Updates](https://t.me/linkshield_updates)"
    )
    buttons = [
        [Button.url("Add me to your Group", "https://t.me/"keep ur own bot user name here"_bot?startgroup=new")]
    ]
    await event.respond(intro_message, buttons=buttons, parse_mode="Markdown")

@client.on(events.NewMessage(pattern=r'^/help(@\w+)?$'))
async def help_command(event):
    if event.is_group:
        return
    update_usage(event.sender_id)
    help_text = (
        "**How to Use This Bot**\n\n"
        "1. **Add Bot to Group:** @linkshield_bot\n"
        "2. **Make Bot an Admin:** Enable **Delete messages** permission.\n\n"
        "I will automatically remove prohibited content.\n\n"
        "For support, contact @Aaditya_Kr_Sah."
    )
    await event.respond(help_text)

@client.on(events.NewMessage(pattern=r'^/commands(@\w+)?$'))
async def commands_command(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return
    update_usage(event.sender_id)
    cmd_text = (
        "**Available Commands**\n\n"
        "• `/start` - Intro (DM only)\n"
        "• `/help` - Help (DM only)\n"
        "• `/commands` - Commands list (group admin)\n"
        "• `/stats` - Bot usage stats (DM only)\n"
    )
    await event.respond(cmd_text)

@client.on(events.NewMessage(pattern=r'^/stats(@\w+)?$'))
async def stats_command(event):
    if event.is_group:
        return
    update_usage(event.sender_id)
    user_count = get_monthly_user_count()
    group_count = get_protecting_group_count()
    total_members = await get_total_members_monitored()
    msg = (
        "📊 **Stats below**\n\n"
        
        f"📊 **Protecting Groups:** `{group_count}`\n"
        f"👥 **Total Members Monitored:** `{total_members}`\n"
    )
    await event.respond(msg)

# -----------------------------------------------------------------------------
# 4) Content Moderation
# -----------------------------------------------------------------------------
@client.on(events.NewMessage)
async def delete_prohibited_content(event):
    try:
        if event.is_group:
            update_usage(event.sender_id, event.chat_id)
        else:
            update_usage(event.sender_id)

        if not event.is_group:
            return
        if await is_admin(event):
            return

        fwd = event.message.fwd_from
        if fwd and getattr(fwd, "from_id", None) and isinstance(fwd.from_id, PeerChannel):
            await block_and_warn(event, "🚫 Forwarded messages from channels are not allowed!")
            return

        if has_links(event):
            await block_and_warn(event, "🚫 Links are not allowed!")
            return

        mentions = mention_regex.findall(event.raw_text)
        if mentions:
            for mention in mentions:
                username = mention.lstrip('@')
                if await is_dangerous_mention(username):
                    await block_and_warn(event, "🚫 Mentions of groups or channels are not allowed!")
                    return
    except Exception as e:
        logging.exception(f"Error in delete_prohibited_content: {e}")

# -----------------------------------------------------------------------------
# Run Bot
# -----------------------------------------------------------------------------
print("Bot is running...")

client.loop.create_task(periodic_usage_cleanup())
client.loop.create_task(periodic_usage_save())

try:
    client.run_until_disconnected()
except Exception as e:
    logging.exception(f"Bot encountered an error: {e}")
