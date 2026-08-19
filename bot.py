import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient

# ==========================================
# 🔧 ENV LOAD (Ye missing tha - bot isi wajah se start nahi ho raha tha)
# ==========================================
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
MONGODB_URL = os.environ.get("MONGODB_URL")
ADMIN_ID = os.environ.get("ADMIN_ID")
DB_CHANNEL_ID = os.environ.get("DB_CHANNEL_ID")  # fallback agar /setdb nahi chala

# Fail fast agar zaroori env vars missing hain - silent crash se better hai clear error
missing = [name for name, val in {
    "BOT_TOKEN": BOT_TOKEN, "API_ID": API_ID, "API_HASH": API_HASH,
    "MONGODB_URL": MONGODB_URL, "ADMIN_ID": ADMIN_ID
}.items() if not val]
if missing:
    raise SystemExit(f"❌ .env me ye missing hai: {', '.join(missing)}")

API_ID = int(API_ID)
ADMIN_ID = int(ADMIN_ID)  # filters.user() ko int chahiye, string nahi (ye bug tha)

# Bot aur Database ka setup
app = Client("RoxieCloudbot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

db_client = AsyncIOMotorClient(MONGODB_URL)
db = db_client["RoxieDB"]
settings_col = db["settings"]
tokens_col = db["tokens"]

PAGE_SIZE = 10  # ek batch me kitni files bhejni hain


# ==========================================
# ⏱ Time parse karne ka jugad (10s, 5m, 1h, 1d)
# ==========================================
def parse_time(time_str):
    """Returns seconds, ya None agar format galat hai."""
    if not time_str or len(time_str) < 2:
        return None
    time_dict = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    unit = time_str[-1].lower()
    number_part = time_str[:-1]
    if unit in time_dict and number_part.isdigit():
        return int(number_part) * time_dict[unit]
    return None


async def get_db_channel_id():
    """DB channel ID uthata hai - pehle Mongo se, warna .env fallback."""
    config = await settings_col.find_one({"_id": "config"})
    if config and config.get("db_channel_id"):
        return int(config["db_channel_id"])
    if DB_CHANNEL_ID:
        return int(DB_CHANNEL_ID)
    return None


# ==========================================
# 🛠 IN-BOT ADMIN PANEL
# ==========================================

@app.on_message(filters.command("setfsub") & filters.user(ADMIN_ID))
async def set_fsub(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Format: /setfsub <channel_id>")
    fsub_id = message.command[1]
    await settings_col.update_one({"_id": "config"}, {"$set": {"fsub_id": fsub_id}}, upsert=True)
    await message.reply_text(f"✅ FSUB ID update ho gayi: {fsub_id}")


@app.on_message(filters.command("setdb") & filters.user(ADMIN_ID))
async def set_db(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Format: /setdb <channel_id>")
    db_id = message.command[1]
    await settings_col.update_one({"_id": "config"}, {"$set": {"db_channel_id": db_id}}, upsert=True)
    await message.reply_text(f"✅ Database Channel ID update ho gayi: {db_id}")


@app.on_message(filters.command("settimer") & filters.user(ADMIN_ID))
async def set_timer(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Format: /settimer <10s|5m|1h|1d>")
    default_timer = message.command[1]
    if parse_time(default_timer) is None:
        return await message.reply_text("❌ Time format galat hai. Use: 10s, 5m, 1h, 1d")
    await settings_col.update_one({"_id": "config"}, {"$set": {"default_timer": default_timer}}, upsert=True)
    await message.reply_text(f"✅ Default Expiry Timer set ho gaya: {default_timer}")


# ==========================================
# 🔑 TOKEN GENERATION LOGIC
# ==========================================

@app.on_message(filters.command("token") & filters.user(ADMIN_ID))
async def generate_token(client, message):
    args = message.command[1:]
    if len(args) < 3:
        return await message.reply_text(
            "Bhai format galat hai.\n"
            "Use: `/token <start_msg_id> <end_msg_id> -<name> [time(optional)]`\n"
            "Example: `/token 101 112 -CuteGirl 1h`"
        )

    start_id_raw, end_id_raw = args[0], args[1]
    if not (start_id_raw.isdigit() and end_id_raw.isdigit()):
        return await message.reply_text("❌ start_msg_id aur end_msg_id number hone chahiye.")

    start_id, end_id = int(start_id_raw), int(end_id_raw)
    if end_id < start_id:
        return await message.reply_text("❌ end_msg_id, start_msg_id se chota nahi ho sakta.")

    token_name = ""
    expiry_str = ""

    # Naam aur time nikaalna baaki args se
    for arg in args[2:]:
        if arg.startswith("-"):
            token_name = arg[1:]
        elif parse_time(arg) is not None:
            expiry_str = arg

    if not token_name:
        return await message.reply_text("❌ Token ka naam do `-name` format me. Example: -CuteGirl")

    # Agar custom time nahi diya, DB se default utha lo
    if not expiry_str:
        config = await settings_col.find_one({"_id": "config"})
        expiry_str = config.get("default_timer", "1h") if config else "1h"

    seconds_to_expire = parse_time(expiry_str)
    if seconds_to_expire is None:
        # Fallback safety - agar DB me bhi galat value pade ho to crash nahi karega
        seconds_to_expire = 3600
        expiry_str = "1h"

    expiry_time = datetime.now() + timedelta(seconds=seconds_to_expire)
    final_token = f"Roxie-{token_name}"

    file_ids = list(range(start_id, end_id + 1))

    token_data = {
        "token_id": final_token,
        "expiry_time": expiry_time,
        "files": file_ids,
    }
    await tokens_col.update_one({"token_id": final_token}, {"$set": token_data}, upsert=True)

    await message.reply_text(
        f"🔥 **Token Generated Successfully!**\n\n"
        f"**Token:** `{final_token}`\n"
        f"**Total Files:** {len(file_ids)}\n"
        f"**Expires In:** {expiry_str}\n\n"
        f"**Bot Link:** `https://t.me/{(await client.get_me()).username}?start={final_token}`"
    )


# ==========================================
# 📤 FILE SENDING HELPER
# ==========================================

async def send_batch(client, chat_id, token_data, offset):
    """DB channel se ek batch (PAGE_SIZE files) copy karke user ko bhejta hai."""
    db_channel_id = await get_db_channel_id()
    if not db_channel_id:
        return await client.send_message(chat_id, "❌ DB Channel set nahi hai. Admin ko /setdb chalane ko bolo.")

    all_files = token_data["files"]
    batch = all_files[offset: offset + PAGE_SIZE]

    if not batch:
        return await client.send_message(chat_id, "❌ Aur files nahi hain.")

    sent_count = 0
    for msg_id in batch:
        try:
            await client.copy_message(chat_id=chat_id, from_chat_id=db_channel_id, message_id=msg_id)
            sent_count += 1
            await asyncio.sleep(0.7)  # flood-wait se bachne ke liye chota delay
        except Exception as e:
            await client.send_message(chat_id, f"⚠️ File ID {msg_id} bhejne me error: {e}")

    next_offset = offset + PAGE_SIZE
    total_files = len(all_files)

    buttons = []
    if next_offset < total_files:
        buttons.append([InlineKeyboardButton(
            "Next ⏭", callback_data=f"next_{token_data['token_id']}_{next_offset}"
        )])
    buttons.append([InlineKeyboardButton("Backup Channel 🔗", url="https://t.me/TeraBackupChannel")])

    await client.send_message(
        chat_id,
        f"✅ Batch complete! ({min(next_offset, total_files)}/{total_files} files sent)",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ==========================================
# 🚀 USER START & BATCH PAGINATION
# ==========================================

@app.on_message(filters.command("start"))
async def start(client, message):
    if len(message.command) > 1:
        token = message.command[1]

        token_data = await tokens_col.find_one({"token_id": token})
        if not token_data:
            return await message.reply_text("❌ Bhai ye token galat hai ya exist nahi karta.")

        if datetime.now() > token_data["expiry_time"]:
            return await message.reply_text("⏳ Ouch! Token expired ho chuka hai bhai.")

        total_files = len(token_data["files"])
        await message.reply_text(f"✅ Token verified! Sending first {min(PAGE_SIZE, total_files)} files...")

        await send_batch(client, message.chat.id, token_data, offset=0)
    else:
        await message.reply_text("Hello bhai! Main RoxieCloudbot hu. Mujhe ek valid token ke sath start karo.")


@app.on_callback_query(filters.regex(r"^next_"))
async def next_batch_callback(client, callback_query):
    # callback_data format: next_<token_id>_<offset>
    # token me khud "-" ho sakta hai isliye rsplit se end se split karo
    parts = callback_query.data.split("_")
    offset = int(parts[-1])
    token = "_".join(parts[1:-1])

    token_data = await tokens_col.find_one({"token_id": token})
    if not token_data:
        return await callback_query.answer("❌ Token nahi mila.", show_alert=True)

    if datetime.now() > token_data["expiry_time"]:
        return await callback_query.answer("⏳ Token expired ho chuka hai.", show_alert=True)

    await callback_query.answer("Sending next batch...")
    await send_batch(client, callback_query.message.chat.id, token_data, offset=offset)


if __name__ == "__main__":
    print("RoxieCloudbot is alive!")
    app.run()
