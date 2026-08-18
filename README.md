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

### f) Batch Link Size (optional)
`MAX_BATCH_SIZE` env var se control hota hai ki ek `/batch` link mein max kitni files ho sakti hain. Default `50`.

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
| `/start` | Sabhi users | Bot start. Agar tum `ADMIN_IDS` mein ho, FSUB skip hoke seedha Admin welcome card + Admin Panel button milega. Normal users ke liye FSUB check, phir welcome + file deliver (agar link se aaya ho) |
| `/help` | Sabhi users | Bot ka use kaise kare, ye batata hai |
| `/admin` | Sirf Admins | Admin panel kholta hai |
| `/broadcast` | Sirf Admins | Kisi message pe **reply** karke, sabko wo msg bhejta hai |
| `/getid` | Sirf Admins | Reply kiye gaye message/channel/user ki ID batata hai |
| `/ban <user_id> [duration] [reason]` | Sirf Admins | User ko ban karta hai. Duration optional — `30m`, `2h`, `1d`. Na do toh permanent ban |
| `/unban <user_id>` | Sirf Admins | Ban hataata hai |
| `/batch <start_msg_id> <end_msg_id>` | Sirf Admins | DB channel ke ek message-ID range ko ek hi shareable link mein bundle karta hai. `start_msg_id`/`end_msg_id` upload confirmation mein milta hai, ya DB channel mein kisi message pe "Copy Message Link" se |
| `/speciallink <link_or_code> views <N>` / `time <duration>` | Sirf Admins | Kisi existing file/batch link ka ek naya, view-limited aur/ya time-limited variant banata hai — original link untouched rehta hai |
| `/speciallinkstats <link_or_token>` | Sirf Admins | Kisi special link ka current usage/status dikhata hai |

> **Note:** Admin ko FSUB join karne ki zaroorat nahi — `ADMIN_IDS` mein listed IDs automatically recognize hoti hain aur seedha Admin Panel button wala welcome card milta hai. Normal users ko ye button kabhi nahi dikhega.

## 4. Ban System

- `/ban <user_id> 2h spam` — 2 ghante ke liye ban, reason ke saath
- `/ban <user_id>` — permanent ban, koi reason nahi
- Temporary bans automatically expire hote hain (background sweeper har 60 second check karta hai) — no manual `/unban` zaroori
- Banned user `/start` karega toh seedha block message milega, koi FSUB ya file delivery nahi hogi
- Admins ko ban nahi kiya ja sakta

## 5. Batch Links

Ek link se files ka poora range deliver karna:
1. Files upload karo (normal tarike se) — har upload ke reply mein DB Message ID milega
2. `/batch <start_msg_id> <end_msg_id>` bhejo (jaise `/batch 42 50` — files 42 se 50 tak)
3. Bot ek single link degа jo saari files us range mein deliver karega

`MAX_BATCH_SIZE` (default 50) se zyada files ek batch mein nahi ja sakti.

## 6. Special (View/Time-Limited) Links

Kisi existing file ya batch link ka ek restricted variant banao, bina original link ko touch kiye:
- `/speciallink <link> views 5` — total milaake sirf 5 baar open ho sakegi (sab users milaake)
- `/speciallink <link> time 1h` — 1 ghante baad expire
- `/speciallink <link> views 5 time 1h` — dono, jo pehle poora ho wahi apply hoga
- `/speciallinkstats <link_or_token>` — current status/usage check karo

Limit poori hote hi link "invalid/expired" bata dega, original link pe koi asar nahi padega.

## 7. Restart-Safe Auto-Delete

Pehle auto-delete timer bot ki memory mein chalta tha — Railway restart/redeploy pe pending deletes cancel ho jaate the. Ab ye MongoDB mein persist hota hai aur ek background sweeper (har 15 second) check karta hai, so restart ke baad bhi due deletions properly execute hoti hain.

### Admin Panel features (ab sab kuch inline buttons se accessible hai)
- **Broadcast** — reply-based broadcast, saare users ko msg jayega
- **Debug** — `bot.log` se latest errors dikhata hai
- **Settings** — Auto-delete timer (10s/30s/1m/5m/15m/30m/2h/OFF), Share on/off, Save on/off, FSUB on/off
- **Edit** — FSUB channel ID / invite link change
- **Messages** — Welcome message aur FSUB Join message ka text customize karo, reset-to-default ke saath (bina redeploy)
- **GetId** — kisi bhi chat/user ki ID nikalna
- **Ban / Unban** — button se ban/unban karo, ya `/ban` `/unban` command se
- **Batch** — usage info dikhata hai (link `/batch <start> <end>` se banta hai — do numbers ek saath type karna easier hai command se)
- **Special Links** — usage info dikhata hai (`/speciallink`/`/speciallinkstats` se banta hai)
- **Exit** — panel band

---

## 4. Important Limitations (chhupaya nahi hai, jaan lo)

1. **Share vs Save toggle — dono ek hi cheez control karte hain.** Telegram API sirf ek flag (`protect_content`) deta hai jo forward + save dono ek saath block karta hai. Inko independently control karna Telegram ki taraf se possible hi nahi hai. Agar dono mein se koi bhi setting OFF hai, delivered file forward/save nahi hogi.
2. **"Link pe click karte hi auto-copy"** technically possible nahi hai — koi bot kisi client ka clipboard control nahi kar sakta. Link ko monospace/code format mein bheja hai, jisse Telegram mein ek tap se copy ho jata hai (standard behavior).
3. **FSUB check ke liye bot dono channels mein admin hona zaroori hai**, warna membership verify fail hoga.
4. Bade files (1-2GB) baar baar transfer karna — Railway ke free/hobby plan ka bandwidth aur memory limited hai, bahut zyada traffic pe upgrade karna pad sakta hai.
5. **Batch links `/protect` jaise per-file exemption support nahi karte** — global auto-delete setting hi poori batch pe apply hogi.

---

## 5. File Structure

```
.
├── bot.py            # Main bot logic
├── requirements.txt  # Python dependencies
├── .env.example       # Environment variable template
└── README.md          # Ye file
```
