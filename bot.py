"""
RoxieCloud - Telegram File Store Bot
-------------------------------------
Admin uploads a file -> bot stores a copy in a private DB channel -> generates
a shareable deep link. Anyone opening that link (after passing Force-Sub
verification, if enabled) receives the file from the bot.

Stack: Pyrogram (MTProto, supports files up to 2GB) + MongoDB (persistent storage)
"""

import os
import asyncio
import logging
import secrets
from datetime import datetime

from dotenv import load_dotenv
from pyrogram import Client, filters
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

DEFAULT_SETTINGS = {
    "_id": "config",
    "auto_delete": 0,  # seconds, 0 = off
    "share_enabled": True,
    "save_enabled": True,
    "fsub_enabled": True,
    "fsub_channel_id": int(os.environ.get("FSUB_CHANNEL_ID", "0") or 0),
    "fsub_invite_link": os.environ.get("FSUB_INVITE_LINK", ""),
}


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
        users_col.insert_one({"_id": user_id, "joined_at": datetime.utcnow()})


def gen_code():
    while True:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        if not files_col.find_one({"_id": code}):
            return code


def is_admin(user_id):
    return user_id in ADMIN_IDS


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
    add_user(user_id)

    args = message.command
    code = args[1] if len(args) > 1 else None

    # Admin auto-recognized: skip FSUB entirely
    if is_admin(user_id):
        if code:
            # Deliver the file directly - repeating the welcome card on every
            # single file link would be redundant.
            await send_file_by_code(client, message.chat.id, code)
        else:
            await send_admin_welcome(client, message.chat.id)
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
            "Please join our channel first, then tap **Verify** below to continue.",
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
    # If this /start carries a file code, the user came from a shared link -
    # deliver the file directly instead of showing the welcome card again.
    # The welcome card is only meant for a first-time / plain /start.
    if code:
        await send_file_by_code(client, chat_id, code)
        return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help", callback_data="show_help")]])
    text = (
        "🪪 **Welcome to RoxieCloud!**\n\n"
        "This bot securely stores your files and lets you share them via links.\n\n"
        f"{CREDIT_LINE}"
    )
    await client.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


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
        asyncio.create_task(auto_delete(client, chat_id, [sent.id, warn.id], delay))


async def auto_delete(client, chat_id, msg_ids, delay):
    await asyncio.sleep(delay)
    for mid in msg_ids:
        try:
            await client.delete_messages(chat_id, mid)
        except Exception:
            pass


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

    await message.reply(f"✅ **File Saved Successfully**\n\nShare Link (tap to copy):\n`{link}`")


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
            [InlineKeyboardButton("🆔 GetId", callback_data="adm_getid_info"),
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

    await cq.answer()


@app.on_message(
    filters.text
    & filters.private
    & filters.user(ADMIN_IDS)
    & ~filters.command(["start", "admin", "broadcast", "getid"])
)
async def handle_admin_text(client, message: Message):
    admin_id = message.from_user.id
    if admin_id not in pending_edit:
        return

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
# RUN
# ============================================================
if __name__ == "__main__":
    log.info("Starting RoxieCloud Bot...")
    app.run()
