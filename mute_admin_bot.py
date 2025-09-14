#!/usr/bin/env python3
# mute_admin_bot_no_db.py  (no DB, in-memory only)
import logging
import os
from typing import Optional, Dict, Set

from telegram import Update, User
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- CONFIG ----------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_ID_ENV = os.environ.get("OWNER_ID")  # numeric Telegram id (recommended)
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable.")
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- In-memory stores (lost on restart) ---
# Structure: { chat_id: set(user_id, ...) }
MUTED: Dict[int, Set[int]] = {}
ALLOWED_ADMINS: Dict[int, Set[int]] = {}
# Global owner (int or None) - if OWNER_ID_ENV set then it takes precedence
_owner_in_memory: Optional[int] = None

# ----- Helpers -----
def get_owner() -> Optional[int]:
    """Return numeric owner id. Priority: OWNER_ID env var, else in-memory claimed owner."""
    if OWNER_ID_ENV:
        try:
            return int(OWNER_ID_ENV)
        except Exception:
            return None
    return _owner_in_memory

def is_authorized(chat_id: int, user_id: int) -> bool:
    owner = get_owner()
    if owner and user_id == owner:
        return True
    # allowed admin for this chat?
    return user_id in ALLOWED_ADMINS.get(chat_id, set())

async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[User]:
    """Resolve from reply, @username or numeric id (best-effort)."""
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    args = context.args
    if not args:
        return None
    arg = args[0]
    chat_id = update.effective_chat.id
    try:
        if arg.startswith("@"):
            member = await context.bot.get_chat_member(chat_id, arg)
            return member.user
        else:
            uid = int(arg)
            member = await context.bot.get_chat_member(chat_id, uid)
            return member.user
    except Exception:
        return None

# ----- Command handlers -----
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I'm alive (no DB mode).\n"
        "Set OWNER_ID env var to lock ownership across restarts.\n"
        "Commands: /myid, /claimowner, /allowadmin, /disallowadmin, /listallowed, /m, /um, /listmuted"
    )

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your numeric Telegram id is: `{update.effective_user.id}`", parse_mode="Markdown")

async def claimowner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _owner_in_memory
    if OWNER_ID_ENV:
        await update.message.reply_text("OWNER_ID is set as an environment variable; you cannot claim ownership.")
        return
    if _owner_in_memory:
        await update.message.reply_text("Owner already claimed in-memory. Restart clears it.")
        return
    _owner_in_memory = update.effective_user.id
    await update.message.reply_text("You claimed ownership (in-memory). Note: this will be lost if the bot restarts.")

# owner-only: add allowed admin for a chat
async def allowadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return
    owner = get_owner()
    if not owner or caller.id != owner:
        await update.message.reply_text("Only the bot owner can add allowed admins.")
        return
    target = await resolve_target_user(update, context)
    if not target:
        await update.message.reply_text("Reply to the user or provide @username / id: /allowadmin @user")
        return
    ALLOWED_ADMINS.setdefault(chat.id, set()).add(target.id)
    await update.message.reply_text(f"✅ {target.full_name} is now an allowed admin in this chat (in-memory).")

async def disallowadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return
    owner = get_owner()
    if not owner or caller.id != owner:
        await update.message.reply_text("Only the bot owner can remove allowed admins.")
        return
    target = await resolve_target_user(update, context)
    if not target:
        await update.message.reply_text("Reply to the user or provide @username / id: /disallowadmin @user")
        return
    ALLOWED_ADMINS.get(chat.id, set()).discard(target.id)
    await update.message.reply_text(f"✅ {target.full_name} removed from allowed admins (in-memory).")

async def listallowed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return
    users = ALLOWED_ADMINS.get(chat.id, set())
    if not users:
        await update.message.reply_text("No allowed admins (in-memory).")
        return
    lines = []
    for uid in users:
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            lines.append(f"- {member.user.full_name} (`{uid}`)")
        except Exception:
            lines.append(f"- `{uid}`")
    await update.message.reply_text("Allowed admins (in-memory):\n" + "\n".join(lines), parse_mode="Markdown")

# mute/unmute/list (owner or allowed admin only)
async def muteadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return
    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("You are not authorized to use this bot (owner or allowed admin only).")
        return
    target = await resolve_target_user(update, context)
    if not target:
        await update.message.reply_text("Reply to the user or provide @username / id: /m @user")
        return
    MUTED.setdefault(chat.id, set()).add(target.id)
    await update.message.reply_text(f"✅ {target.full_name} added to auto-delete list (in-memory).")

async def unmuteadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return
    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("You are not authorized to use this bot (owner or allowed admin only).")
        return
    target = await resolve_target_user(update, context)
    if not target:
        await update.message.reply_text("Reply to the user or provide @username / id: /un @user")
        return
    MUTED.get(chat.id, set()).discard(target.id)
    await update.message.reply_text(f"✅ {target.full_name} removed from auto-delete list (in-memory).")

async def listmuted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return
    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("You are not authorized to use this bot (owner or allowed admin only).")
        return
    users = MUTED.get(chat.id, set())
    if not users:
        await update.message.reply_text("No users are auto-muted in this chat (in-memory).")
        return
    lines = []
    for uid in users:
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            lines.append(f"- {member.user.full_name} (`{uid}`)")
        except Exception:
            lines.append(f"- `{uid}`")
    await update.message.reply_text("Auto-delete list (in-memory):\n" + "\n".join(lines), parse_mode="Markdown")

# auto-delete handler
async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    sender = msg.from_user
    if not sender:
        return
    if sender.id in MUTED.get(chat.id, set()):
        try:
            await context.bot.delete_message(chat.id, msg.message_id)
            logger.info("Deleted message %s from user %s in chat %s", msg.message_id, sender.id, chat.id)
        except Exception as e:
            logger.warning("Failed to delete message: %s", e)

# --- Main ---
def main():
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
    app.add_handler(CommandHandler("m", muteadmin))
    app.add_handler(CommandHandler("un", unmuteadmin))
    app.add_handler(CommandHandler("listmuted", listmuted))

    # catch all messages
    app.add_handler(MessageHandler(filters.ALL, on_any_message))

    logger.info("Starting bot (no DB)...")
    app.run_polling()

if __name__ == "__main__":
    main()
