import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant
from motor.motor_asyncio import AsyncIOMotorClient

# ==========================================
# 🔧 ENV LOAD
# ==========================================
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
MONGODB_URL = os.environ.get("MONGODB_URL")
ADMIN_ID = os.environ.get("ADMIN_ID")
DB_CHANNEL_ID = os.environ.get("DB_CHANNEL_ID")

missing = [name for name, val in {
    "BOT_TOKEN": BOT_TOKEN, "API_ID": API_ID, "API_HASH": API_HASH,
    "MONGODB_URL": MONGODB_URL, "ADMIN_ID": ADMIN_ID
}.items() if not val]
if missing:
    raise SystemExit(f"❌ .env me ye missing hai: {', '.join(missing)}")

API_ID = int(API_ID)
ADMIN_ID = int(ADMIN_ID)

app = Client("RoxieCloudbot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

db_client = AsyncIOMotorClient(MONGODB_URL)
db = db_client["RoxieDB"]
settings_col = db["settings"]
tokens_col = db["tokens"]

PAGE_SIZE = 10
pending_action = {}  # { user_id: "awaiting_xxx" }


# ==========================================
# ⏱ Time parser
# ==========================================
def parse_time(time_str):
    if not time_str or len(time_str) < 2:
        return None
    time_dict = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    unit = time_str[-1].lower()
    number_part = time_str[:-1]
    if unit in time_dict and number_part.isdigit():
        return int(number_part) * time_dict[unit]
    return None


async def get_config():
    return await settings_col.find_one({"_id": "config"}) or {}


async def get_db_channel_id():
    config = await get_config()
    if config.get("db_channel_id"):
        return int(config["db_channel_id"])
    if DB_CHANNEL_ID:
        return int(DB_CHANNEL_ID)
    return None


async def is_fsub_joined(client, user_id):
    config = await get_config()
    fsub_id = config.get("fsub_id")
    if not fsub_id:
        return True
    try:
        await client.get_chat_member(int(fsub_id), user_id)
        return True
    except UserNotParticipant:
        return False
    except Exception:
        return True  # fail-open agar bot admin nahi hai ya koi aur error


# ==========================================
# 🛠 ADMIN PANEL
# ==========================================

async def admin_panel_markup():
    config = await get_config()
    protect_status = "🟢 ON" if config.get("content_protection") else "🔴 OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Set FSUB", callback_data="panel_setfsub"),
         InlineKeyboardButton("🗄 Set DB Channel", callback_data="panel_setdb")],
        [InlineKeyboardButton("⏱ Set Timer", callback_data="panel_settimer"),
         InlineKeyboardButton("🗑 Auto-Delete", callback_data="panel_autodelete")],
        [InlineKeyboardButton("🔑 Generate Token", callback_data="panel_gentoken"),
         InlineKeyboardButton("❌ Revoke Token", callback_data="panel_revoke")],
        [InlineKeyboardButton(f"🔒 Content Protection: {protect_status}", callback_data="panel_toggleprotect")],
        [InlineKeyboardButton("🚪 Quit", callback_data="panel_quit")],
    ])


@app.on_message(filters.command("admin") & filters.user(ADMIN_ID))
async def admin_panel(client, message):
    await message.reply_text("🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup())


@app.on_callback_query(filters.regex(r"^panel_") & filters.user(ADMIN_ID))
async def panel_callback(client, callback_query):
    action = callback_query.data.split("_", 1)[1]
    user_id = callback_query.from_user.id

    if action == "quit":
        pending_action.pop(user_id, None)
        return await callback_query.message.delete()

    if action == "back":
        pending_action.pop(user_id, None)
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
        )

    if action == "toggleprotect":
        config = await get_config()
        new_value = not config.get("content_protection", False)
        await settings_col.update_one({"_id": "config"}, {"$set": {"content_protection": new_value}}, upsert=True)
        await callback_query.answer(f"Content Protection {'ON' if new_value else 'OFF'} kar diya.")
        return await callback_query.message.edit_text(
            "🛠 **Admin Panel** — neeche se option chuno:", reply_markup=await admin_panel_markup()
        )

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="panel_back")]])

    prompts = {
        "setfsub": "📢 Pehle FSUB **channel ID** bhej (e.g. `-1001234567890`):",
        "setdb": "🗄 DB channel ki ID bhej (e.g. `-1001234567890`):",
        "settimer": "⏱ Default token expiry time bhej (e.g. `1h`, `30m`, `1d`):",
        "autodelete": "🗑 Files kitni der baad auto-delete ho (e.g. `10m`, `1h`). Band karne ke liye `off` bhej:",
        "gentoken": "🔑 Format me bhej:\n`<start_msg_id> <end_msg_id> <name>`\nExample: `101 112 CuteGirl`",
        "revoke": "❌ Jo token revoke karna hai uska naam bhej (e.g. `Roxie-CuteGirl`):",
    }
    pending_action[user_id] = f"awaiting_{action}"
    await callback_query.answer()
    await callback_query.message.reply_text(prompts[action], reply_markup=back_btn)


# ==========================================
# 📩 ADMIN'S FOLLOW-UP REPLIES
# ==========================================

@app.on_message(filters.private & filters.text & filters.user(ADMIN_ID) & ~filters.command([
    "start", "admin"
]))
async def handle_admin_pending(client, message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)
    if not action:
        return

    text = message.text.strip()

    if action == "awaiting_setfsub":
        # Pehle ID le rahe hain, phir link maangenge
        try:
            int(text)
        except ValueError:
            return await message.reply_text("❌ Ye ID nahi lag rahi. Number bhej (e.g. `-1001234567890`) — dobara try kar.")
        await settings_col.update_one({"_id": "config"}, {"$set": {"fsub_id": text}}, upsert=True)
        pending_action[user_id] = "awaiting_setfsublink"
        return await message.reply_text("✅ ID save ho gayi. Ab channel ka **invite/join link** bhej (e.g. `https://t.me/teraChannel`):")

    elif action == "awaiting_setfsublink":
        if not (text.startswith("https://t.me/") or text.startswith("t.me/")):
            return await message.reply_text("❌ Ye valid link nahi lag raha. `https://t.me/...` format me bhej.")
        await settings_col.update_one({"_id": "config"}, {"$set": {"fsub_link": text}}, upsert=True)
        await message.reply_text(f"✅ FSUB poora set ho gaya!\n**ID:** saved\n**Link:** {text}\n\n⚠️ Bot ko us channel me admin banana mat bhoolna, warna join-check kaam nahi karega.")

    elif action == "awaiting_setdb":
        await settings_col.update_one({"_id": "config"}, {"$set": {"db_channel_id": text}}, upsert=True)
        await message.reply_text(f"✅ DB channel set ho gaya: `{text}`")

    elif action == "awaiting_settimer":
        if parse_time(text) is None:
            return await message.reply_text("❌ Format galat hai. Use: 10s, 5m, 1h, 1d — dobara bhej.")
        await settings_col.update_one({"_id": "config"}, {"$set": {"default_timer": text}}, upsert=True)
        await message.reply_text(f"✅ Default timer set ho gaya: `{text}`")

    elif action == "awaiting_autodelete":
        if text.lower() == "off":
            await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_seconds": 0}}, upsert=True)
            await message.reply_text("✅ Auto-delete band kar diya.")
        else:
            seconds = parse_time(text)
            if seconds is None:
                return await message.reply_text("❌ Format galat hai. Use: 10m, 1h, ya `off` — dobara bhej.")
            await settings_col.update_one({"_id": "config"}, {"$set": {"auto_delete_seconds": seconds}}, upsert=True)
            await message.reply_text(f"✅ Files ab {text} baad auto-delete hongi.")

    elif action == "awaiting_gentoken":
        parts = text.split()
        if len(parts) < 3:
            return await message.reply_text("❌ Format galat hai. Use: `<start_id> <end_id> <name>` — dobara bhej.")
        start_raw, end_raw, name = parts[0], parts[1], parts[2]
        if not (start_raw.isdigit() and end_raw.isdigit()):
            return await message.reply_text("❌ start_id aur end_id number hone chahiye — dobara bhej.")
        start_id, end_id = int(start_raw), int(end_raw)
        if end_id < start_id:
            return await message.reply_text("❌ end_id, start_id se chota nahi ho sakta — dobara bhej.")

        config = await get_config()
        expiry_str = config.get("default_timer", "1h")
        seconds = parse_time(expiry_str) or 3600

        token_id = f"Roxie-{name}"
        file_ids = list(range(start_id, end_id + 1))
        await tokens_col.update_one(
            {"token_id": token_id},
            {"$set": {
                "token_id": token_id,
                "expiry_time": datetime.now() + timedelta(seconds=seconds),
                "files": file_ids,
                "revoked": False,
            }},
            upsert=True
        )
        await message.reply_text(
            f"🔥 **Token Generated!**\n\n**Token:** `{token_id}`\n"
            f"**Files:** {len(file_ids)}\n**Expires in:** {expiry_str}"
        )

    elif action == "awaiting_revoke":
        token_id = text if text.startswith("Roxie-") else f"Roxie-{text}"
        result = await tokens_col.update_one({"token_id": token_id}, {"$set": {"revoked": True}})
        if result.matched_count == 0:
            await message.reply_text(f"❌ Token `{token_id}` mila hi nahi.")
        else:
            await message.reply_text(f"✅ Token `{token_id}` revoke kar diya gaya.")

    if action != "awaiting_setfsub":  # setfsub ke baad link ka wait chalu rehna chahiye
        pending_action.pop(user_id, None)


# ==========================================
# 📤 FILE SENDING (+ AUTO-DELETE)
# ==========================================

async def schedule_delete(client, chat_id, message_id, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass  # message pehle hi delete ho chuki ho sakti hai


async def send_batch(client, chat_id, token_data, offset):
    db_channel_id = await get_db_channel_id()
    if not db_channel_id:
        return await client.send_message(chat_id, "❌ DB Channel set nahi hai. Admin ko batao.")

    config = await get_config()
    protect = config.get("content_protection", False)
    auto_delete_seconds = config.get("auto_delete_seconds", 0)

    all_files = token_data["files"]
    batch = all_files[offset: offset + PAGE_SIZE]
    if not batch:
        return await client.send_message(chat_id, "❌ Aur files nahi hain.")

    for msg_id in batch:
        try:
            sent = await client.copy_message(
                chat_id=chat_id, from_chat_id=db_channel_id, message_id=msg_id,
                protect_content=protect
            )
            if auto_delete_seconds > 0:
                asyncio.create_task(schedule_delete(client, chat_id, sent.id, auto_delete_seconds))
            await asyncio.sleep(0.7)
        except Exception as e:
            await client.send_message(chat_id, f"⚠️ File ID {msg_id} bhejne me error: {e}")

    next_offset = offset + PAGE_SIZE
    total_files = len(all_files)
    if next_offset < total_files:
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton(
            "Next ⏭", callback_data=f"next_{token_data['token_id']}_{next_offset}"
        )]])
        await client.send_message(chat_id, f"({min(next_offset, total_files)}/{total_files} files sent)", reply_markup=buttons)

    if auto_delete_seconds > 0:
        warn = await client.send_message(chat_id, f"⚠️ Ye files {auto_delete_seconds // 60 if auto_delete_seconds >= 60 else auto_delete_seconds}{'m' if auto_delete_seconds >= 60 else 's'} me delete ho jayengi, jaldi save kar lo.")


@app.on_callback_query(filters.regex(r"^next_"))
async def next_batch_callback(client, callback_query):
    parts = callback_query.data.split("_")
    offset = int(parts[-1])
    token = "_".join(parts[1:-1])

    token_data = await tokens_col.find_one({"token_id": token})
    if not token_data or token_data.get("revoked"):
        return await callback_query.answer("❌ Token revoke ho chuka hai.", show_alert=True)
    if datetime.now() > token_data["expiry_time"]:
        return await callback_query.answer("⏳ Token expired ho chuka hai.", show_alert=True)

    await callback_query.answer("Sending next batch...")
    await send_batch(client, callback_query.message.chat.id, token_data, offset=offset)


# ==========================================
# 🚀 USER FLOW: /start -> 🧑‍💻 + Join/Verify -> 🪪 -> token -> 📤/❌
# ==========================================

@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    config = await get_config()
    fsub_link = config.get("fsub_link")

    if fsub_link and not await is_fsub_joined(client, message.from_user.id):
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=fsub_link)],
            [InlineKeyboardButton("✅ Verify", callback_data="verify_fsub")],
        ])
        return await message.reply_text("🧑‍💻", reply_markup=buttons)

    await message.reply_text("🪪")


@app.on_callback_query(filters.regex(r"^verify_fsub$"))
async def verify_fsub_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if await is_fsub_joined(client, user_id):
        await callback_query.answer("✅ Verified!")
        await callback_query.message.edit_text("🪪")
    else:
        await callback_query.answer("❌ Abhi bhi join nahi kiya hai. Pehle join kar.", show_alert=True)


@app.on_message(filters.private & filters.text & filters.regex(r"^Roxie-") & ~filters.user(ADMIN_ID))
async def handle_token_input(client, message):
    user_id = message.from_user.id
    token = message.text.strip()

    if not await is_fsub_joined(client, user_id):
        config = await get_config()
        fsub_link = config.get("fsub_link")
        buttons = None
        if fsub_link:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=fsub_link)],
                [InlineKeyboardButton("✅ Verify", callback_data="verify_fsub")],
            ])
        return await message.reply_text("❌ Pehle channel join karo.", reply_markup=buttons)

    token_data = await tokens_col.find_one({"token_id": token})

    if not token_data or token_data.get("revoked") or datetime.now() > token_data["expiry_time"]:
        return await message.reply_text("❌")

    await message.reply_text("📤")
    await send_batch(client, message.chat.id, token_data, offset=0)


if __name__ == "__main__":
    print("RoxieCloudbot is alive!")
    app.run()
