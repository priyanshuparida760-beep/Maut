"""
Telegram Multi-Account Manager
- Add/remove Hydrogram sessions
- View account status
- Select ONE account for manually requested public chat join/leave
- Owner-only bot commands

IMPORTANT:
1. Never share session strings, API_HASH, or BOT_TOKEN.
2. Set BOT_TOKEN, API_ID, API_HASH, OWNER_ID as environment variables.
3. Keep this bot private. Session strings are equivalent to account credentials.
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
    InviteHashExpired,
    InviteHashInvalid,
    UsernameInvalid,
    UsernameNotOccupied,
    UserAlreadyParticipant,
)

# --- CONFIG ---
# Set these four values in your hosting provider's Environment Variables.
BOT_TOKEN = os.getenv("9240427294:AAFus1ftYAAK7kLjkDd2nUYMkJ3_xGTEqOU", "").strip()
API_ID_RAW = os.getenv("35271075", "").strip()
API_HASH = os.getenv("e8c3704b92160afdc08a81c1156197cc", "").strip()
OWNER_ID_RAW = os.getenv("8416917212", "").strip()
DB_PATH = os.getenv("DB_PATH", "telegram_manager.db")

if not all((BOT_TOKEN, API_ID_RAW, API_HASH, OWNER_ID_RAW)):
    raise RuntimeError(
        "Missing config. Add BOT_TOKEN, API_ID, API_HASH and OWNER_ID "
        "in your hosting provider's Environment Variables."
    )

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

# user_id -> Hydrogram Client
clients: dict[int, Client] = {}
active_account_id: Optional[int] = None
db_lock = asyncio.Lock()


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)


def get_account_rows():
    return db.execute(
        "SELECT user_id, display_name FROM accounts ORDER BY added_at"
    ).fetchall()


def get_saved_session(user_id: int):
    row = db.execute(
        "SELECT session_string FROM accounts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


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
    """Start a saved session and return a readable result."""
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
        label = " ".join(
            part for part in (me.first_name, me.last_name) if part
        ) or str(user_id)
        db.execute(
            "UPDATE accounts SET display_name = ? WHERE user_id = ?",
            (label, user_id),
        )
        db.commit()
        return True, f"Online as {label} ({user_id})"
    except Exception as exc:
        with suppress(Exception):
            await client.stop()
        log.exception("Could not start account %s", user_id)
        return False, f"{type(exc).__name__}: {exc}"


async def load_saved_accounts():
    for user_id, session_string in db.execute(
        "SELECT user_id, session_string FROM accounts"
    ).fetchall():
        ok, info = await start_saved_client(user_id, session_string)
        log.info("Account %s: %s", user_id, info)


async def get_active_client() -> tuple[Optional[int], Optional[Client]]:
    global active_account_id
    if active_account_id is None:
        return None, None
    client = clients.get(active_account_id)
    return active_account_id, client


# ---------------- FSM ----------------

class AddSession(StatesGroup):
    waiting_session = State()


# ---------------- COMMANDS ----------------

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not is_owner(message):
        return await message.answer("⛔ This bot is private.")
    total = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    online = len(clients)
    await message.answer(
        "🤖 <b>Telegram Account Manager</b>\n\n"
        f"📱 Saved accounts: <b>{total}</b>\n"
        f"🟢 Online: <b>{online}</b>\n\n"
        "<b>Commands</b>\n"
        "/addsession — add a session\n"
        "/status — account status\n"
        "/sessions — list accounts\n"
        "/use ACCOUNT_ID — select one account\n"
        "/join @username — join one public group/channel\n"
        "/leave @username — leave the selected chat\n"
        "/removesession ACCOUNT_ID — remove saved account\n"
        "/cancel — cancel current action\n\n"
        "<i>Join/leave commands operate on one selected account only.</i>",
        parse_mode="HTML",
    )


@dp.message(Command("addsession"))
async def add_session_cmd(message: Message, state: FSMContext):
    if not is_owner(message):
        return
    await state.set_state(AddSession.waiting_session)
    await message.answer(
        "Apna Hydrogram-compatible session string bhejo.\n"
        "⚠️ Ye password ki tarah sensitive hai. Kisi aur ko mat bhejna.\n"
        "Cancel: /cancel"
    )


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
    if not session_string or session_string.startswith("/"):
        return await message.answer("Valid session string bhejo, ya /cancel.")
    # Delete the incoming message where possible to reduce accidental exposure.
    with suppress(Exception):
        await message.delete()

    status = await message.answer("⏳ Session verify ho raha hai...")
    temp = Client(
        name="session_check",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )
    try:
        await temp.start()
        me = await temp.get_me()
        display_name = " ".join(
            part for part in (me.first_name, me.last_name) if part
        ) or str(me.id)
        await temp.stop()

        existing = db.execute(
            "SELECT 1 FROM accounts WHERE user_id = ?", (me.id,)
        ).fetchone()
        if existing:
            await status.edit_text("⚠️ Ye account pehle se saved hai.")
            await state.clear()
            return

        db.execute(
            "INSERT INTO accounts (user_id, display_name, session_string) "
            "VALUES (?, ?, ?)",
            (me.id, display_name, session_string),
        )
        db.commit()

        ok, info = await start_saved_client(me.id, session_string)
        await status.edit_text(
            f"✅ Account saved.\n"
            f"Name: {display_name}\n"
            f"ID: <code>{me.id}</code>\n"
            f"Runtime: {'🟢 Online' if ok else '🔴 Offline'}\n"
            f"{info}",
            parse_mode="HTML",
        )
    except Exception as exc:
        with suppress(Exception):
            await temp.stop()
        log.exception("Session verification failed")
        await status.edit_text(
            f"❌ Session verify nahi hua: {type(exc).__name__}: {exc}"
        )
    finally:
        await state.clear()


@dp.message(Command("status"))
async def status_cmd(message: Message):
    if not is_owner(message):
        return
    rows = get_account_rows()
    online = len(clients)
    lines = [
        f"📊 <b>Account status</b>",
        f"Total: <b>{len(rows)}</b>",
        f"Online: <b>{online}</b>",
        f"Offline: <b>{len(rows) - online}</b>",
    ]
    if active_account_id is not None:
        lines.append(f"Selected: <code>{active_account_id}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("sessions"))
async def sessions_cmd(message: Message):
    if not is_owner(message):
        return
    rows = get_account_rows()
    if not rows:
        return await message.answer("Abhi koi session saved nahi hai.")
    lines = ["<b>Saved accounts</b>"]
    for user_id, name in rows:
        state = "🟢 Online" if user_id in clients else "🔴 Offline"
        selected = " ⭐ selected" if user_id == active_account_id else ""
        lines.append(
            f"• {name} — <code>{user_id}</code> — {state}{selected}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("use"))
async def use_cmd(message: Message):
    global active_account_id
    if not is_owner(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Use: /use ACCOUNT_ID")
    user_id = int(args[1])
    if user_id not in clients:
        return await message.answer("Ye account online nahi hai. /status check karo.")
    active_account_id = user_id
    await message.answer(f"✅ Selected account: <code>{user_id}</code>", parse_mode="HTML")


@dp.message(Command("join"))
async def join_cmd(message: Message):
    if not is_owner(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        return await message.answer("Use: /join @publicusername")
    user_id, client = await get_active_client()
    if client is None:
        return await message.answer("Pehle /use ACCOUNT_ID se ek online account select karo.")
    try:
        username = clean_target(args[1])
        result = await client.join_chat(username)
        title = getattr(result, "title", None) or username
        await message.answer(
            f"✅ Selected account joined: <b>{title}</b>\n"
            f"Account: <code>{user_id}</code>",
            parse_mode="HTML",
        )
    except UserAlreadyParticipant:
        await message.answer("ℹ️ Ye account pehle se is chat ka member hai.")
    except (UsernameInvalid, UsernameNotOccupied):
        await message.answer("❌ Public username invalid hai ya chat nahi mila.")
    except (InviteHashExpired, InviteHashInvalid):
        await message.answer("❌ Invite link invalid/expired hai. Public @username try karo.")
    except FloodWait as exc:
        await message.answer(
            f"⏳ Telegram ne wait karne ko kaha: {getattr(exc, 'value', 'some time')} seconds."
        )
    except Exception as exc:
        log.exception("Join failed")
        await message.answer(f"❌ Join failed: {type(exc).__name__}: {exc}")


@dp.message(Command("leave"))
async def leave_cmd(message: Message):
    if not is_owner(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        return await message.answer("Use: /leave @publicusername")
    user_id, client = await get_active_client()
    if client is None:
        return await message.answer("Pehle /use ACCOUNT_ID se ek online account select karo.")
    try:
        username = clean_target(args[1])
        await client.leave_chat(username)
        await message.answer(
            f"✅ Selected account left: <b>@{username}</b>\n"
            f"Account: <code>{user_id}</code>",
            parse_mode="HTML",
        )
    except FloodWait as exc:
        await message.answer(
            f"⏳ Telegram ne wait karne ko kaha: {getattr(exc, 'value', 'some time')} seconds."
        )
    except Exception as exc:
        log.exception("Leave failed")
        await message.answer(f"❌ Leave failed: {type(exc).__name__}: {exc}")


@dp.message(Command("removesession"))
async def remove_session_cmd(message: Message):
    global active_account_id
    if not is_owner(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Use: /removesession ACCOUNT_ID")
    user_id = int(args[1])
    if user_id not in [row[0] for row in get_account_rows()]:
        return await message.answer("Account ID nahi mila.")
    client = clients.pop(user_id, None)
    if client:
        with suppress(Exception):
            await client.stop()
    db.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
    db.commit()
    if active_account_id == user_id:
        active_account_id = None
    await message.answer(f"🗑️ Account <code>{user_id}</code> removed.", parse_mode="HTML")


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
