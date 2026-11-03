import asyncio
import logging
import sqlite3
import os
import threading
from flask import Flask
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from hydrogram import Client
from hydrogram.handlers import MessageHandler
from pytgcalls import PyTgCalls

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# --- CONFIGURATION ---
BOT_TOKEN = "9240427294:AAFus1ftYAAK7kLjkDd2nUYMkJ3_xGTEqOU"  # Apna Bot Token dalein
API_ID = 35271075                                           # API ID
API_HASH = "e8c3704b92160afdc08a81c1156197cc"               # API Hash
OWNER_ID = 8416917212                                       # Aapki Telegram User ID

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- FLASK SERVER (For Render Port Binding) ---
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Bot is active and running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# --- DATABASE SETUP ---
db = sqlite3.connect("bot_database.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT,
    session_string TEXT UNIQUE,
    owner INTEGER
);
CREATE TABLE IF NOT EXISTS auto_react (
    owner INTEGER,
    chat_id TEXT,
    emoji TEXT,
    PRIMARY KEY(owner, chat_id)
);
""")
db.commit()

# --- RUNTIME STATES ---
RUNNING_CLIENTS = {}  # tg_id -> (Client, PyTgCalls)
AUTO_REACT = {}       # (owner, chat_id) -> emoji

class AddSessionState(StatesGroup):
    waiting_for_session = State()

class AutoReactState(StatesGroup):
    waiting_chat = State()
    waiting_emoji = State()

# --- AUTO REACT HANDLER ---
def make_auto_react_handler(owner):
    async def on_channel_post(client, message):
        chat_id = message.chat.id
        emoji = AUTO_REACT.get((owner, chat_id)) or AUTO_REACT.get((OWNER_ID, chat_id))
        if emoji:
            try:
                await client.send_reaction(chat_id, message.id, emoji)
            except Exception as e:
                logger.debug(f"Auto react error: {e}")
    return on_channel_post

async def start_client_from_db(tg_id, session_string, owner):
    try:
        app = Client(
            f"client_{tg_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            in_memory=True
        )
        await app.start()
        calls = PyTgCalls(app)
        await calls.start()
        
        app.add_handler(MessageHandler(make_auto_react_handler(owner), filters=None))
        
        RUNNING_CLIENTS[tg_id] = (app, calls)
        logger.info(f"✅ Account started: {tg_id}")
    except Exception as e:
        logger.error(f"❌ Failed to start client {tg_id}: {e}")

async def load_all_data():
    rows = db.execute("SELECT id, session_string, owner FROM clients").fetchall()
    for tg_id, session, owner in rows:
        await start_client_from_db(tg_id, session, owner or OWNER_ID)
    
    AUTO_REACT.clear()
    for owner, chat_id, emoji in db.execute("SELECT owner, chat_id, emoji FROM auto_react").fetchall():
        AUTO_REACT[(owner, int(chat_id))] = emoji

# --- BOT COMMANDS ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Yeh bot sirf owner ke liye hai.")
        return
    
    total = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    online = len(RUNNING_CLIENTS)
    
    await message.answer(
        f"🤖 **Multi-Account Voice Chat & Auto-React Bot**\n\n"
        f"📱 Total Accounts: **{total}**\n"
        f"🟢 Online Accounts: **{online}**\n\n"
        f"**Commands:**\n"
        f"➕ Naya account add karein: `/addsession`\n"
        f"📊 Accounts status dekhein: `/status`\n"
        f"⚡ Auto-React set karein: `/autoreact`\n"
        f"🎧 Voice Chat join karein: `/join @username`\n"
        f"🔇 Voice Chat leave karein: `/leave @username`",
        parse_mode="Markdown"
    )

@dp.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    total = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    online = len(RUNNING_CLIENTS)
    offline = total - online
    
    rows = db.execute("SELECT id, name FROM clients").fetchall()
    details = "\n".join(f"• {name or 'User'} (`{tg_id}`) — {'🟢 Online' if tg_id in RUNNING_CLIENTS else '🔴 Offline'}" for tg_id, name in rows) or "No accounts added yet."
    
    await message.answer(
        f"📊 **Accounts Status Summary**\n\n"
        f"📱 Total: **{total}** | 🟢 Online: **{online}** | 🔴 Offline: **{offline}**\n\n"
        f"**Accounts List:**\n{details}",
        parse_mode="Markdown"
    )

@dp.message(Command("addsession"))
async def cmd_add_session(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    await state.set_state(AddSessionState.waiting_for_session)
    await message.answer("🔑 Apni Hydrogram/Pyrogram Session String yahan bhejein:\n\n*(Cancel karne ke liye /cancel likhein)*")

@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Action cancelled.")

@dp.message(AddSessionState.waiting_for_session)
async def receive_session_string(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    
    session_str = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    
    status_msg = await message.answer("⏳ Checking session string...")
    
    try:
        temp_app = Client("temp_login", api_id=API_ID, api_hash=API_HASH, session_string=session_str, in_memory=True)
        await temp_app.start()
        me = await temp_app.get_me()
        await temp_app.stop()
        
        # 🔴 Duplicate Account Check
        existing = db.execute("SELECT id FROM clients WHERE id=?", (me.id,)).fetchone()
        if existing:
            await status_msg.edit_text("❌ Aapne pehle se yeh account add kiya hai! Naya account add karo.")
            await state.clear()
            return
        
        name = " ".join(x for x in [me.first_name, me.last_name] if x) or "NoName"
        
        db.execute("INSERT INTO clients (id, name, session_string, owner) VALUES (?, ?, ?, ?)",
                   (me.id, name, session_str, message.from_user.id))
        db.commit()
        
        await start_client_from_db(me.id, session_str, message.from_user.id)
        
        await status_msg.edit_text(
            f"✅ **Account Successfully Added!**\n\n"
            f"👤 Name: {name}\n"
            f"🆔 ID: `{me.id}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Invalid session string ya error: `{e}`", parse_mode="Markdown")
    
    await state.clear()

@dp.message(Command("autoreact"))
async def cmd_autoreact(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    await state.set_state(AutoReactState.waiting_chat)
    await message.answer("⚡ Auto-React set karne ke liye channel ka username ya Chat ID bhejein (e.g., `@channel` ya `-100...`):")

@dp.message(AutoReactState.waiting_chat)
async def autoreact_chat(message: Message, state: FSMContext):
    await state.update_data(chat=message.text.strip())
    await state.set_state(AutoReactState.waiting_emoji)
    await message.answer("Ab woh emoji bhejein jisse naye posts par auto-react karna hai (e.g., 👍 ya 🔥):")

@dp.message(AutoReactState.waiting_emoji)
async def autoreact_emoji(message: Message, state: FSMContext):
    data = await state.get_data()
    chat_target = data["chat"]
    emoji = message.text.strip()
    uid = message.from_user.id
    
    try:
        if not RUNNING_CLIENTS:
            await message.answer("❌ Pehle koi account add karein (`/addsession`)!")
            await state.clear()
            return
            
        first_app = list(RUNNING_CLIENTS.values())[0][0]
        chat_obj = await first_app.get_chat(chat_target)
        chat_id = chat_obj.id
        
        AUTO_REACT[(uid, chat_id)] = emoji
        db.execute("INSERT OR REPLACE INTO auto_react (owner, chat_id, emoji) VALUES (?, ?, ?)",
                   (uid, str(chat_id), emoji))
        db.commit()
        
        await message.answer(f"✅ Auto-React enabled for chat `{chat_id}` with emoji {emoji}!", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Error setting auto-react: {e}")
    
    await state.clear()

# --- ULTRA-FAST VOICE CHAT JOIN ---
@dp.message(Command("join"))
async def cmd_join(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Usage: `/join @channel_username ya -100xxxxxxxxxx`", parse_mode="Markdown")
        return
    
    target_chat = args[1].strip()
    if not RUNNING_CLIENTS:
        await message.answer("❌ Ek bhi account online nahi hai!")
        return

    status_msg = await message.answer(f"⚡ Fast joining Voice Chat with {len(RUNNING_CLIENTS)} accounts simultaneously...", parse_mode="Markdown")
    
    # Resolve chat ID
    first_app = list(RUNNING_CLIENTS.values())[0][0]
    try:
        resolved_chat = await first_app.get_chat(target_chat)
        chat_id = resolved_chat.id
    except Exception as e:
        await status_msg.edit_text(f"❌ Invalid chat ID / Username: `{e}`", parse_mode="Markdown")
        return

    # Single client join helper
    async def join_single(tg_id, app, calls):
        try:
            await calls.play(chat_id)
            return True
        except Exception as e:
            logger.error(f"Client {tg_id} join error: {e}")
            return False

    # Parallel join using asyncio.gather for maximum speed
    tasks = [join_single(tg_id, app, calls) for tg_id, (app, calls) in RUNNING_CLIENTS.items()]
    results = await asyncio.gather(*tasks)
    
    success = sum(1 for r in results if r)
    fail = len(results) - success

    await status_msg.edit_text(
        f"⚡ **Fast Join Complete!**\n\n"
        f"🟢 Success: **{success}**\n"
        f"🔴 Failed: **{fail}**",
        parse_mode="Markdown"
    )

@dp.message(Command("leave"))
async def cmd_leave(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Usage: `/leave @channel_username ya -100xxxxxxxxxx`", parse_mode="Markdown")
        return
    
    target_chat = args[1].strip()
    status_msg = await message.answer(f"⏳ Leaving Voice Chat...", parse_mode="Markdown")
    
    first_app = list(RUNNING_CLIENTS.values())[0][0]
    try:
        resolved_chat = await first_app.get_chat(target_chat)
        chat_id = resolved_chat.id
    except Exception:
        chat_id = target_chat

    async def leave_single(tg_id, app, calls):
        try:
            await calls.leave_call(chat_id)
            return True
        except Exception:
            return False

    tasks = [leave_single(tg_id, app, calls) for tg_id, (app, calls) in RUNNING_CLIENTS.items()]
    await asyncio.gather(*tasks)

    await status_msg.edit_text("✅ Saare accounts ne voice chat leave kar diya hai.")

async def main():
    # Start Flask server in a separate background thread
    threading.Thread(target=run_flask, daemon=True).start()
    print("🌐 Flask server started for port binding...")
    
    print("📂 Loading data from database...")
    await load_all_data()
    print("🤖 Starting Telegram Bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    