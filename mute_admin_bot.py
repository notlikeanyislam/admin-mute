#!/usr/bin/env python3
# mute_admin_bot.py
import logging
import sqlite3
import os
from contextlib import closing
from typing import Optional

from telegram import Update, User
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- CONFIG ----------
DB_PATH = os.environ.get("DB_PATH", "muted_admins.db")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_ID_ENV = os.environ.get("OWNER_ID")  # optional: set to your numeric Telegram user id
if not TOKEN:
    raise RuntimeError("Set the TELEGRAM_BOT_TOKEN environment variable.")
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----- DB Helpers -----
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS muted (
                chat_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY(chat_id, user_id)
            )"""
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_admins (
                chat_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY(chat_id, user_id)
            )"""
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS owner (
                owner_id INTEGER PRIMARY KEY
            )"""
        )
        con.commit()

def add_muted(chat_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO muted(chat_id,user_id) VALUES (?,?)", (chat_id, user_id))
        con.commit()

def remove_muted(chat_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM muted WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        con.commit()

def list_muted(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("SELECT user_id FROM muted WHERE chat_id=?", (chat_id,))
        return [row[0] for row in cur.fetchall()]

def is_muted(chat_id: int, user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM muted WHERE chat_id=? AND user_id=? LIMIT 1", (chat_id, user_id))
        return cur.fetchone() is not None

# allowed-admins helpers
def add_allowed_admin(chat_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO allowed_admins(chat_id,user_id) VALUES (?,?)", (chat_id, user_id))
        con.commit()

def remove_allowed_admin(chat_id: int, user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM allowed_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        con.commit()

def list_allowed_admins(chat_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("SELECT user_id FROM allowed_admins WHERE chat_id=?", (chat_id,))
        return [row[0] for row in cur.fetchall()]

def is_allowed_admin(chat_id: int, user_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM allowed_admins WHERE chat_id=? AND user_id=? LIMIT 1", (chat_id, user_id))
        return cur.fetchone() is not None

# owner helpers
def get_owner_from_db() -> Optional[int]:
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("SELECT owner_id FROM owner LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None

def set_owner_in_db(user_id: int):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO owner(owner_id) VALUES (?)", (user_id,))
        con.commit()

def clear_owner_in_db():
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.execute("DELETE FROM owner")
        con.commit()

# ----- Authorization -----
def get_owner() -> Optional[int]:
    """Return numeric owner id. Priority: OWNER_ID env var, else DB value."""
    if OWNER_ID_ENV:
        try:
            return int(OWNER_ID_ENV)
        except Exception:
            return None
    return get_owner_from_db()

def is_authorized(chat_id: int, user_id: int) -> bool:
    """Authorized if user is global owner or allowed admin for this chat."""
    owner = get_owner()
    if owner and user_id == owner:
        return True
    return is_allowed_admin(chat_id, user_id)

# ----- Utilities -----
async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[User]:
    """Resolve target user from reply, @username or id arg. Returns User or None."""
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    args = context.args
    if not args:
        return None
    arg = args[0]
    if arg.startswith("@"):
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, arg)
            return member.user
        except Exception:
            return None
    else:
        try:
            uid = int(arg)
            member = await context.bot.get_chat_member(update.effective_chat.id, uid)
            return member.user
        except Exception:
            return None

# ----- Command handlers -----
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I'm alive.\n"
        "Owner can set allowed admins with /allowadmin (reply or @username or id).\n"
        "Commands: /myid, /claimowner (if OWNER_ID not set), /allowadmin, /disallowadmin, /listallowed, /muteadmin, /unmuteadmin, /listmuted"
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Your numeric Telegram id is: `{uid}`", parse_mode="Markdown")

async def claimowner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Claim ownership only if no OWNER_ID env var and no owner in DB."""
    if OWNER_ID_ENV:
        await update.message.reply_text("OWNER_ID is set as an environment variable; cannot claim owner.")
        return
    current = get_owner_from_db()
    if current:
        await update.message.reply_text("Owner already set — cannot claim.")
        return
    caller = update.effective_user.id
    set_owner_in_db(caller)
    await update.message.reply_text("You are now the owner of this bot. (You can still set OWNER_ID env var if you prefer.)")

# owner-only admin management
async def allowadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command works only in groups/supergroups.")
        return

    # Only global owner can add allowed admins
    owner = get_owner()
    if not owner or caller.id != owner:
        await update.message.reply_text("Only the bot owner can add allowed admins. (Use /claimowner if OWNER_ID not set.)")
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text("Reply to the user or provide their @username or numeric id: /allowadmin @username")
        return

    add_allowed_admin(chat.id, target_user.id)
    await update.message.reply_text(f"✅ {target_user.full_name} is now an allowed admin in this chat.")

async def disallowadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command works only in groups/supergroups.")
        return

    owner = get_owner()
    if not owner or caller.id != owner:
        await update.message.reply_text("Only the bot owner can remove allowed admins.")
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text("Reply to the user or provide their @username or numeric id: /disallowadmin @username")
        return

    remove_allowed_admin(chat.id, target_user.id)
    await update.message.reply_text(f"✅ {target_user.full_name} removed from allowed admins in this chat.")

async def listallowed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command works only in groups/supergroups.")
        return

    users = list_allowed_admins(chat.id)
    if not users:
        await update.message.reply_text("No allowed admins in this chat.")
        return

    lines = []
    for uid in users:
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            lines.append(f"- {member.user.full_name} (`{uid}`)")
        except Exception:
            lines.append(f"- `{uid}`")
    await update.message.reply_text("Allowed admins:\n" + "\n".join(lines), parse_mode="Markdown")

# --- Existing mute admin commands but now check is_authorized ---
async def muteadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command works only in groups/supergroups.")
        return

    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("You are not authorized to use this bot. Only the owner and allowed admins can use it.")
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text("Reply to the user or provide their @username or numeric id: /muteadmin @username")
        return

    add_muted(chat.id, target_user.id)
    await update.message.reply_text(f"✅ Added {target_user.full_name} to auto-delete list in this chat.")

async def unmuteadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command works only in groups/supergroups.")
        return

    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("You are not authorized to use this bot. Only the owner and allowed admins can use it.")
        return

    target_user = await resolve_target_user(update, context)
    if not target_user:
        await update.message.reply_text("Reply to the user or provide their @username or numeric id: /unmuteadmin @username")
        return

    remove_muted(chat.id, target_user.id)
    await update.message.reply_text(f"✅ Removed {target_user.full_name} from auto-delete list in this chat.")

async def listmuted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command works only in groups/supergroups.")
        return

    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("You are not authorized to use this bot. Only the owner and allowed admins can use it.")
        return

    users = list_muted(chat.id)
    if not users:
        await update.message.reply_text("No users are auto-muted in this chat.")
        return

    lines = []
    for uid in users:
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            lines.append(f"- {member.user.full_name} (`{uid}`)")
        except Exception:
            lines.append(f"- `{uid}`")
    await update.message.reply_text("Auto-delete list:\n" + "\n".join(lines), parse_mode="Markdown")

# Message handler: auto-delete if muted (runs for any message)
async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    sender = msg.from_user
    if not sender:
        return
    if is_muted(chat.id, sender.id):
        try:
            await context.bot.delete_message(chat.id, msg.message_id)
            logger.info("Deleted message %s from user %s in chat %s", msg.message_id, sender.id, chat.id)
        except Exception as e:
            logger.warning("Failed to delete message: %s", e)

# --- Main ---
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    # info / owner helpers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("claimowner", claimowner_cmd))

    # owner-only allowed admin management
    app.add_handler(CommandHandler("allowadmin", allowadmin_cmd))
    app.add_handler(CommandHandler("disallowadmin", disallowadmin_cmd))
    app.add_handler(CommandHandler("listallowed", listallowed_cmd))

    # mute/unmute/list - only owner or allowed admins may call
    app.add_handler(CommandHandler("muteadmin", muteadmin))
    app.add_handler(CommandHandler("unmuteadmin", unmuteadmin))
    app.add_handler(CommandHandler("listmuted", listmuted))

    # catch ALL messages and attempt deletion if needed
    app.add_handler(MessageHandler(filters.ALL, on_any_message))

    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
