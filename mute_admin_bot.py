#!/usr/bin/env python3
# mute_admin_bot_no_db.py  (no DB, in-memory only)
import logging
import asyncio
import time
from collections import defaultdict, deque
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
from telegram.error import RetryAfter, TelegramError


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


# ====== CONFIG for deletion worker ======
DELETE_RATE_PER_SECOND = 15        # increase for faster deletes (tune down if you hit limits)
DEBOUNCE_WINDOW_SECONDS = 0.6      # collapse bursts within this window
MAX_QUEUE_SIZE = 4000              # safety cap
SPAM_NOTIFY_THRESHOLD = 20         # number of messages queued from same user to treat as heavy spam
# =======================================

_delete_queue: "asyncio.Queue[tuple[int,int,int]]" = None
_last_seen_by_user: dict[tuple[int,int], float] = defaultdict(float)
_pending_messages_by_user: dict[tuple[int,int], deque[int]] = defaultdict(lambda: deque(maxlen=50))
_user_spam_counters: dict[tuple[int,int], int] = defaultdict(int)




# ----- Helpers -----

# Schedule the delete worker once the Application is running
async def _start_background_workers(app):
    """Called after Application starts — safe to create background tasks here."""
    # create the delete worker in the app's running loop
    try:
        app.create_task(_delete_worker(app))
    except Exception:
        # fallback: schedule on running loop
        asyncio.get_running_loop().create_task(_delete_worker(app))


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
    

async def _delete_worker(app):
    global _delete_queue
    if _delete_queue is None:
        _delete_queue = asyncio.Queue()
    base_interval = 1.0 / max(1, DELETE_RATE_PER_SECOND)
    bot = app.bot

    backoff_multiplier = 1.0
    min_backoff = base_interval
    max_backoff = 10.0

    while True:
        try:
            chat_id, msg_id, user_id = await _delete_queue.get()
            try:
                await bot.delete_message(chat_id, msg_id)
                logger.debug("Deleted msg %s from user %s in chat %s", msg_id, user_id, chat_id)
                backoff_multiplier = max(1.0, backoff_multiplier * 0.95)
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 1.0))
                logger.warning("Rate limited by Telegram: retry_after=%.2f, backing off.", wait)
                # re-enqueue after waiting the advised time
                await asyncio.sleep(wait)
                try:
                    await _delete_queue.put((chat_id, msg_id, user_id))
                except Exception:
                    logger.exception("Failed to re-enqueue after RetryAfter")
                backoff_multiplier = min(8.0, backoff_multiplier * 2.0)
            except TelegramError as e:
                logger.debug("TelegramError during delete: %s", e)
                await asyncio.sleep(min_backoff)
            except Exception as e:
                logger.exception("Unexpected delete error: %s", e)
                backoff_multiplier = min(8.0, backoff_multiplier * 1.5)
                await asyncio.sleep(min(max_backoff, base_interval * backoff_multiplier))
            else:
                await asyncio.sleep(min(max_backoff, base_interval * backoff_multiplier))
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Delete worker top-level error, sleeping briefly.")
            await asyncio.sleep(1.0)


def _enqueue_delete(app, chat_id: int, message_id: int, user_id: int):
    global _delete_queue
    if _delete_queue is None:
        _delete_queue = asyncio.Queue()

    try:
        qsize = _delete_queue.qsize()
    except Exception:
        qsize = 0

    if qsize >= MAX_QUEUE_SIZE:
        try:
            _delete_queue.get_nowait()
        except Exception:
            pass

    try:
        _delete_queue.put_nowait((chat_id, message_id, user_id))
    except asyncio.QueueFull:
        try:
            asyncio.get_event_loop().create_task(_delete_queue.put((chat_id, message_id, user_id)))
        except Exception:
            logger.exception("Failed to schedule enqueue for delete")


# ----- Command handlers -----
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I'm alive (no DB mode).\n"
        "Set OWNER_ID env var to lock ownership across restarts.\n"
        "Commands: /myid, /claimowner, /allowadmin, /disallowadmin, /listallowed, /m, /un, /listmuted"
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
        key = (chat.id, sender.id)
        now = time.time()

        # push id into pending buffer (maintain order)
        pending = _pending_messages_by_user[key]
        pending.append(msg.message_id)
        _last_seen_by_user[key] = now

        # increase spam counter
        _user_spam_counters[key] += 1
        # notify owner if spam is heavy
        if _user_spam_counters[key] == SPAM_NOTIFY_THRESHOLD:
            owner = get_owner()
            if owner:
                try:
                    await context.bot.send_message(owner, f"⚠️ Heavy spam detected from user `{sender.id}` in chat `{chat.id}`. Consider demoting or removing them.", parse_mode="Markdown")
                except Exception:
                    pass

        # --- NEW: if the muted sender is an admin or creator, flush all pending immediately ---
        try:
            member = await context.bot.get_chat_member(chat.id, sender.id)
            if member.status in ("administrator", "creator"):
                # enqueue *all* pending messages immediately (no debounce)
                while pending:
                    mid = pending.popleft()
                    _enqueue_delete(context.application, chat.id, mid, sender.id)
                # reset counters
                _user_spam_counters[key] = 0
                _last_seen_by_user[key] = 0.0
                return
        except Exception:
            # if get_chat_member fails, fall back to normal behavior
            pass

        # schedule a flush after debounce window (existing behavior)
        async def _flush_after_delay(app, k, delay):
            await asyncio.sleep(delay)
            last = _last_seen_by_user.get(k, 0.0)
            if time.time() - last >= delay:
                pend = _pending_messages_by_user.get(k)
                if not pend:
                    return
                # only keep the newest message id to delete (collapse)
                newest_mid = None
                while pend:
                    newest_mid = pend.popleft()
                if newest_mid:
                    _enqueue_delete(app, k[0], newest_mid, k[1])
                # reset spam counter and last seen
                _user_spam_counters[k] = 0
                _last_seen_by_user[k] = 0.0

        # schedule flush (non-blocking)
        try:
            context.application.create_task(_flush_after_delay(context.application, key, DEBOUNCE_WINDOW_SECONDS))
        except Exception:
            asyncio.create_task(_flush_after_delay(context.application, key, DEBOUNCE_WINDOW_SECONDS))



# --- Main ---
def main():
    # attach the startup callback so worker starts after the app's loop is running
    app = ApplicationBuilder().token(TOKEN).post_init(_start_background_workers).build()

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
