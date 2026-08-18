import asyncio
import re
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient

# Bot aur Database ka setup
app = Client("RoxieCloudbot", bot_token="TERA_BOT_TOKEN", api_id=12345, api_hash="TERA_API_HASH")

# MongoDB connection
db_client = AsyncIOMotorClient("TERA_MONGODB_URL")
db = db_client["RoxieDB"]
settings_col = db["settings"]
tokens_col = db["tokens"]

# Time parse karne ka chota sa jugad (10s, 5m, 1h, 1d)
def parse_time(time_str):
    time_dict = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    unit = time_str[-1].lower()
    if unit in time_dict and time_str[:-1].isdigit():
        return int(time_str[:-1]) * time_dict[unit]
    return None

# ==========================================
# 🛠 IN-BOT ADMIN PANEL (No Railway Visits!)
# ==========================================

@app.on_message(filters.command("setfsub") & filters.user("TERA_ADMIN_ID"))
async def set_fsub(client, message):
    fsub_id = message.command[1]
    await settings_col.update_one({"_id": "config"}, {"$set": {"fsub_id": fsub_id}}, upsert=True)
    await message.reply_text(f"✅ FSUB ID update ho gayi: {fsub_id}")

@app.on_message(filters.command("setdb") & filters.user("TERA_ADMIN_ID"))
async def set_db(client, message):
    db_id = message.command[1]
    await settings_col.update_one({"_id": "config"}, {"$set": {"db_channel_id": db_id}}, upsert=True)
    await message.reply_text(f"✅ Database Channel ID update ho gayi: {db_id}")

@app.on_message(filters.command("settimer") & filters.user("TERA_ADMIN_ID"))
async def set_timer(client, message):
    default_timer = message.command[1] # e.g., "1h"
    await settings_col.update_one({"_id": "config"}, {"$set": {"default_timer": default_timer}}, upsert=True)
    await message.reply_text(f"✅ Default Expiry Timer set ho gaya: {default_timer}")


# ==========================================
# 🔑 TOKEN GENERATION LOGIC
# ==========================================

@app.on_message(filters.command("token") & filters.user("TERA_ADMIN_ID"))
async def generate_token(client, message):
    args = message.command[1:]
    if len(args) < 2:
        return await message.reply_text("Bhai format galat hai. Use: /token [num] [batch(optional)] -[name] [time(optional)]")

    content_num = args[0]
    batch_size = 1  # Default single
    token_name = ""
    expiry_str = ""

    # Tera wala smart splitting logic
    for arg in args[1:]:
        if arg.isdigit():
            batch_size = int(arg) # Ye tera 15 (batch size) hai
        elif arg.startswith("-"):
            token_name = arg[1:] # Ye tera CuteGirl ya random text hai
        elif arg[-1].lower() in ['s', 'm', 'h', 'd']:
            expiry_str = arg # Ye tera time hai

    # Agar custom time nahi diya, to DB se default time utha lenge
    if not expiry_str:
        config = await settings_col.find_one({"_id": "config"})
        expiry_str = config.get("default_timer", "1h") if config else "1h"

    # Expiry time calculate karna
    seconds_to_expire = parse_time(expiry_str)
    expiry_time = datetime.now() + timedelta(seconds=seconds_to_expire)

    final_token = f"Roxie-{token_name}"
    
    # DB mein save kar rahe hain (Dummy file IDs ke sath abhi ke liye)
    token_data = {
        "token_id": final_token,
        "content_num": content_num,
        "batch_size": batch_size,
        "expiry_time": expiry_time,
        "files": [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112] # Ye DB channel se ayenge
    }
    await tokens_col.insert_one(token_data)

    await message.reply_text(
        f"🔥 **Token Generated Successfully!**\n\n"
        f"**Token:** `{final_token}`\n"
        f"**Batch Size:** {batch_size}\n"
        f"**Expires In:** {expiry_str}\n\n"
        f"**Bot Link:** `https://t.me/RoxieCloudbot?start={final_token}`"
    )

# ==========================================
# 🚀 USER START & BATCH PAGINATION
# ==========================================

@app.on_message(filters.command("start"))
async def start(client, message):
    if len(message.command) > 1:
        token = message.command[1] # e.g., Roxie-CuteGirl
        
        # Check in DB
        token_data = await tokens_col.find_one({"token_id": token})
        
        if not token_data:
            return await message.reply_text("❌ Bhai ye token galat hai ya exist nahi karta.")
        
        # Expiry Check
        if datetime.now() > token_data["expiry_time"]:
            return await message.reply_text("⏳ Ouch! Token expired ho chuka hai bhai.")

        # Yaha pe hum pehle 10 files bhejenge (Tera 10 ka 1 group logic)
        batch_size = 10 
        total_files = len(token_data["files"])
        
        await message.reply_text(f"✅ Token verified! Sending first {min(10, total_files)} files...")
        
        # Pagination Buttons
        buttons = []
        if total_files > 10:
            buttons.append([InlineKeyboardButton("Next ⏭", callback_data=f"next_{token}_10")])
        
        buttons.append([InlineKeyboardButton("Backup Channel 🔗", url="https://t.me/TeraBackupChannel")])
        
        reply_markup = InlineKeyboardMarkup(buttons)
        await message.reply_text("Batch 1 complete!", reply_markup=reply_markup)
        
    else:
        await message.reply_text("Hello bhai! Main RoxieCloudbot hu. Mujhe ek valid token ke sath start karo.")

if __name__ == "__main__":
    print("RoxieCloudbot is alive!")
    app.run()
    