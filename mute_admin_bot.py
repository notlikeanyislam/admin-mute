#!/usr/bin/env python3
"""
mute_admin_bot.py — webhook-ready version (no DB, in-memory).

Environment variables required:
- TELEGRAM_BOT_TOKEN (required)
- WEBHOOK_URL (required)  -> e.g. https://<your-service>.onrender.com/webhook
Optional:
- WEBHOOK_PATH (optional) -> path portion e.g. /webhook (if not set, run_webhook uses webhook_url)
- OWNER_ID (optional)     -> numeric Telegram id (recommended)
- DELETE_RATE_PER_SECOND (optional) -> int, default 15
- DEBOUNCE_WINDOW_SECONDS (optional) -> float, default 0.6
- MAX_QUEUE_SIZE (optional) -> int, default 4000
- SPAM_NOTIFY_THRESHOLD (optional) -> int, default 20
"""
import asyncio
import logging
import json
import os
import re
import time
from collections import defaultdict, deque
from typing import Optional, Dict, Set

from telegram import Update, User
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- CONFIG / env ----------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH")
OWNER_ID_ENV = os.environ.get("OWNER_ID")

if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable.")
if not WEBHOOK_URL:
    raise RuntimeError("Set WEBHOOK_URL environment variable (e.g. https://your-service.onrender.com/webhook).")

DELETE_RATE_PER_SECOND = int(os.environ.get("DELETE_RATE_PER_SECOND", "15"))
DEBOUNCE_WINDOW_SECONDS = float(os.environ.get("DEBOUNCE_WINDOW_SECONDS", "0.6"))
MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "4000"))
SPAM_NOTIFY_THRESHOLD = int(os.environ.get("SPAM_NOTIFY_THRESHOLD", "20"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----- Helpers: Markdown escape -----
_MDV2_ESCAPE = re.compile(r'([_*\[\]\(\)~`>#+\-=|{}.!\\])')


def escape_markdown_v2(text: str) -> str:
    if text is None:
        return ""
    return _MDV2_ESCAPE.sub(r'\\\1', text)


def format_user(user: Optional[User]) -> str:
    """Return human-friendly string for a telegram.User using MarkdownV2-safe escaping."""
    if not user:
        return "<unknown>"
    uname = f"@{escape_markdown_v2(user.username)}" if getattr(user, "username", None) else ""
    full = escape_markdown_v2(user.full_name)
    return f"{full} {uname} (`{user.id}`)"


# --- In-memory stores (lost on restart) ---
MUTED: Dict[int, Set[int]] = {}
ALLOWED_ADMINS: Dict[int, Set[int]] = {}
_owner_in_memory: Optional[int] = None

# queue & debounce state
_delete_queue: Optional[asyncio.Queue] = None
_last_seen_by_user: dict[tuple[int, int], float] = defaultdict(float)
_pending_messages_by_user: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=50))
_user_spam_counters: dict[tuple[int, int], int] = defaultdict(int)


# ----- Queue helpers -----
def _ensure_delete_queue() -> asyncio.Queue:
    global _delete_queue
    if _delete_queue is None:
        # bounded queue to avoid unbounded memory growth
        _delete_queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    return _delete_queue


def _drop_oldest_and_put(q: asyncio.Queue, item):
    """If queue full, remove oldest and put the new item."""
    try:
        q.get_nowait()
    except Exception:
        # if we fail to pop, continue (put_nowait below may raise)
        pass
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        # last resort: schedule a background put (shouldn't usually happen)
        try:
            asyncio.get_event_loop().create_task(q.put(item))
        except Exception:
            logger.exception("Failed to enqueue delete after dropping oldest")


def _enqueue_delete(app, chat_id: int, message_id: int, user_id: int):
    q = _ensure_delete_queue()
    item = (chat_id, message_id, user_id)
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        _drop_oldest_and_put(q, item)


# ----- Owner / auth helpers -----
def get_owner() -> Optional[int]:
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
    return user_id in ALLOWED_ADMINS.get(chat_id, set())


# ----- Resolve target user -----
async def resolve_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[User]:
    """Resolve a target user from a reply, numeric id, or username (@username or username)."""
    if update.message is None:
        return None

    # 1) If command is a reply, use that (most reliable)
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user

    args = context.args or []
    if not args:
        return None
    raw = args[0].strip()
    if not raw:
        return None

    chat = update.effective_chat
    if not chat:
        return None

    logger.info("resolve_target_user: chat=%s caller=%s raw_arg=%r", chat.id, update.effective_user.id, raw)

    # 2) Try numeric id
    try:
        uid = int(raw)
    except Exception:
        uid = None

    if uid is not None:
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            logger.info("resolve_target_user: resolved by id %s -> %s", uid, member.user.id)
            return member.user
        except Exception as exc:
            logger.info("resolve_target_user: get_chat_member by id failed (%s): %s", uid, exc)

    # 3) Username attempts (strip leading @)
    lookup = raw.lstrip("@")
    tries = [lookup, f"@{lookup}"]
    for attempt in tries:
        try:
            member = await context.bot.get_chat_member(chat.id, attempt)
            logger.info("resolve_target_user: resolved by username %r -> %s", attempt, member.user.id)
            return member.user
        except Exception as exc:
            logger.info("resolve_target_user: get_chat_member(%r) failed: %s", attempt, exc)
    # nothing found
    return None


# ----- Delete worker / queueing -----
async def _delete_worker(app):
    q = _ensure_delete_queue()
    base_interval = 1.0 / max(1, DELETE_RATE_PER_SECOND)
    bot = app.bot

    backoff_multiplier = 1.0
    min_backoff = base_interval
    max_backoff = 10.0

    while True:
        try:
            chat_id, msg_id, user_id = await q.get()
            try:
                await bot.delete_message(chat_id, msg_id)
                logger.debug("Deleted msg %s from user %s in chat %s", msg_id, user_id, chat_id)
                # gentle reduce backoff on success
                backoff_multiplier = max(1.0, backoff_multiplier * 0.95)
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 1.0))
                logger.warning("Rate limited by Telegram: retry_after=%.2f, re-enqueueing.", wait)
                # re-enqueue and sleep for recommended period
                try:
                    q.put_nowait((chat_id, msg_id, user_id))
                except asyncio.QueueFull:
                    _drop_oldest_and_put(q, (chat_id, msg_id, user_id))
                await asyncio.sleep(wait)
                backoff_multiplier = min(8.0, backoff_multiplier * 2.0)
            except TelegramError as e:
                # e.g., BadRequest if message already deleted, Forbidden, etc.
                logger.debug("TelegramError during delete: %s", e)
                # small sleep to avoid busy loop on repeated errors
                await asyncio.sleep(min_backoff)
            except Exception as e:
                logger.exception("Unexpected delete error: %s", e)
                backoff_multiplier = min(8.0, backoff_multiplier * 1.5)
                await asyncio.sleep(min(max_backoff, base_interval * backoff_multiplier))
            else:
                await asyncio.sleep(min(max_backoff, base_interval * backoff_multiplier))
        except asyncio.CancelledError:
            logger.info("Delete worker cancelled, exiting")
            break
        except Exception:
            logger.exception("Delete worker top-level error, sleeping briefly.")
            await asyncio.sleep(1.0)


# ----- Command handlers -----
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I'm alive (webhook mode).\n"
        "Commands: /myid, /claimowner, /allowadmin, /disallowadmin, /listallowed, /m, /un, /listmuted"
    )


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your numeric Telegram id is: `{update.effective_user.id}`", parse_mode="MarkdownV2")


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


async def dumpallowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    caller = update.effective_user
    if owner and caller.id != owner:
        await update.message.reply_text("Owner only.")
        return
    await update.message.reply_text("ALLOWED_ADMINS raw:\n" + json.dumps({str(k): list(v) for k, v in ALLOWED_ADMINS.items()}))


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

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        target = await resolve_target_user(update, context)

    if not target:
        await update.message.reply_text("Reply to the user or provide @username / id: /allowadmin @user")
        return

    ALLOWED_ADMINS.setdefault(chat.id, set()).add(target.id)
    logger.info("allowadmin: caller=%s chat=%s added_user=%s id=%s", caller.id, chat.id, target.full_name, target.id)
    await update.message.reply_text(
        f"✅ Added allowed admin: {format_user(target)} (in-memory).",
        parse_mode="MarkdownV2"
    )


async def whois_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not context.args:
        await update.message.reply_text("Usage: /whois <numeric_id>")
        return

    try:
        uid = int(context.args[0].strip())
    except ValueError:
        await update.message.reply_text("Pass a numeric id, e.g. /whois 123456789")
        return

    try:
        member = await context.bot.get_chat_member(chat.id, uid)
        uname = f"@{escape_markdown_v2(member.user.username)}" if getattr(member.user, "username", None) else ""
        await update.message.reply_text(f"User: {escape_markdown_v2(member.user.full_name)} {uname} (`{uid}`)", parse_mode="MarkdownV2")
    except Exception as e:
        logger.info("whois: failed to resolve %s in chat %s: %s", uid, chat.id if chat else None, e)
        await update.message.reply_text(f"Could not resolve user `{uid}` in this chat: {escape_markdown_v2(str(e))}", parse_mode="MarkdownV2")
        return


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
    await update.message.reply_text(f"✅ {format_user(target)} removed from allowed admins (in-memory).", parse_mode="MarkdownV2")


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
    for uid in sorted(users):
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            lines.append(f"- {format_user(member.user)}")
        except Exception:
            lines.append(f"- `{uid}` (could not resolve name)")
    logger.info("listallowed called by %s in chat %s -> %s", update.effective_user.id, chat.id, list(users))
    await update.message.reply_text("Allowed admins (in-memory):\n" + "\n".join(lines), parse_mode="MarkdownV2")


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
    await update.message.reply_text(f"✅ {format_user(target)} added to auto-delete list (in-memory).", parse_mode="MarkdownV2")


async def unmuteadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return

    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("You are not authorized to use this bot (owner or allowed admin only).")
        return

    chat_set = MUTED.setdefault(chat.id, set())

    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user

    if not target_user and context.args:
        arg = context.args[0].strip()
        try:
            uid = int(arg)
            if uid in chat_set:
                chat_set.discard(uid)
                await update.message.reply_text(f"✅ User id `{uid}` removed from auto-delete list (if present).", parse_mode="MarkdownV2")
            else:
                await update.message.reply_text(f"User id `{uid}` was not in auto-delete list.", parse_mode="MarkdownV2")
            return
        except ValueError:
            if arg.startswith("@"):
                try:
                    member = await context.bot.get_chat_member(chat.id, arg)
                    target_user = member.user
                except Exception:
                    await update.message.reply_text("Couldn't resolve that @username in this chat. Try replying to their message or pass their numeric id.")
                    return

    if not target_user:
        await update.message.reply_text("Reply to the user or provide @username / id: /un @user or /un 123456789")
        return

    uid = target_user.id
    logger.info("unmute by user object: caller=%s chat=%s target=%s", caller.id, chat.id, uid)
    if uid in chat_set:
        chat_set.discard(uid)
        await update.message.reply_text(f"✅ {format_user(target_user)} removed from auto-delete list (in-memory).", parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(f"User {format_user(target_user)} is not in the auto-delete list.", parse_mode="MarkdownV2")


async def unall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    caller = update.effective_user
    if not chat or chat.type not in ("group", "supergroup"):
        await update.message.reply_text("This command only works in groups/supergroups.")
        return
    if not is_authorized(chat.id, caller.id):
        await update.message.reply_text("Only owner/allowed admins can do this.")
        return
    MUTED.pop(chat.id, None)
    await update.message.reply_text("✅ Cleared all auto-muted users in this chat (in-memory).")


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
        await update.message.reply_text("No users are auto-muted in this chat.")
        return

    lines = []
    for uid in sorted(users):
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            lines.append(f"- {format_user(member.user)}")
        except Exception:
            lines.append(f"- `{uid}` (could not resolve name)")
    logger.info("listmuted called by %s in chat %s -> muted_ids=%s", caller.id, chat.id, list(users))
    await update.message.reply_text("Auto-delete list:\n" + "\n".join(lines), parse_mode="MarkdownV2")


async def dumpmuted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    caller = update.effective_user
    if owner and caller.id != owner:
        await update.message.reply_text("Owner only.")
        return
    await update.message.reply_text("MUTED raw:\n" + json.dumps({str(k): list(v) for k, v in MUTED.items()}))


# auto-delete handler (with admin-flush)
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

        pending = _pending_messages_by_user[key]
        pending.append(msg.message_id)
        _last_seen_by_user[key] = now

        _user_spam_counters[key] += 1
        if _user_spam_counters[key] == SPAM_NOTIFY_THRESHOLD:
            owner = get_owner()
            if owner:
                try:
                    await context.bot.send_message(owner, f"⚠️ Heavy spam detected from user `{sender.id}` in chat `{chat.id}`. Consider demoting or removing them.", parse_mode="MarkdownV2")
                except Exception:
                    pass

        # If muted sender is an admin/creator => flush all pending immediately
        try:
            member = await context.bot.get_chat_member(chat.id, sender.id)
            if member.status in ("administrator", "creator"):
                while pending:
                    mid = pending.popleft()
                    _enqueue_delete(context.application, chat.id, mid, sender.id)
                _user_spam_counters[key] = 0
                _last_seen_by_user[key] = 0.0
                return
        except Exception:
            # could not resolve member -> continue with normal flow
            pass

        # otherwise schedule a debounce flush (collapse to newest)
        async def _flush_after_delay(app, k, delay):
            await asyncio.sleep(delay)
            last = _last_seen_by_user.get(k, 0.0)
            if time.time() - last >= delay:
                pend = _pending_messages_by_user.get(k)
                if not pend:
                    return
                newest_mid = None
                while pend:
                    newest_mid = pend.popleft()
                if newest_mid:
                    _enqueue_delete(app, k[0], newest_mid, k[1])
                _user_spam_counters[k] = 0
                _last_seen_by_user[k] = 0.0

        try:
            # Use Application.create_task when available
            context.application.create_task(_flush_after_delay(context.application, key, DEBOUNCE_WINDOW_SECONDS))
        except Exception:
            asyncio.create_task(_flush_after_delay(context.application, key, DEBOUNCE_WINDOW_SECONDS))


# ----- Startup helper: set webhook & start worker -----
async def _start_background_workers(app):
    # ensure webhook is set (delete old webhook first)
    try:
        await app.bot.delete_webhook()
        # set webhook explicitly so Telegram will POST to your URL
        await app.bot.set_webhook(WEBHOOK_URL)
        logger.info("Webhook set to %s", WEBHOOK_URL)
    except Exception:
        logger.exception("Failed to set webhook (continuing; run logs to debug)")

    # start delete worker
    try:
        app.create_task(_delete_worker(app))
    except Exception:
        asyncio.get_running_loop().create_task(_delete_worker(app))


# --- Main ---
def main():
    app = ApplicationBuilder().token(TOKEN).post_init(_start_background_workers).build()

    # command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("claimowner", claimowner_cmd))
    app.add_handler(CommandHandler("dumpmuted", dumpmuted))

    app.add_handler(CommandHandler("allowadmin", allowadmin_cmd))
    app.add_handler(CommandHandler("disallowadmin", disallowadmin_cmd))
    app.add_handler(CommandHandler("listallowed", listallowed_cmd))
    app.add_handler(CommandHandler("dumpallowed", dumpallowed))

    app.add_handler(CommandHandler("m", muteadmin))
    app.add_handler(CommandHandler("un", unmuteadmin))
    app.add_handler(CommandHandler("listmuted", listmuted))
    app.add_handler(CommandHandler("unall", unall_cmd))

    app.add_handler(MessageHandler(filters.ALL, on_any_message))
    app.add_handler(CommandHandler("whois", whois_cmd))

    # Derive webhook_path (optional) and port
    webhook_path = WEBHOOK_PATH or None
    port = int(os.environ.get("PORT", os.environ.get("RENDER_PORT", "8443")))

    logger.info("Starting webhook server (listening on 0.0.0.0:%s)", port)
    # run webhook: listen on all interfaces; Render will provide HTTPS externally
    app.run_webhook(listen="0.0.0.0", port=port, webhook_url=WEBHOOK_URL, webhook_path=webhook_path)


if __name__ == "__main__":
    main()
