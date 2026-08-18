"""
RoxieCloud - Telegram File Store Bot
-------------------------------------
Admin uploads a file -> bot stores a copy in a private DB channel -> generates
a shareable deep link. Anyone opening that link (after passing Force-Sub
verification, if enabled) receives the file from the bot.

Stack: Pyrogram (MTProto, supports files up to 2GB) + MongoDB (persistent storage)
"""

import os
import re
import asyncio
import logging
import secrets
from datetime import datetime, timedelta

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.errors import UserNotParticipant, FloodWait, RPCError
from pymongo import MongoClient

load_dotenv()  # no-op in production if .env doesn't exist; useful for local testing

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("RoxieCloud")

# ============================================================
# ENV
# ============================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
MONGO_URI = os.environ["MONGO_URI"]
DB_CHANNEL_ID = int(os.environ["DB_CHANNEL_ID"])
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

if not ADMIN_IDS:
    log.warning("ADMIN_IDS is empty! No one will be able to access the admin panel.")

# ============================================================
# BRANDING / CREDITS
# ============================================================
CREDIT_LINE = "👨‍💻 Developed by [RoxieADMIN](https://t.me/RoxieADMIN) • [GitHub](https://github.com/456IND)"

# ============================================================
# DATABASE
# ============================================================
mongo = MongoClient(MONGO_URI)
db = mongo["roxiecloud"]
files_col = db["files"]
users_col = db["users"]
settings_col = db["settings"]
banned_col = db["banned"]
batch_col = db["batches"]
special_links_col = db["special_links"]
pending_deletions_col = db["pending_deletions"]

MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "50"))
SPECIAL_PREFIX = "s_"
BATCH_PREFIX = "b_"

DEFAULT_SETTINGS = {
    "_id": "config",
    "auto_delete": 0,  # seconds, 0 = off
    "share_enabled": True,
    "save_enabled": True,
    "fsub_enabled": True,
    "fsub_channel_id": int(os.environ.get("FSUB_CHANNEL_ID", "0") or 0),
    "fsub_invite_link": os.environ.get("FSUB_INVITE_LINK", ""),
    "welcome_text": None,    # None = use DEFAULT_WELCOME_TEXT
    "fsub_join_text": None,  # None = use DEFAULT_FSUB_JOIN_TEXT
}

# Admin-editable via the Admin Panel's "📝 Messages" section, without a
# redeploy. `get_message()` falls back to these whenever no override is
# saved in MongoDB (or the override was reset).
DEFAULT_WELCOME_TEXT = "This bot securely stores your files and lets you share them via links."
DEFAULT_FSUB_JOIN_TEXT = "Please join our channel first, then tap **Verify** below to continue."
EDITABLE_MESSAGES = {
    "welcome_text": ("Welcome Message", DEFAULT_WELCOME_TEXT),
    "fsub_join_text": ("FSUB Join Message", DEFAULT_FSUB_JOIN_TEXT),
}


def get_message(key):
    conf = get_settings()
    return conf.get(key) or EDITABLE_MESSAGES[key][1]


def get_settings():
    conf = settings_col.find_one({"_id": "config"})
    if not conf:
        settings_col.insert_one(DEFAULT_SETTINGS)
        conf = DEFAULT_SETTINGS
    return conf


def update_setting(key, value):
    settings_col.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)


def add_user(user_id):
    if not users_col.find_one({"_id": user_id}):
        users_col.insert_one({"_id": user_id, "joined_at": datetime.utcnow(), "welcomed": False})


def has_welcomed(user_id):
    doc = users_col.find_one({"_id": user_id})
    return bool(doc and doc.get("welcomed"))


def mark_welcomed(user_id):
    users_col.update_one({"_id": user_id}, {"$set": {"welcomed": True}}, upsert=True)


def gen_code():
    while True:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        if not files_col.find_one({"_id": code}):
            return code


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ---- Ban system ----
def ban_user(user_id, reason=None, expires_at=None):
    banned_col.update_one(
        {"_id": user_id},
        {"$set": {"banned_at": datetime.utcnow(), "reason": reason, "expires_at": expires_at}},
        upsert=True,
    )


def unban_user(user_id):
    banned_col.delete_one({"_id": user_id})


def is_banned(user_id):
    doc = banned_col.find_one({"_id": user_id})
    if not doc:
        return False
    expires_at = doc.get("expires_at")
    if expires_at and expires_at <= datetime.utcnow():
        unban_user(user_id)
        return False
    return True


def get_expired_bans():
    now = datetime.utcnow()
    return [d["_id"] for d in banned_col.find({"expires_at": {"$ne": None, "$lte": now}})]


async def _execute_ban(message, target, rest_text):
    """Shared by /ban and the Admin Panel's Ban button. rest_text is
    everything after the user_id, as one raw string (may be empty)."""
    if is_admin(target):
        await message.reply("🚫 Admins cannot be banned.")
        return

    expires_at = None
    reason = None
    rest_text = rest_text.strip()
    if rest_text:
        first_word, _, remainder = rest_text.partition(" ")
        seconds = parse_duration(first_word)
        if seconds is not None:
            expires_at = datetime.utcnow() + timedelta(seconds=seconds)
            reason = remainder.strip() or None
        else:
            reason = rest_text

    ban_user(target, reason, expires_at)
    reply = f"✅ User `{target}` has been banned."
    reply += f"\n⏳ Auto-unban at: `{expires_at.isoformat()} UTC`" if expires_at else "\n⏳ Type: Permanent"
    if reason:
        reply += f"\nReason: {reason}"
    await message.reply(reply)


async def _execute_unban(message, target):
    unban_user(target)
    await message.reply(f"✅ User `{target}` has been unbanned.")


def parse_duration(text):
    """Parses '30', '30m', '2h', '1d' into a number of seconds. A bare
    number (no suffix) is treated as minutes. Returns None if unparsable."""
    m = re.match(r"^(\d+)\s*([mhd]?)$", text.strip().lower())
    if not m:
        return None
    value, unit = m.groups()
    value = int(value)
    if value <= 0:
        return None
    return value * {"m": 60, "h": 3600, "d": 86400, "": 60}[unit]


# ---- Batch links ----
def gen_batch_code():
    while True:
        token = BATCH_PREFIX + secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        if not batch_col.find_one({"_id": token}):
            return token


# ---- Special (view/time-limited) links ----
def gen_special_token():
    while True:
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        if not special_links_col.find_one({"_id": token}):
            return token


def create_special_link(source_code, max_views=None, expires_at=None):
    token = gen_special_token()
    special_links_col.insert_one(
        {
            "_id": token,
            "source_code": source_code,
            "max_views": max_views,
            "views_used": 0,
            "expires_at": expires_at,
            "created_at": datetime.utcnow(),
            "revoked": False,
        }
    )
    return token


def claim_special_link(token):
    """Atomically attempts to consume one view of a special link.
    Returns (status, source_code): status is "ok", "revoked", "expired",
    "limit_reached", or "invalid"."""
    doc = special_links_col.find_one({"_id": token})
    if not doc:
        return "invalid", None
    if doc.get("revoked"):
        return "revoked", None
    expires_at = doc.get("expires_at")
    if expires_at and expires_at <= datetime.utcnow():
        return "expired", None

    max_views = doc.get("max_views")
    if max_views is not None:
        result = special_links_col.update_one(
            {"_id": token, "views_used": {"$lt": max_views}}, {"$inc": {"views_used": 1}}
        )
        if result.modified_count == 0:
            return "limit_reached", None
        return "ok", doc["source_code"]

    special_links_col.update_one({"_id": token}, {"$inc": {"views_used": 1}})
    return "ok", doc["source_code"]


def extract_code(raw):
    """Admins often paste the whole link rather than just the code - pull
    the code back out of a `?start=...` URL if that's what was given."""
    raw = raw.strip()
    if "start=" in raw:
        raw = raw.split("start=", 1)[1]
    return raw.split("&")[0].strip()


# ---- Restart-safe auto-delete queue ----
def schedule_deletion(chat_id, message_id, delay_seconds):
    delete_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
    pending_deletions_col.update_one(
        {"_id": f"{chat_id}:{message_id}"},
        {"$set": {"chat_id": chat_id, "message_id": message_id, "delete_at": delete_at}},
        upsert=True,
    )


# ============================================================
# APP
# ============================================================
app = Client(
    "roxiecloud",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

BOT_USERNAME = None

# In-memory state (resets on restart - fine for these short-lived flows)
pending_start_payload = {}  # user_id -> code waiting to be delivered after verify
join_msgs = {}              # user_id -> [message_ids] to delete after verify
pending_edit = {}           # admin_id -> "fsub_id" | "fsub_link"


async def get_bot_username(client):
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await client.get_me()
        BOT_USERNAME = me.username
    return BOT_USERNAME


# ============================================================
# START / FORCE-SUB / DELIVERY FLOW
# ============================================================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    user_id = message.from_user.id

    if is_banned(user_id):
        await message.reply("🚫 You are banned from using this bot. Contact the administrator if you believe this is a mistake.")
        return

    add_user(user_id)

    args = message.command
    code = args[1] if len(args) > 1 else None

    # Admin auto-recognized: skip FSUB entirely
    if is_admin(user_id):
        if code:
            await deliver_code(client, message.chat.id, code)
        elif not has_welcomed(user_id):
            await send_admin_welcome(client, message.chat.id)
            mark_welcomed(user_id)
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Admin Panel", callback_data="adm_back")]])
            await client.send_message(message.chat.id, "Welcome back, Administrator.", reply_markup=kb)
        return

    conf = get_settings()

    joined = True
    if conf.get("fsub_enabled") and conf.get("fsub_channel_id"):
        try:
            await client.get_chat_member(conf["fsub_channel_id"], user_id)
        except UserNotParticipant:
            joined = False
        except RPCError as e:
            log.error(f"FSUB check failed (fail-open): {e}")
            joined = True

    if not joined:
        pending_start_payload[user_id] = code
        m1 = await message.reply("🔒 **Access Restricted**")
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📢 Join Channel", url=conf.get("fsub_invite_link") or "https://t.me")],
                [InlineKeyboardButton("✅ Verify", callback_data="verify")],
            ]
        )
        m2 = await message.reply(
            get_message("fsub_join_text"),
            reply_markup=kb,
        )
        join_msgs[user_id] = [m1.id, m2.id]
        return

    await deliver_start(client, message.chat.id, user_id, code)


@app.on_callback_query(filters.regex("^verify$"))
async def verify_cb(client, cq: CallbackQuery):
    user_id = cq.from_user.id
    conf = get_settings()

    try:
        await client.get_chat_member(conf["fsub_channel_id"], user_id)
    except UserNotParticipant:
        await cq.answer("You have not joined the channel yet. Please join first.", show_alert=True)
        return
    except RPCError:
        pass

    await cq.answer("Verification successful ✅")

    for mid in join_msgs.get(user_id, []):
        try:
            await client.delete_messages(cq.message.chat.id, mid)
        except Exception:
            pass
    join_msgs.pop(user_id, None)

    try:
        await cq.message.delete()
    except Exception:
        pass

    code = pending_start_payload.pop(user_id, None)
    await deliver_start(client, cq.message.chat.id, user_id, code)


async def deliver_start(client, chat_id, user_id, code):
    # If this /start carries a code, the user came from a shared link -
    # deliver the content directly, no need for the welcome card at all.
    if code:
        await deliver_code(client, chat_id, code)
        return

    # Plain /start with no code: only show the full welcome card the first
    # time this user is ever seen. Returning users (already FSUB-verified
    # before) get a short acknowledgement instead of the whole card again.
    if has_welcomed(user_id):
        await client.send_message(chat_id, "Welcome back.")
        return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help", callback_data="show_help")]])
    text = (
        "🪪 **Welcome to RoxieCloud!**\n\n"
        f"{get_message('welcome_text')}\n\n"
        f"{CREDIT_LINE}"
    )
    await client.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    mark_welcomed(user_id)


async def send_admin_welcome(client, chat_id):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚙️ Admin Panel", callback_data="adm_back")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")],
        ]
    )
    text = (
        "🪪 **Welcome back, Administrator!**\n\n"
        "The bot is fully operational. You can open the Admin Panel directly below.\n\n"
        f"{CREDIT_LINE}"
    )
    await client.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


HELP_TEXT = (
    "**📖 RoxieCloud — Help**\n\n"
    "This is a file-storage bot. The administrator uploads files, the bot generates "
    "a shareable link for each one, and anyone with that link can access the file.\n\n"
    "**Commands:**\n"
    "• `/start` — Start the bot\n"
    "• Clicking a shared link automatically triggers `/start <code>` and delivers the file\n"
    "• `/help` — Show this message\n\n"
    f"{CREDIT_LINE}"
)


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(client, message: Message):
    await message.reply(HELP_TEXT, disable_web_page_preview=True)


@app.on_callback_query(filters.regex("^show_help$"))
async def help_cb(client, cq: CallbackQuery):
    await cq.answer()
    await cq.message.reply(HELP_TEXT, disable_web_page_preview=True)


async def deliver_code(client, chat_id, code):
    """Resolves any /start code - special-link, batch, or a normal single
    file - and delivers the underlying content."""
    if code.startswith(SPECIAL_PREFIX):
        token = code[len(SPECIAL_PREFIX):]
        status, inner_code = claim_special_link(token)
        if status != "ok":
            reply_text = {
                "revoked": "🚫 This link has been revoked.",
                "expired": "⏳ This link's time limit has expired.",
                "limit_reached": "🚫 This link's view limit has been reached.",
            }.get(status, "⚠️ This link is invalid or has expired.")
            await client.send_message(chat_id, reply_text)
            return
        code = inner_code

    if code.startswith(BATCH_PREFIX):
        await deliver_batch(client, chat_id, code)
        return

    await send_file_by_code(client, chat_id, code)


async def deliver_batch(client, chat_id, code):
    doc = batch_col.find_one({"_id": code})
    if not doc:
        await client.send_message(chat_id, "⚠️ This batch link is invalid or has expired.")
        return

    start_id, end_id = doc["start_msg_id"], doc["end_msg_id"]
    conf = get_settings()
    protect = not (conf.get("share_enabled", True) and conf.get("save_enabled", True))
    delay = conf.get("auto_delete", 0)

    status_msg = await client.send_message(chat_id, f"Sending {end_id - start_id + 1} file(s), please wait...")
    sent_count = 0
    for msg_id in range(start_id, end_id + 1):
        try:
            sent = await client.copy_message(
                chat_id=chat_id, from_chat_id=DB_CHANNEL_ID, message_id=msg_id, protect_content=protect
            )
            sent_count += 1
            if delay and delay > 0:
                schedule_deletion(chat_id, sent.id, delay)
            await asyncio.sleep(0.3)
        except Exception:
            continue

    summary = f"✅ Delivered {sent_count} file(s)."
    if delay and delay > 0:
        summary += f" These will be automatically deleted in {delay} seconds."
    await status_msg.edit_text(summary)


async def send_file_by_code(client, chat_id, code):
    doc = files_col.find_one({"_id": code})
    if not doc:
        await client.send_message(chat_id, "⚠️ This link is invalid or has expired.")
        return

    conf = get_settings()
    # Telegram exposes only ONE flag for both forward+save restriction.
    protect = not (conf.get("share_enabled", True) and conf.get("save_enabled", True))

    try:
        sent = await client.copy_message(
            chat_id=chat_id,
            from_chat_id=DB_CHANNEL_ID,
            message_id=doc["msg_id"],
            protect_content=protect,
        )
    except Exception as e:
        log.error(f"Failed to deliver file {code}: {e}")
        await client.send_message(chat_id, "❌ An error occurred while delivering the file.")
        return

    delay = conf.get("auto_delete", 0)
    if delay and delay > 0:
        warn = await client.send_message(
            chat_id, f"⚠️ This file will be automatically deleted in **{delay} seconds**. Please save or forward it now."
        )
        # Persisted to MongoDB (not asyncio.sleep) so scheduled deletions
        # survive a bot restart - deletion_sweeper() picks these up.
        schedule_deletion(chat_id, sent.id, delay)
        schedule_deletion(chat_id, warn.id, delay)


# ============================================================
# ADMIN: FILE UPLOAD -> SAVE TO DB CHANNEL -> GENERATE LINK
# ============================================================
MEDIA_FILTER = (
    filters.photo | filters.video | filters.audio | filters.document
    | filters.animation | filters.voice | filters.video_note
)


@app.on_message(MEDIA_FILTER & filters.private & filters.user(ADMIN_IDS))
async def save_file_handler(client, message: Message):
    try:
        copied = await message.copy(DB_CHANNEL_ID)
    except Exception as e:
        log.error(f"DB channel copy failed: {e}")
        await message.reply(
            "❌ Failed to save the file to the DB channel.\nPlease check whether the bot has admin rights there."
        )
        return

    code = gen_code()
    files_col.insert_one(
        {
            "_id": code,
            "msg_id": copied.id,
            "added_by": message.from_user.id,
            "added_at": datetime.utcnow(),
        }
    )

    username = await get_bot_username(client)
    link = f"https://t.me/{username}?start={code}"

    # Post the link directly under the file in the DB channel too (as a reply,
    # so it stays visually attached), in monospace so it can be tapped to copy
    # and re-shared without needing to come back to the bot's DM.
    try:
        await client.send_message(
            DB_CHANNEL_ID,
            f"`{link}`",
            reply_to_message_id=copied.id,
        )
    except Exception as e:
        log.error(f"Failed to post link under file in DB channel: {e}")

    await message.reply(f"✅ **File Saved Successfully**\n\nShare Link (tap to copy):\n`{link}`\n\nDB Message ID: `{copied.id}` (needed for `/batch`)")


# ============================================================
# ADMIN PANEL
# ============================================================
def admin_main_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast_info"),
             InlineKeyboardButton("🐞 Debug", callback_data="adm_debug")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings"),
             InlineKeyboardButton("✏️ Edit", callback_data="adm_edit")],
            [InlineKeyboardButton("📝 Messages", callback_data="adm_messages"),
             InlineKeyboardButton("🆔 GetId", callback_data="adm_getid_info")],
            [InlineKeyboardButton("🚫 Ban / Unban", callback_data="adm_ban_menu"),
             InlineKeyboardButton("📦 Batch", callback_data="adm_batch_info")],
            [InlineKeyboardButton("🔒 Special Links", callback_data="adm_speciallink_info"),
             InlineKeyboardButton("❓ Help", callback_data="show_help")],
            [InlineKeyboardButton("❌ Exit", callback_data="adm_exit")],
        ]
    )


def settings_kb(conf):
    share_label = f"🔗 Share: {'✅ ON' if conf.get('share_enabled') else '🚫 OFF'}"
    save_label = f"💾 Save: {'✅ ON' if conf.get('save_enabled') else '🚫 OFF'}"
    fsub_label = f"📢 FSUB: {'✅ ON' if conf.get('fsub_enabled') else '🚫 OFF'}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏱ Time Changer", callback_data="adm_time")],
            [InlineKeyboardButton(share_label, callback_data="adm_toggle_share"),
             InlineKeyboardButton(save_label, callback_data="adm_toggle_save")],
            [InlineKeyboardButton(fsub_label, callback_data="adm_toggle_fsub")],
            [InlineKeyboardButton("🔙 Back", callback_data="adm_back")],
        ]
    )


def time_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("10s", callback_data="adm_time_10"), InlineKeyboardButton("30s", callback_data="adm_time_30")],
            [InlineKeyboardButton("1m", callback_data="adm_time_60"), InlineKeyboardButton("5m", callback_data="adm_time_300")],
            [InlineKeyboardButton("15m", callback_data="adm_time_900"), InlineKeyboardButton("30m", callback_data="adm_time_1800")],
            [InlineKeyboardButton("2h", callback_data="adm_time_7200"), InlineKeyboardButton("OFF", callback_data="adm_time_0")],
            [InlineKeyboardButton("🔙 Back", callback_data="adm_settings")],
        ]
    )


@app.on_message(filters.command("admin") & filters.private & filters.user(ADMIN_IDS))
async def admin_panel(client, message: Message):
    await message.reply("🛠 **Admin Panel**\n\nPlease select an option below:", reply_markup=admin_main_kb())


@app.on_callback_query(filters.regex("^adm_") & filters.user(ADMIN_IDS))
async def admin_cb(client, cq: CallbackQuery):
    data = cq.data

    if data == "adm_back":
        await cq.message.edit_text("🛠 **Admin Panel**\n\nPlease select an option below:", reply_markup=admin_main_kb())

    elif data == "adm_exit":
        await cq.message.delete()

    elif data == "adm_broadcast_info":
        await cq.message.edit_text(
            "**Broadcast**\n\nType a message (text, link, or emoji are all supported), "
            "then reply to that message with `/broadcast` to send it to all users.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]]),
        )

    elif data == "adm_debug":
        text = "✅ No recent errors found in logs."
        try:
            with open("bot.log", "r") as f:
                lines = f.readlines()
                errors = [l for l in lines if "ERROR" in l][-15:]
                if errors:
                    text = "```\n" + "".join(errors) + "\n```"
        except FileNotFoundError:
            pass
        await cq.message.edit_text(
            f"**Debug Log**\n\n{text}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]]),
        )

    elif data == "adm_getid_info":
        await cq.message.edit_text(
            "**Get ID**\n\nForward or send any message (from a channel, group, or user) here, "
            "then reply to that message with `/getid`.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]]),
        )

    elif data == "adm_settings":
        await cq.message.edit_text("**Settings**", reply_markup=settings_kb(get_settings()))

    elif data == "adm_toggle_share":
        conf = get_settings()
        update_setting("share_enabled", not conf.get("share_enabled", True))
        await cq.message.edit_text("**Settings**", reply_markup=settings_kb(get_settings()))

    elif data == "adm_toggle_save":
        conf = get_settings()
        update_setting("save_enabled", not conf.get("save_enabled", True))
        await cq.message.edit_text("**Settings**", reply_markup=settings_kb(get_settings()))

    elif data == "adm_toggle_fsub":
        conf = get_settings()
        update_setting("fsub_enabled", not conf.get("fsub_enabled", True))
        await cq.message.edit_text("**Settings**", reply_markup=settings_kb(get_settings()))

    elif data == "adm_time":
        await cq.message.edit_text("**Auto-Delete Timer**\n\nSelect how long delivered files should remain before being automatically deleted:", reply_markup=time_kb())

    elif data.startswith("adm_time_"):
        seconds = int(data.split("_")[-1])
        update_setting("auto_delete", seconds)
        await cq.answer(f"Timer set to {seconds} seconds" if seconds else "Timer turned off")
        label = f"{seconds}s" if seconds else "OFF"
        await cq.message.edit_text(f"**Auto-Delete Timer**\n\nCurrent: {label}\n\nSelect how long delivered files should remain before being automatically deleted:", reply_markup=time_kb())

    elif data == "adm_edit":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🆔 Change FSUB Channel ID", callback_data="adm_edit_id")],
                [InlineKeyboardButton("🔗 Change FSUB Invite Link", callback_data="adm_edit_link")],
                [InlineKeyboardButton("🔙 Back", callback_data="adm_back")],
            ]
        )
        await cq.message.edit_text("**Edit**", reply_markup=kb)

    elif data == "adm_edit_id":
        pending_edit[cq.from_user.id] = "fsub_id"
        await cq.message.edit_text("Please send the new FSUB Channel ID (e.g. `-1001234567890`):")

    elif data == "adm_edit_link":
        pending_edit[cq.from_user.id] = "fsub_link"
        await cq.message.edit_text("Please send the new FSUB invite link (e.g. `https://t.me/+xxxxxxxxxxxx`):")

    elif data == "adm_messages":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Welcome Message", callback_data="adm_edit_msg_welcome_text")],
                [InlineKeyboardButton("✏️ FSUB Join Message", callback_data="adm_edit_msg_fsub_join_text")],
                [InlineKeyboardButton("🔙 Back", callback_data="adm_back")],
            ]
        )
        await cq.message.edit_text("**📝 Messages**\n\nChoose a message to customize:", reply_markup=kb)

    elif data.startswith("adm_edit_msg_"):
        key = data[len("adm_edit_msg_"):]
        pending_edit[cq.from_user.id] = key
        label, _default = EDITABLE_MESSAGES[key]
        current = get_message(key)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Reset to Default", callback_data=f"adm_reset_msg_{key}")],
                [InlineKeyboardButton("🔙 Cancel", callback_data="adm_messages")],
            ]
        )
        await cq.message.edit_text(f"**{label}**\n\nCurrent text:\n{current}\n\nSend the new text to replace it:", reply_markup=kb)

    elif data.startswith("adm_reset_msg_"):
        key = data[len("adm_reset_msg_"):]
        update_setting(key, None)
        label, _default = EDITABLE_MESSAGES[key]
        await cq.answer(f"{label} reset to default ✅")
        await cq.message.edit_text(
            f"✅ **{label}** has been reset to default.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_messages")]]),
        )

    elif data == "adm_ban_menu":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🚫 Ban User", callback_data="adm_ban_start")],
                [InlineKeyboardButton("✅ Unban User", callback_data="adm_unban_start")],
                [InlineKeyboardButton("🔙 Back", callback_data="adm_back")],
            ]
        )
        await cq.message.edit_text("**Ban / Unban**\n\nChoose an action:", reply_markup=kb)

    elif data == "adm_ban_start":
        pending_edit[cq.from_user.id] = "ban_user"
        await cq.message.edit_text(
            "Send the details in one line:\n`<user_id> [duration] [reason]`\n\n"
            "Duration is optional (`30m`, `2h`, `1d`) — omit it for a permanent ban.\n"
            "Example: `123456789 2h spamming`"
        )

    elif data == "adm_unban_start":
        pending_edit[cq.from_user.id] = "unban_user"
        await cq.message.edit_text("Send the user ID to unban:")

    elif data == "adm_batch_info":
        await cq.message.edit_text(
            "**📦 Batch Links**\n\n"
            "Usage: `/batch <start_msg_id> <end_msg_id>`\n\n"
            "Use the DB channel message IDs (shown when a file is uploaded, or via "
            "Telegram's \"Copy Message Link\" on any message in the DB channel).\n\n"
            f"Max files per batch: `{MAX_BATCH_SIZE}`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]]),
        )

    elif data == "adm_speciallink_info":
        await cq.message.edit_text(
            "**🔒 Special Links**\n\n" + SPECIAL_LINK_USAGE + "\n\nCheck status: `/speciallinkstats <link_or_token>`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="adm_back")]]),
        )

    await cq.answer()


# Only matches when we're actually waiting for this admin's text input,
# AND the message isn't itself a slash command (so if an admin changes
# their mind mid-flow and runs a real command instead, it still reaches
# that command's own handler instead of being swallowed as edit input).
awaiting_edit_filter = filters.create(lambda _, __, m: bool(m.from_user) and m.from_user.id in pending_edit)
not_command_filter = filters.create(lambda _, __, m: not (m.text and m.text.startswith("/")))


@app.on_message(filters.text & filters.private & filters.user(ADMIN_IDS) & awaiting_edit_filter & not_command_filter)
async def handle_admin_text(client, message: Message):
    admin_id = message.from_user.id
    action = pending_edit.pop(admin_id)

    if action == "fsub_id":
        try:
            new_id = int(message.text.strip())
            update_setting("fsub_channel_id", new_id)
            await message.reply(f"✅ FSUB Channel ID updated: `{new_id}`")
        except ValueError:
            await message.reply("❌ Invalid ID. It must be a numeric value, e.g. `-1001234567890`")

    elif action == "fsub_link":
        update_setting("fsub_invite_link", message.text.strip())
        await message.reply("✅ FSUB invite link updated.")

    elif action == "welcome_text":
        update_setting("welcome_text", message.text)
        await message.reply("✅ Welcome message updated.")

    elif action == "fsub_join_text":
        update_setting("fsub_join_text", message.text)
        await message.reply("✅ FSUB join message updated.")

    elif action == "ban_user":
        parts = message.text.split(None, 1)
        try:
            target = int(parts[0])
        except (ValueError, IndexError):
            await message.reply("The user ID must be numeric.")
            return
        rest_text = parts[1] if len(parts) > 1 else ""
        await _execute_ban(message, target, rest_text)

    elif action == "unban_user":
        try:
            target = int(message.text.strip())
        except ValueError:
            await message.reply("The user ID must be numeric.")
            return
        await _execute_unban(message, target)


# ============================================================
# BROADCAST
# ============================================================
@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_IDS) & filters.reply)
async def broadcast_handler(client, message: Message):
    src = message.reply_to_message
    all_users = list(users_col.find({}, {"_id": 1}))
    total = len(all_users)
    status = await message.reply(f"📢 Broadcasting to {total} users...")

    sent, failed = 0, 0
    for u in all_users:
        uid = u["_id"]
        try:
            await src.copy(uid)
            sent += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await src.copy(uid)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # basic throttle, stays under Telegram's ~30 msg/s cap

    await status.edit_text(f"✅ **Broadcast complete.**\n\nSent: {sent}\nFailed: {failed}\nTotal: {total}")


@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_IDS) & ~filters.reply)
async def broadcast_no_reply(client, message: Message):
    await message.reply("Please reply to a message with `/broadcast` to send it.")


# ============================================================
# GETID
# ============================================================
@app.on_message(filters.command("getid") & filters.private & filters.user(ADMIN_IDS))
async def getid_handler(client, message: Message):
    if message.reply_to_message:
        src = message.reply_to_message
        lines = [f"**Chat ID:** `{message.chat.id}`"]
        if src.from_user:
            lines.append(f"**User ID:** `{src.from_user.id}`")
            lines.append(f"**Name:** {src.from_user.first_name}")
        if src.forward_from_chat:
            fc = src.forward_from_chat
            lines.append(f"**Forwarded From Chat ID:** `{fc.id}`")
            lines.append(f"**Chat Title:** {fc.title}")
            lines.append(f"**Chat Type:** {fc.type}")
        if src.forward_from:
            ff = src.forward_from
            lines.append(f"**Forwarded From User ID:** `{ff.id}`")
        await message.reply("\n".join(lines))
    else:
        await message.reply(f"**Your ID:** `{message.from_user.id}`\n**This Chat ID:** `{message.chat.id}`")


# ============================================================
# BAN SYSTEM
# ============================================================
@app.on_message(filters.command("ban") & filters.private & filters.user(ADMIN_IDS))
async def cmd_ban(client, message: Message):
    parts = message.text.split(None, 2)
    if len(parts) < 2:
        await message.reply(
            "Usage: `/ban <user_id> [duration] [reason]`\n"
            "Duration is optional: `30m`, `2h`, `1d`. Omit it for a permanent ban."
        )
        return
    try:
        target = int(parts[1])
    except ValueError:
        await message.reply("The user ID must be numeric.")
        return
    rest_text = parts[2] if len(parts) > 2 else ""
    await _execute_ban(message, target, rest_text)


@app.on_message(filters.command("unban") & filters.private & filters.user(ADMIN_IDS))
async def cmd_unban(client, message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: `/unban <user_id>`")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await message.reply("The user ID must be numeric.")
        return
    await _execute_unban(message, target)


# ============================================================
# BATCH LINKS
# ============================================================
@app.on_message(filters.command("batch") & filters.private & filters.user(ADMIN_IDS))
async def create_batch_link(client, message: Message):
    parts = message.text.split()
    if len(parts) != 3:
        await message.reply(
            "Usage: `/batch <start_msg_id> <end_msg_id>`\n\n"
            "Use the DB channel message IDs (shown when a file is uploaded, "
            "or via Telegram's \"Copy Message Link\" on any message in the DB channel)."
        )
        return
    try:
        start_id, end_id = int(parts[1]), int(parts[2])
    except ValueError:
        await message.reply("start_msg_id and end_msg_id must be numeric.")
        return
    if start_id > end_id:
        await message.reply("start_msg_id must be less than or equal to end_msg_id.")
        return
    if (end_id - start_id + 1) > MAX_BATCH_SIZE:
        await message.reply(
            f"A batch link can contain at most {MAX_BATCH_SIZE} files "
            f"(requested {end_id - start_id + 1}). Use a smaller range, or raise the `MAX_BATCH_SIZE` env var."
        )
        return

    token = gen_batch_code()
    batch_col.insert_one(
        {
            "_id": token,
            "start_msg_id": start_id,
            "end_msg_id": end_id,
            "created_by": message.from_user.id,
            "created_at": datetime.utcnow(),
        }
    )
    username = await get_bot_username(client)
    link = f"https://t.me/{username}?start={token}"
    await message.reply(f"✅ **Batch Link Ready**\n\nFiles: {end_id - start_id + 1}\nLink (tap to copy):\n`{link}`")


# ============================================================
# SPECIAL (VIEW/TIME-LIMITED) LINKS
# ============================================================
SPECIAL_LINK_USAGE = (
    "**Usage:**\n"
    "`/speciallink <link_or_code> views <N>` — usable a total of N times (across all users)\n"
    "`/speciallink <link_or_code> time <duration>` — expires after the given duration (e.g. `30m`, `2h`, `1d`)\n"
    "Both can be combined — whichever limit is hit first wins:\n"
    "`/speciallink <link_or_code> views 5 time 1h`"
)


@app.on_message(filters.command("speciallink") & filters.private & filters.user(ADMIN_IDS))
async def speciallink_cmd(client, message: Message):
    args = message.text.split()[1:]
    if len(args) < 3:
        await message.reply(SPECIAL_LINK_USAGE)
        return

    source_code = extract_code(args[0])
    if source_code.startswith(SPECIAL_PREFIX):
        await message.reply("A special link cannot be created from another special link. Use the original file's normal link instead.")
        return

    exists = files_col.find_one({"_id": source_code}) or batch_col.find_one({"_id": source_code})
    if not exists:
        await message.reply("This code/link does not correspond to any uploaded file or batch.")
        return

    max_views = None
    expires_at = None
    i = 1  # args[0] is the source link, skip it
    while i < len(args) - 1:
        key = args[i].lower()
        if key in ("views", "view", "v"):
            try:
                max_views = int(args[i + 1])
            except ValueError:
                await message.reply("`views` must be followed by a number, e.g. `views 5`.")
                return
            if max_views < 1:
                await message.reply("`views` must be at least 1.")
                return
            i += 2
        elif key in ("time", "t"):
            secs = parse_duration(args[i + 1])
            if secs is None:
                await message.reply("Could not parse the time value. Try `time 30m`, `time 2h`, or `time 1d`.")
                return
            expires_at = datetime.utcnow() + timedelta(seconds=secs)
            i += 2
        else:
            i += 1

    if max_views is None and expires_at is None:
        await message.reply("Please provide at least one limit — `views <N>` or `time <duration>`.\n\n" + SPECIAL_LINK_USAGE)
        return

    token = create_special_link(source_code, max_views, expires_at)
    username = await get_bot_username(client)
    link = f"https://t.me/{username}?start={SPECIAL_PREFIX}{token}"

    detail_lines = []
    if max_views is not None:
        detail_lines.append(f"Max views: `{max_views}` (total, across all users)")
    if expires_at:
        detail_lines.append(f"Expires at: `{expires_at.isoformat()} UTC`")

    await message.reply(
        "🔒 **Special Link Ready**\n\n`" + link + "`\n" + "\n".join(detail_lines)
        + "\n\nOnce the limit is reached, this link will stop delivering content."
    )


@app.on_message(filters.command("speciallinkstats") & filters.private & filters.user(ADMIN_IDS))
async def speciallinkstats_cmd(client, message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: `/speciallinkstats <link_or_token>`")
        return
    token = extract_code(parts[1])
    if token.startswith(SPECIAL_PREFIX):
        token = token[len(SPECIAL_PREFIX):]

    doc = special_links_col.find_one({"_id": token})
    if not doc:
        await message.reply("This special-link token does not exist.")
        return

    status = "🚫 Revoked" if doc.get("revoked") else "✅ Active"
    lines = [f"📊 **Special Link Stats — `{token}`**", f"Status: {status}", f"Created: `{doc['created_at'].isoformat()} UTC`"]
    max_views = doc.get("max_views")
    if max_views is not None:
        lines.append(f"Views used: `{doc.get('views_used', 0)}/{max_views}`")
    else:
        lines.append(f"Views used: `{doc.get('views_used', 0)}` (no view limit)")
    if doc.get("expires_at"):
        lines.append(f"Expires at: `{doc['expires_at'].isoformat()} UTC`")
    await message.reply("\n".join(lines))


# ============================================================
# BACKGROUND SWEEPERS (restart-safe: state lives in MongoDB, not memory)
# ============================================================
async def ban_sweeper():
    while True:
        try:
            for user_id in get_expired_bans():
                unban_user(user_id)
                for admin_id in ADMIN_IDS:
                    try:
                        await app.send_message(admin_id, f"Temporary ban expired — user `{user_id}` has been automatically unbanned.")
                    except Exception:
                        pass
        except Exception as e:
            log.error(f"Ban sweeper error: {e}")
        await asyncio.sleep(60)


async def deletion_sweeper():
    while True:
        try:
            now = datetime.utcnow()
            for d in list(pending_deletions_col.find({"delete_at": {"$lte": now}})):
                try:
                    await app.delete_messages(d["chat_id"], d["message_id"])
                except Exception:
                    pass
                pending_deletions_col.delete_one({"_id": d["_id"]})
        except Exception as e:
            log.error(f"Deletion sweeper error: {e}")
        await asyncio.sleep(15)


# ============================================================
# RUN
# ============================================================
async def main():
    await app.start()
    username = await get_bot_username(app)
    asyncio.create_task(ban_sweeper())
    asyncio.create_task(deletion_sweeper())
    log.info(f"RoxieCloud Bot is live as @{username}")
    await idle()
    await app.stop()


if __name__ == "__main__":
    log.info("Starting RoxieCloud Bot...")
    app.run(main())
