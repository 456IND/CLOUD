# RoxieCloud — Telegram File Store Bot

Admin file upload karega → bot DB channel mein save karega → ek shareable link generate hoga. Koi bhi wo link kholega toh (FSUB verify ke baad) file bot se mil jayegi.

---

## 1. Setup — kya kya chahiye

### a) Bot Token
1. Telegram pe [@BotFather](https://t.me/BotFather) ko `/newbot` bhejo (ya apna existing `@RoxieCloudbot` use karo).
2. Token copy kar lo → `BOT_TOKEN`

### b) API_ID + API_HASH
Ye zaroori hai kyunki bade files (500MB–2GB) sirf **MTProto (Pyrogram)** handle kar sakta hai, normal Bot API nahi (uski 50MB limit hai).
1. [my.telegram.org](https://my.telegram.org) pe apne personal Telegram number se login karo.
2. "API Development Tools" → naya app banao.
3. `api_id` aur `api_hash` copy kar lo.

### c) MongoDB (Database)
Railway ka filesystem **ephemeral** hai — SQLite use karte toh redeploy pe saara data (file links, users, settings) delete ho jata. Isliye MongoDB use kiya hai jo Railway se bahar, persistent rehta hai.
1. [MongoDB Atlas](https://www.mongodb.com/cloud/atlas/register) pe free account banao.
2. Free (M0) cluster create karo.
3. Database user banao + connection string copy karo → `MONGO_URI`
4. Network Access mein `0.0.0.0/0` allow karo (Railway ka IP fixed nahi hota).

### d) Channels
Do channels chahiye:
- **DB Channel** — private channel jahan bot actual files store karega
- **FSUB Channel** — jahan users ko join karna mandatory hoga

Dono channels mein **bot ko admin banao** (post + delete permission DB channel mein, member list dekhne ki permission FSUB channel mein).

Channel ki numeric ID nikalne ke liye:
- Channel mein koi bhi message ko apne bot ko forward karo
- Usi forwarded message pe reply karke `/getid` bhejo (bot deploy hone ke baad, admin se)

### e) Admin User IDs
[@userinfobot](https://t.me/userinfobot) ko `/start` karke apna numeric ID le lo.

---

## 2. Railway pe Deploy

1. Ye repo Railway se connect karo (GitHub linked ho ya direct upload).
2. Railway → Settings → **Start Command**: `python bot.py`
3. Railway → **Variables** tab mein `.env.example` ke saare keys add karo (real values ke saath):
   - `BOT_TOKEN`
   - `API_ID`
   - `API_HASH`
   - `MONGO_URI`
   - `DB_CHANNEL_ID`
   - `FSUB_CHANNEL_ID`
   - `FSUB_INVITE_LINK`
   - `ADMIN_IDS`
4. Deploy trigger karo. Logs mein `Starting RoxieCloud Bot...` dikhna chahiye.

> Note: Railway par ye ek **background worker** ki tarah chalega (koi HTTP port expose nahi hota) — koi web server config karne ki zaroorat nahi.

---

## 3. Commands

| Command | Kaun use kare | Kya karta hai |
|---|---|---|
| `/start` | Sabhi users | Bot start, FSUB check, file deliver (agar link se aaya ho) |
| `/admin` | Sirf Admins | Admin panel kholta hai |
| `/broadcast` | Sirf Admins | Kisi message pe **reply** karke, sabko wo msg bhejta hai |
| `/getid` | Sirf Admins | Reply kiye gaye message/channel/user ki ID batata hai |

### Admin Panel features
- **Broadcast** — reply-based broadcast, saare users ko msg jayega
- **Debug** — `bot.log` se latest errors dikhata hai
- **Settings** — Auto-delete timer (10s/30s/1m/5m/15m/30m/2h/OFF), Share on/off, Save on/off, FSUB on/off
- **Edit** — FSUB channel ID / invite link change
- **GetId** — kisi bhi chat/user ki ID nikalna
- **Exit** — panel band

---

## 4. Important Limitations (chhupaya nahi hai, jaan lo)

1. **Share vs Save toggle — dono ek hi cheez control karte hain.** Telegram API sirf ek flag (`protect_content`) deta hai jo forward + save dono ek saath block karta hai. Inko independently control karna Telegram ki taraf se possible hi nahi hai. Agar dono mein se koi bhi setting OFF hai, delivered file forward/save nahi hogi.
2. **Auto-delete timer bot ki memory mein chalta hai.** Agar bot beech mein restart ho gaya (Railway redeploy, crash), pending scheduled deletes cancel ho jayenge. Persistent job queue (jaise Redis/APScheduler+DB) production-grade fix hai, abhi MVP scope mein nahi hai.
3. **"Link pe click karte hi auto-copy"** technically possible nahi hai — koi bot kisi client ka clipboard control nahi kar sakta. Link ko monospace/code format mein bheja hai, jisse Telegram mein ek tap se copy ho jata hai (standard behavior).
4. **FSUB check ke liye bot dono channels mein admin hona zaroori hai**, warna membership verify fail hoga.
5. Bade files (1-2GB) baar baar transfer karna — Railway ke free/hobby plan ka bandwidth aur memory limited hai, bahut zyada traffic pe upgrade karna pad sakta hai.

---

## 5. File Structure

```
.
├── bot.py            # Main bot logic
├── requirements.txt  # Python dependencies
├── .env.example       # Environment variable template
└── README.md          # Ye file
```
