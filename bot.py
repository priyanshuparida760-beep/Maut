"""
Telegram Multi-Account Manager & Voice Chat Joiner
"""

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import suppress
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from hydrogram import Client
from hydrogram.errors import (
    FloodWait,
    UsernameInvalid,
    UsernameNotOccupied,
    UserAlreadyParticipant,
)
from hydrogram.raw.functions.phone import JoinGroupCall, LeaveGroupCall
from hydrogram.raw.types import InputGroupCall, InputPeerSelf

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9240427294:AAFus1ftYAAK7kLjkDd2nUYMkJ3_xGTEqOU").strip()
API_ID_RAW = os.getenv("API_ID", "35271075").strip()
API_HASH = os.getenv("API_HASH", "e8c3704b92160afdc08a81c1156197cc").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "8416917212").strip()
DB_PATH = os.getenv("DB_PATH", "telegram_manager.db")

if not all((BOT_TOKEN, API_ID_RAW, API_HASH, OWNER_ID_RAW)):
    raise RuntimeError("Missing config.")

try:
    API_ID = int(API_ID_RAW)
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError as exc:
    raise RuntimeError("API_ID and OWNER_ID must be numbers.") from exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("telegram-manager")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ---------------- DATABASE ----------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    session_string TEXT NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
db.commit()

clients: dict[int, Client] = {}

def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)

def get_account_rows():
    return db.execute("SELECT user_id, display_name FROM accounts ORDER BY added_at").fetchall()

def clean_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("https://t.me/"):
        target = target.removeprefix("https://t.me/").strip("/")
    elif target.startswith("http://t.me/"):
        target = target.removeprefix("http://t.me/").strip("/")
    if target.startswith("@"):
        target = target[1:]
    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", target):
        raise ValueError("Public username sahi nahi hai. Example: @publicgroup")
    return target

async def start_saved_client(user_id: int, session_string: str) -> tuple[bool, str]:
    if user_id in clients:
        return True, "Already online"

    client = Client(
        name=f"account_{user_id}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.start()
        me = await client.get_me()
        clients[user_id] = client
        label = " ".join(part for part in (me.first_name, me.last_name) if part) or str(user_id)
        db.execute("UPDATE accounts SET display_name = ? WHERE user_id = ?", (label, user_id))
        db.commit()
        return True, f"Online as {label} ({user_id})"
    except Exception as exc:
        with suppress(Exception):
            await client.stop()
        return False, f"{type(exc).__name__}: {exc}"

async def load_saved_accounts():
    for user_id, session_string in db.execute("SELECT user_id, session_string FROM accounts").fetchall():
        await start_saved_client(user_id, session_string)

class AddSession(StatesGroup):
    waiting_session = State()

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_owner(message):
        return
    total = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    online = len(clients)
    await message.answer(
        f"🤖 **Multi-Account Manager**\n\n"
        f"📱 Saved: **{total}** | 🟢 Online: **{online}**\n\n"
        "Commands:\n"
        "/addsession\n"
        "/status\n"
        "/sessions\n"
        "/join @username (Join Voice Chat with ALL accounts)\n"
        "/leave @username\n"
        "/removesession ID",
        parse_mode="Markdown"
    )

@dp.message(Command("addsession"))
async def add_session_cmd(message: Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.set_state(AddSession.waiting_session)
    await message.answer("Apna session string bhejo:")

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.clear()
    await message.answer("Cancelled.")

@dp.message(AddSession.waiting_session)
async def receive_session(message: Message, state: FSMContext):
    if not is_owner(message):
        return
    session_string = (message.text or "").strip()
    with suppress(Exception):
        await message.delete()

    status = await message.answer("⏳ Verifying...")
    try:
        temp = Client("temp_login", api_id=API_ID, api_hash=API_HASH, session_string=session_string, in_memory=True, no_updates=True)
        await temp.start()
        me = await temp.get_me()
        await temp.stop()

        display_name = " ".join(part for part in (me.first_name, me.last_name) if part) or str(me.id)
        db.execute("INSERT OR REPLACE INTO accounts (user_id, display_name, session_string) VALUES (?, ?, ?)", (me.id, display_name, session_string))
        db.commit()

        ok, info = await start_saved_client(me.id, session_string)
        await status.edit_text(f"✅ Saved: {display_name} (`{me.id}`)\n{info}", parse_mode="Markdown")
    except Exception as exc:
        await status.edit_text(f"❌ Error: {exc}")
    finally:
        await state.clear()

@dp.message(Command("status"))
async def status_cmd(message: Message):
    if not is_owner(message):
        return
    rows = get_account_rows()
    await message.answer(f"Total: {len(rows)} | Online: {len(clients)}")

@dp.message(Command("sessions"))
async def sessions_cmd(message: Message):
    if not is_owner(message):
        return
    rows = get_account_rows()
    if not rows:
        return await message.answer("No sessions saved.")
    lines = [f"• {name} (`{uid}`) — {'🟢 Online' if uid in clients else '🔴 Offline'}" for uid, name in rows]
    await message.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("join"))
async def join_cmd(message: Message):
    if not is_owner(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        return await message.answer("Use: /join @channel_username")
    if not clients:
        return await message.answer("❌ Koi account online nahi hai!")

    status_msg = await message.answer(f"⚡ Joining Voice Chat with {len(clients)} accounts...")
    try:
        username = clean_target(args[1])
    except ValueError as e:
        return await status_msg.edit_text(f"❌ {e}")

    first_client = list(clients.values())[0]
    try:
        chat = await first_client.get_chat(username)
        chat_id = chat.id
    except Exception as e:
        return await status_msg.edit_text(f"❌ Chat resolve nahi hui: {e}")

    async def join_single(user_id, client):
        try:
            await client.get_chat(chat_id)
            full_chat = await client.invoke(
                client.raw.functions.channels.GetFullChannel(
                    channel=await client.resolve_peer(chat_id)
                ) if str(chat_id).startswith("-100") else client.raw.functions.messages.GetFullChat(
                    chat_id=int(str(chat_id).replace("-", ""))
                )
            )
            call = full_chat.full_chat.call
            if not call:
                return "No Active Voice Chat"

            await client.invoke(
                JoinGroupCall(
                    call=InputGroupCall(call.id, call.access_hash),
                    join_as=InputPeerSelf(),
                    muted=True
                )
            )
            return True
        except UserAlreadyParticipant:
            return True
        except Exception as exc:
            return str(exc)

    import hydrogram.raw.functions.channels
    import hydrogram.raw.functions.messages

    tasks = [join_single(uid, cl) for uid, cl in clients.items()]
    results = await asyncio.gather(*tasks)

    success_list = [r for r in results if r is True]
    error_reasons = [r for r in results if r is not True]

    success = len(success_list)
    fail = len(error_reasons)
    err_text = f"\n\n⚠️ Error: `{error_reasons[0]}`" if error_reasons else ""

    await status_msg.edit_text(
        f"⚡ **Voice Chat Join Complete!**\n\n"
        f"🟢 Success: **{success}**\n"
        f"🔴 Failed: **{fail}**{err_text}",
        parse_mode="Markdown"
    )

@dp.message(Command("leave"))
async def leave_cmd(message: Message):
    if not is_owner(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        return await message.answer("Use: /leave @channel_username")
    if not clients:
        return await message.answer("❌ Koi account online nahi hai!")

    status_msg = await message.answer("⏳ Leaving Voice Chat...")
    try:
        username = clean_target(args[1])
    except ValueError as e:
        return await status_msg.edit_text(f"❌ {e}")

    first_client = list(clients.values())[0]
    try:
        chat = await first_client.get_chat(username)
        chat_id = chat.id
    except Exception:
        chat_id = username

    async def leave_single(user_id, client):
        try:
            await client.get_chat(chat_id)
            full_chat = await client.invoke(
                client.raw.functions.channels.GetFullChannel(
                    channel=await client.resolve_peer(chat_id)
                )
            )
            call = full_chat.full_chat.call
            if call:
                await client.invoke(
                    LeaveGroupCall(
                        call=InputGroupCall(call.id, call.access_hash)
                    )
                )
            return True
        except Exception:
            return False

    tasks = [leave_single(uid, cl) for uid, cl in clients.items()]
    await asyncio.gather(*tasks)
    await status_msg.edit_text("✅ Sabhi accounts ne voice chat leave kar di hai.")

@dp.message(Command("removesession"))
async def remove_session_cmd(message: Message):
    if not is_owner(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Use: /removesession ID")
    user_id = int(args[1])
    client = clients.pop(user_id, None)
    if client:
        with suppress(Exception):
            await client.stop()
    db.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
    db.commit()
    await message.answer(f"🗑️ Removed `{user_id}`", parse_mode="Markdown")

async def main():
    await load_saved_accounts()
    log.info("Bot polling started")
    try:
        await dp.start_polling(bot)
    finally:
        for client in list(clients.values()):
            with suppress(Exception):
                await client.stop()
        db.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
