import logging
import os
import json
import random
import base64
import asyncio
import time
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, filters,
    CommandHandler, ConversationHandler, CallbackQueryHandler
)
from groq import Groq
from pymongo import MongoClient

# ═══════════════════════════════════════════════════
#   AI TASK BOT
# ═══════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MONGODB_URI = os.environ.get("MONGODB_URI")


def _parse_admin_ids():
    raw = (os.environ.get("ADMIN_ID", "") or "") + "," + (os.environ.get("ADMIN_IDS", "") or "")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                pass
    seen = set()
    unique = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


ADMIN_IDS = _parse_admin_ids()
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0

if not TELEGRAM_TOKEN: raise ValueError("Missing TELEGRAM_TOKEN.")
if not GROQ_API_KEY: raise ValueError("Missing GROQ_API_KEY.")
if not MONGODB_URI: raise ValueError("Missing MONGODB_URI.")

try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    print("✅ Database connected.")
    print(f"✅ Admins loaded: {ADMIN_IDS if ADMIN_IDS else 'NONE'}")
except Exception as e:
    print(f"❌ DB error: {e}")
    raise e

db = mongo_client["telegram_bot"]
collection = db["data"]

DEFAULTS = {
    "apps": {}, "history": {}, "proofs": {}, "saved_proofs": [],
    "users": {}, "withdrawals": [], "banned_users": [], "rejections": [],
    "used_screenshots": [], "pending_proofs": {}, "force_join_channel": None,
    "attempts": {},
    "comment_usage": {},
    "last_request": {},
    "auto_ban": {},
    "bulk_requests": [],
    "pending_bulk": {},
}


def load_data():
    try:
        doc = collection.find_one({"_id": "config"})
        if not doc:
            return {**DEFAULTS}
        doc.pop("_id", None)
        for k, v in DEFAULTS.items():
            if k not in doc:
                doc[k] = v if not isinstance(v, (dict, list)) else type(v)()
        return doc
    except Exception as e:
        print(f"load_data error: {e}")
        return {**DEFAULTS}


def save_data(data):
    try:
        data["_id"] = "config"
        collection.replace_one({"_id": "config"}, data, upsert=True)
    except Exception as e:
        print(f"save_data error: {e}")


client = Groq(api_key=GROQ_API_KEY)
VISION_MODEL = "qwen/qwen3.8-27b"


def is_admin(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


def is_banned(data, user_id):
    return str(user_id) in [str(x) for x in data.get("banned_users", [])]


def auto_ban_hours_left(data, uid):
    ab = data.get("auto_ban", {}).get(str(uid), {})
    until = ab.get("banned_until")
    if until and time.time() < until:
        return int((until - time.time()) / 3600) + 1
    return 0


def count_used(data, app_name):
    used = 0
    for uid, uh in data.get("history", {}).items():
        if app_name in uh and uh[app_name]:
            used += 1
    return used


def fmt_dt(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "N/A"


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["Get Comment", "Bulk Comments"], ["My Profile", "History"], ["Withdrawal"]],
    resize_keyboard=True
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Add Comment", "Live Apps"],
        ["Saved Proof", "Stats"],
        ["Broadcast", "Add/Remove Bal"],
        ["Set Force Join", "Ban User"],
        ["Admin Panel", "Bulk Requests"],
    ],
    resize_keyboard=True
)

(
    ADMIN_MENU, ADD_APP, ADD_COMMENTS, BROADCAST_MSG, ADMIN_BAL_ID, ADMIN_BAL_AMT,
    WITHDRAW_METHOD, WITHDRAW_UPI, WITHDRAW_AMT,
    FORCE_JOIN_SET, BAN_USER_ID, APP_ADD_COMMENTS,
    USER_HIST_ID,
    BULK_SELECT_APP, BULK_ENTER_COUNT,
) = range(15)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
SEP = "──────────────────────"


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception:", exc_info=context.error)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=f"⚠️ Bot Error:\n\n{context.error}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════
#   FORCE JOIN
# ═══════════════════════════════════════════════════

async def ensure_joined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    data = load_data()
    channel = data.get("force_join_channel")
    if not channel:
        return True

    user_id = update.effective_user.id
    if is_admin(user_id):
        return True

    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception as e:
        print(f"get_chat_member error: {e}")

    try:
        chat = await context.bot.get_chat(channel)
        invite = chat.invite_link or f"https://t.me/{channel.lstrip('@')}"
    except Exception:
        invite = f"https://t.me/{channel.lstrip('@')}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=invite)],
        [InlineKeyboardButton("✅ I Joined", callback_data="check_join")]
    ])
    text = (
        "⚠️ You must join our channel first to use this bot.\n\n"
        f"👉 Join: {channel}\n\n"
        "After joining, tap 'I Joined'.\n\n"
        "⚠️ If you already joined but still see this, ask the admin to make the bot an admin in the channel."
    )
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
    except Exception:
        pass
    return False


async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = load_data()
    channel = data.get("force_join_channel")

    if not channel:
        await query.edit_message_text("✅ Verified. Send /start now.")
        return

    if is_admin(update.effective_user.id):
        await query.edit_message_text("✅ Admin verified. Send /start.")
        return

    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=update.effective_user.id)
        if member.status in ("member", "administrator", "creator"):
            await query.edit_message_text(
                "✅ Verified! You can now use the bot.\n\n"
                "Choose an option below or send /start."
            )
            return
    except Exception as e:
        print(f"verify error: {e}")

    await query.answer("❌ You haven't joined the channel yet. Join and try again.", show_alert=True)


# ═══════════════════════════════════════════════════
#   START
# ═══════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    context.user_data["proof_app"] = None
    context.user_data["awaiting_bulk_proof"] = False
    context.user_data["bulk_app"] = None
    context.user_data["bulk_comments"] = []
    context.user_data["bulk_screenshots"] = []

    data = load_data()
    if is_banned(data, update.effective_user.id):
        await update.message.reply_text("🚫 You have been banned.\nContact: @Aarvii_005")
        return

    if not await ensure_joined(update, context):
        return

    name = update.effective_user.first_name or "friend"
    text = (
        f"👋 Welcome {name}!\n"
        f"{SEP}\n\n"
        "Use this bot to get review comments and submit proofs.\n\n"
        "Choose an option from the menu below."
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


# ═══════════════════════════════════════════════════
#   ADMIN PANEL  (matches image layout)
# ═══════════════════════════════════════════════════

def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Task History", callback_data="apanel_taskhist"),
         InlineKeyboardButton("👤 User Task Hist", callback_data="apanel_userhist")],
        [InlineKeyboardButton("📋 All Tasks History", callback_data="apanel_allhist")],
        [InlineKeyboardButton("📅 Today's Report", callback_data="apanel_today"),
         InlineKeyboardButton("📋 My History", callback_data="apanel_myhist")],
        [InlineKeyboardButton("📸 All SS of Task", callback_data="apanel_allss")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="apanel_mainmenu")],
    ])


async def admin_panel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return
    await update.message.reply_text(
        f"🛠️ Admin Panel\n{SEP}\n\nChoose an option:",
        reply_markup=admin_panel_keyboard()
    )


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return

    action = query.data.replace("apanel_", "")
    data = load_data()

    if action == "mainmenu":
        await query.edit_message_text(
            f"🛠️ Admin Panel\n{SEP}\n\nUse the reply keyboard for full admin controls."
        )
        return

    if action == "taskhist":
        proofs = data.get("saved_proofs", [])
        if not proofs:
            await query.edit_message_text("📭 No task history yet.")
            return
        text = f"📊 Task History (Last 15)\n{SEP}\n\n"
        for i, p in enumerate(proofs[-15:], 1):
            text += (
                f"{i}. 📱 {p.get('app_name', 'N/A')}\n"
                f"   👤 @{p.get('username', 'N/A')} (ID: {p.get('user_id', 'N/A')})\n"
                f"   🔍 Reviewer: {p.get('reviewer_name', 'N/A')}\n"
                f"   🕒 {fmt_dt(p.get('timestamp', 0))}\n\n"
            )
        await query.edit_message_text(text)
        return

    if action == "allhist":
        proofs = data.get("saved_proofs", [])
        rejections = data.get("rejections", [])
        combined = []
        for p in proofs:
            combined.append({
                "type": "✅",
                "app": p.get("app_name", "N/A"),
                "uid": p.get("user_id", "N/A"),
                "user": p.get("username", "N/A"),
                "detail": p.get("reviewer_name", "N/A"),
                "ts": p.get("timestamp", 0)
            })
        for r in rejections:
            combined.append({
                "type": "❌",
                "app": r.get("app_name", "N/A"),
                "uid": r.get("user_id", "N/A"),
                "user": r.get("username", "N/A"),
                "detail": r.get("reason", "rejected"),
                "ts": r.get("timestamp", 0)
            })
        combined.sort(key=lambda x: x["ts"], reverse=True)
        if not combined:
            await query.edit_message_text("📭 No tasks history yet.")
            return
        text = f"📋 All Tasks History (Last 20)\n{SEP}\n\n"
        for i, c in enumerate(combined[:20], 1):
            text += (
                f"{i}. {c['type']} 📱 {c['app']}\n"
                f"   👤 @{c['user']} (ID: {c['uid']})\n"
                f"   📝 {c['detail']}\n"
                f"   🕒 {fmt_dt(c['ts'])}\n\n"
            )
        await query.edit_message_text(text)
        return

    if action == "today":
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        proofs = [p for p in data.get("saved_proofs", []) if p.get("timestamp", 0) >= today_start]
        rejections = [r for r in data.get("rejections", []) if r.get("timestamp", 0) >= today_start]
        active_users = set()
        for p in proofs:
            active_users.add(p.get("user_id"))
        withdrawals_today = [w for w in data.get("withdrawals", []) if w.get("approved_at", 0) >= today_start or w.get("requested_at", 0) >= today_start]

        text = (
            f"📅 Today's Report\n{SEP}\n\n"
            f"✅ Approved Proofs: {len(proofs)}\n"
            f"❌ Rejected Proofs: {len(rejections)}\n"
            f"👥 Active Users: {len(active_users)}\n"
            f"💸 Withdrawals Today: {len(withdrawals_today)}\n\n"
            f"🕒 Date: {datetime.now().strftime('%Y-%m-%d')}"
        )
        await query.edit_message_text(text)
        return

    if action == "myhist":
        uid = str(update.effective_user.id)
        proofs = [p for p in data.get("saved_proofs", []) if p.get("user_id") == uid]
        if not proofs:
            await query.edit_message_text("📭 You haven't done any tasks yet.")
            return
        text = f"📋 My Task History (Last 15)\n{SEP}\n\n"
        for i, p in enumerate(proofs[-15:], 1):
            text += (
                f"{i}. 📱 {p.get('app_name', 'N/A')}\n"
                f"   🔍 {p.get('reviewer_name', 'N/A')}\n"
                f"   🕒 {fmt_dt(p.get('timestamp', 0))}\n\n"
            )
        await query.edit_message_text(text)
        return

    if action == "allss":
        shots = data.get("used_screenshots", [])
        if not shots:
            await query.edit_message_text("📭 No screenshots submitted yet.")
            return
        recent = shots[-10:]
        await query.edit_message_text(
            f"📸 Sending last {len(recent)} screenshot(s)...\n{SEP}"
        )
        for shot in recent:
            fid = shot.get("file_id")
            if not fid:
                continue
            cap = (
                f"📱 {shot.get('app_name', 'N/A')}\n"
                f"👤 ID: {shot.get('user_id', 'N/A')}\n"
                f"🕒 {fmt_dt(shot.get('timestamp', 0))}"
            )
            try:
                await context.bot.send_photo(chat_id=update.effective_user.id, photo=fid, caption=cap)
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"send_photo error: {e}")
        return

    if action == "userhist":
        await query.edit_message_text("👤 Send the User ID you want to view history for:")
        return USER_HIST_ID


async def admin_user_hist_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    uid = update.message.text.strip()
    data = load_data()
    proofs = [p for p in data.get("saved_proofs", []) if str(p.get("user_id")) == uid]
    rejections = [r for r in data.get("rejections", []) if str(r.get("user_id")) == uid]
    user_info = data.get("users", {}).get(uid, {})

    text = (
        f"👤 User Task History\n{SEP}\n\n"
        f"ID: {uid}\n"
        f"Balance: ₹{user_info.get('balance', 0.0):.2f}\n"
        f"Total Tasks: {user_info.get('total_tasks', 0)}\n"
        f"✅ Accepted: {user_info.get('accepted', 0)}\n"
        f"❌ Rejected: {user_info.get('rejected', 0)}\n\n"
        f"{SEP}\n"
    )
    if not proofs and not rejections:
        text += "📭 No task history found."
    else:
        combined = []
        for p in proofs:
            combined.append(("✅", p.get("app_name", "N/A"), p.get("reviewer_name", "N/A"), p.get("timestamp", 0)))
        for r in rejections:
            combined.append(("❌", r.get("app_name", "N/A"), r.get("reason", "rejected"), r.get("timestamp", 0)))
        combined.sort(key=lambda x: x[3], reverse=True)
        for icon, app, detail, ts in combined[:15]:
            text += f"{icon} 📱 {app}\n   {detail}\n   🕒 {fmt_dt(ts)}\n\n"

    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════
#   ADMIN BASICS
# ═══════════════════════════════════════════════════

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Not authorized.")
        return ConversationHandler.END
    await update.message.reply_text(
        f"🛠️ Admin Panel\n{SEP}\n\nChoose an action:",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU


async def add_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Send the App Name:")
    return ADD_APP


async def add_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_app_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ App: {context.user_data['temp_app_name']}\n\n"
        "Now send comments separated by commas.\n"
        "Example: `Nice app,Good UI,Love it`",
        parse_mode="Markdown"
    )
    return ADD_COMMENTS


async def add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comments = [c.strip() for c in update.message.text.split(",") if c.strip()]
    app_name = context.user_data.get("temp_app_name")
    data = load_data()
    if app_name not in data["apps"]:
        data["apps"][app_name] = []
    data["apps"][app_name].extend(comments)
    save_data(data)
    await update.message.reply_text(
        f"✅ Comments saved.\n{SEP}\n📱 App: {app_name}\n💬 Added: {len(comments)}",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU


async def live_apps_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = data.get("apps", {})
    if not apps:
        await update.message.reply_text("📭 No apps yet.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = f"📱 Live Apps\n{SEP}\n\nTap an app:"
    keyboard = []
    for app_name, comments in apps.items():
        total = len(comments)
        used = count_used(data, app_name)
        left = total - used
        keyboard.append([InlineKeyboardButton(f"📱 {app_name} ({left} left / {used} used)", callback_data=f"manageapp_{app_name}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MENU


async def manage_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    app_name = query.data.replace("manageapp_", "")
    data = load_data()
    if app_name not in data["apps"]:
        await query.edit_message_text("❌ App does not exist.")
        return
    total = len(data["apps"][app_name])
    used = count_used(data, app_name)
    left = total - used
    text = (
        f"📱 {app_name}\n"
        f"{SEP}\n"
        f"Total: {total}\n"
        f"Used: {used}\n"
        f"Remaining: {left}\n\n"
        f"Choose an action:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Comments", callback_data=f"addcmt_{app_name}")],
        [InlineKeyboardButton("➖ Remove a Comment", callback_data=f"rmcmt_{app_name}")],
        [InlineKeyboardButton("🗑️ Delete App", callback_data=f"delapp_{app_name}")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard)


async def add_cmt_to_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    app_name = query.data.replace("addcmt_", "")
    context.user_data["app_to_add"] = app_name
    await query.edit_message_text(f"*{app_name}* — Send new comments separated by commas:", parse_mode="Markdown")
    return APP_ADD_COMMENTS


async def app_add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comments = [c.strip() for c in update.message.text.split(",") if c.strip()]
    app_name = context.user_data.get("app_to_add")
    data = load_data()
    if app_name not in data["apps"]:
        data["apps"][app_name] = []
    data["apps"][app_name].extend(comments)
    save_data(data)
    await update.message.reply_text(
        f"✅ Added {len(comments)} comments to {app_name}.",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU


async def remove_cmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    app_name = query.data.replace("rmcmt_", "")
    data = load_data()
    comments = data["apps"].get(app_name, [])
    if not comments:
        await query.edit_message_text("No comments.")
        return
    keyboard = []
    for i, c in enumerate(comments[:20]):
        short = c[:40] + ("..." if len(c) > 40 else "")
        keyboard.append([InlineKeyboardButton(f"❌ {short}", callback_data=f"delcmt_{app_name}_{i}")])
    keyboard.append([InlineKeyboardButton("↩️ Back", callback_data=f"manageapp_{app_name}")])
    await query.edit_message_text(
        f"*{app_name}* — Tap a comment to delete:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def delete_cmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    parts = query.data.split("_", 2)
    app_name = parts[1]
    index = int(parts[2])
    data = load_data()
    if app_name in data["apps"] and 0 <= index < len(data["apps"][app_name]):
        removed = data["apps"][app_name].pop(index)
        save_data(data)
        await query.edit_message_text(f"✅ Removed: {removed[:60]}")
    else:
        await query.edit_message_text("❌ Comment not found.")


async def delete_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update.effective_user.id):
        return
    app_name = query.data.replace("delapp_", "")
    data = load_data()
    if app_name in data["apps"]:
        del data["apps"][app_name]
        save_data(data)
        await query.edit_message_text(f"🗑️ Deleted: {app_name}")
    else:
        await query.edit_message_text("❌ App not found.")


async def show_saved_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    proofs = data.get("saved_proofs", [])
    if not proofs:
        await update.message.reply_text("📭 No saved proofs yet.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = f"📋 Saved Proofs — Last 10\n{SEP}\n\n"
    for i, p in enumerate(proofs[-10:], 1):
        text += (
            f"{i}. 📱 {p.get('app_name', 'N/A')}\n"
            f"   👤 Reviewer: {p.get('reviewer_name', 'N/A')}\n"
            f"   🧑 User: @{p.get('username', 'N/A')}\n"
            f"   🆔 ID: {p.get('user_id', 'N/A')}\n"
            f"   🕒 {fmt_dt(p.get('timestamp', 0))}\n\n"
        )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    users = data.get("users", {})
    proofs = data.get("saved_proofs", [])
    rejections = data.get("rejections", [])
    total_balance = sum(u.get("balance", 0) for u in users.values())
    total_withdrawn = sum(w.get("amount", 0) for w in data.get("withdrawals", []) if w.get("status") == "approved")
    pending_count = sum(len(v) for v in data.get("pending_proofs", {}).values())
    pending_bulk = len([b for b in data.get("bulk_requests", []) if b.get("status") == "pending"])

    text = (
        f"📊 Bot Statistics\n"
        f"{SEP}\n\n"
        f"👥 Total Users: {len(users)}\n"
        f"📝 Approved Proofs: {len(proofs)}\n"
        f"❌ Rejected Proofs: {len(rejections)}\n"
        f"⏳ Active Tasks: {pending_count}\n"
        f"📥 Pending Bulk Requests: {pending_bulk}\n\n"
        f"{SEP}\n"
        f"💰 Active Balance: ₹{total_balance:.2f}\n"
        f"💸 Total Withdrawn: ₹{total_withdrawn:.2f}"
    )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU


async def broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 Send the broadcast message:")
    return BROADCAST_MSG


async def broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    data = load_data()
    users = list(data.get("users", {}).keys())
    await update.message.reply_text(f"⏳ Sending to {len(users)} users...")
    count = 0
    final_msg = f"📢 Announcement\n{SEP}\n\n{msg}"
    for uid in users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=final_msg)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Delivered to {count} users.", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU


async def admin_bal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖️ Send the User ID:")
    return ADMIN_BAL_ID


async def admin_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bal_user_id"] = update.message.text.strip()
    await update.message.reply_text("💵 Send the amount (e.g., 50 to add, -50 to remove):")
    return ADMIN_BAL_AMT


async def admin_bal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    uid = context.user_data.get("bal_user_id")
    data = load_data()
    if uid not in data["users"]:
        data["users"][uid] = {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0}
    data["users"][uid]["balance"] += amount
    save_data(data)
    action = "added" if amount > 0 else "removed"
    await update.message.reply_text(
        f"✅ Balance updated.\n{SEP}\n👤 {uid}\n💵 ₹{abs(amount):.2f} {action}\n💰 New: ₹{data['users'][uid]['balance']:.2f}",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU


async def force_join_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    current = data.get("force_join_channel") or "Not set"
    await update.message.reply_text(
        f"📢 Current Force Join: {current}\n\n"
        "Send the channel username (e.g., @mychannel) or `off` to disable.\n\n"
        "⚠️ Make the bot an admin in that channel, or verification will fail.",
        parse_mode="Markdown"
    )
    return FORCE_JOIN_SET


async def force_join_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    data = load_data()
    if val.lower() == "off":
        data["force_join_channel"] = None
        save_data(data)
        await update.message.reply_text("✅ Force join disabled.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    if not val.startswith("@"):
        val = "@" + val
    data["force_join_channel"] = val
    save_data(data)

    warning = ""
    try:
        chat = await context.bot.get_chat(val)
        me = await context.bot.get_chat_member(chat_id=val, user_id=context.bot.id)
        if me.status not in ("administrator", "creator"):
            warning = "\n\n⚠️ Bot is NOT an admin in this channel! Force join will not work properly."
    except Exception as e:
        warning = f"\n\n⚠️ Could not verify channel: {e}\nPlease make the bot admin."

    await update.message.reply_text(
        f"✅ Force join set: {val}{warning}",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU


async def ban_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Send the User ID to ban:")
    return BAN_USER_ID


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    data = load_data()
    banned = data.get("banned_users", [])
    if uid not in [str(x) for x in banned]:
        banned.append(uid)
        data["banned_users"] = banned
        save_data(data)
        await update.message.reply_text(f"✅ User {uid} banned.", reply_markup=ADMIN_KEYBOARD)
    else:
        await update.message.reply_text(f"⚠️ User {uid} already banned.", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU


# ═══════════════════════════════════════════════════
#   BULK REQUESTS (Admin view)
# ═══════════════════════════════════════════════════

async def bulk_requests_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    data = load_data()
    reqs = [r for r in data.get("bulk_requests", []) if r.get("status") == "pending"]
    if not reqs:
        await update.message.reply_text("📭 No pending bulk requests.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    for r in reqs:
        text = (
            f"📥 Bulk Request\n{SEP}\n"
            f"👤 User: @{r.get('username', 'N/A')}\n"
            f"🆔 ID: {r.get('user_id')}\n"
            f"📱 App: {r.get('app_name')}\n"
            f"💬 Requested: {r.get('count')} comments\n"
            f"🕒 {fmt_dt(r.get('requested_at', 0))}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"bulk_approve_{r.get('user_id')}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"bulk_reject_{r.get('user_id')}")
        ]])
        try:
            await context.bot.send_message(chat_id=update.effective_user.id, text=text, reply_markup=kb)
        except Exception:
            pass
    return ADMIN_MENU


async def bulk_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    parts = query.data.split("_", 2)
    action = parts[1]
    uid = parts[2]

    data = load_data()
    reqs = data.get("bulk_requests", [])
    target = None
    for r in reqs:
        if str(r.get("user_id")) == uid and r.get("status") == "pending":
            target = r
            break
    if not target:
        await query.edit_message_text(f"⚠️ No pending bulk request for {uid}.")
        return

    app_name = target["app_name"]
    count = target["count"]

    if action == "reject":
        target["status"] = "rejected"
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=(
                    f"❌ Bulk request rejected.\n{SEP}\n"
                    f"📱 App: {app_name}\n"
                    f"💬 Requested: {count}\n\n"
                    f"Contact: @Aarvii_005"
                ),
                reply_markup=MAIN_KEYBOARD
            )
        except Exception:
            pass
        await query.edit_message_text(f"❌ Rejected bulk request for {uid}.")
        return

    # APPROVE
    if app_name not in data["apps"]:
        await query.edit_message_text("❌ App no longer exists.")
        target["status"] = "rejected"
        save_data(data)
        return

    # Remove already-used comments from this app
    used_comments = set()
    for u, uh in data.get("history", {}).items():
        if app_name in uh and uh[app_name]:
            for c in uh[app_name]:
                used_comments.add(c)

    available = [c for c in data["apps"][app_name] if c not in used_comments]
    if len(available) < count:
        target["status"] = "rejected"
        save_data(data)
        await query.edit_message_text(
            f"❌ Only {len(available)} comments available, need {count}. Request auto-rejected."
        )
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"❌ Bulk request failed. Only {len(available)} comments available for {app_name}.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception:
            pass
        return

    # Pick least-used comments first
    usage = data.setdefault("comment_usage", {}).setdefault(app_name, {})
    available.sort(key=lambda c: usage.get(c, 0))
    chosen = available[:count]

    for c in chosen:
        usage[c] = usage.get(c, 0) + 1

    # Save assigned to user's history so they don't get same comment again
    if uid not in data["history"]:
        data["history"][uid] = {}
    data["history"][uid][app_name] = chosen

    # Create pending_proofs entry for bulk
    if "pending_proofs" not in data:
        data["pending_proofs"] = {}
    if uid not in data["pending_proofs"]:
        data["pending_proofs"][uid] = {}
    data["pending_proofs"][uid][app_name] = {
        "comment": chosen[0],
        "comments": chosen,
        "bulk": True,
        "remaining": list(chosen),
        "verified": [],
        "timestamp": time.time(),
        "count": count
    }

    target["status"] = "approved"
    target["approved_at"] = time.time()
    save_data(data)

    # Send comments to user
    comments_text = "\n".join([f"{i}. `{c}`" for i, c in enumerate(chosen, 1)])
    try:
        await context.bot.send_message(
            chat_id=int(uid),
            text=(
                f"✅ Bulk Request Approved!\n{SEP}\n\n"
                f"📱 App: {app_name}\n"
                f"💬 Comments ({count}):\n{comments_text}\n\n"
                f"📸 Now submit {count} screenshots (one by one).\n"
                f"⏰ You have 1 hour.\n"
                f"⚠️ After 1 hour, unsubmitted comments will be released."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"bulk approve send error: {e}")

    # Set user context
    # Note: user may not be in this conversation; we rely on flags set when they submit
    # We'll set flags by checking pending on photo handler
    await query.edit_message_text(f"✅ Approved {count} comments for {uid} on {app_name}.")

    # Schedule timeout
    jq = context.job_queue
    if jq:
        jq.run_once(
            bulk_timeout_job,
            when=3600,
            data={"user_id": uid, "app_name": app_name},
            name=f"bulk_timeout_{uid}_{app_name}"
        )


async def bulk_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    uid = job.data["user_id"]
    app_name = job.data["app_name"]
    data = load_data()

    pending = data.get("pending_proofs", {})
    if uid not in pending or app_name not in pending[uid]:
        return
    entry = pending[uid][app_name]
    if not entry.get("bulk"):
        return

    # Release unsubmitted comments back
    remaining = entry.get("remaining", [])
    for c in remaining:
        # Return to history removal
        if uid in data.get("history", {}) and app_name in data["history"][uid]:
            if c in data["history"][uid][app_name]:
                data["history"][uid][app_name].remove(c)

    del pending[uid][app_name]
    if not pending[uid]:
        del pending[uid]

    # Mark request status
    for r in data.get("bulk_requests", []):
        if str(r.get("user_id")) == uid and r.get("app_name") == app_name and r.get("status") == "approved":
            r["status"] = "timed_out"
            break

    save_data(data)

    try:
        await context.bot.send_message(
            chat_id=int(uid),
            text=(
                f"⏰ Bulk task timed out!\n{SEP}\n\n"
                f"📱 App: {app_name}\n"
                f"❌ Unsubmitted comments: {len(remaining)}\n\n"
                f"These comments have been released. Contact: @Aarvii_005"
            ),
            reply_markup=MAIN_KEYBOARD
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════
#   PROFILE / HISTORY
# ═══════════════════════════════════════════════════

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, context): return
    uid = str(update.effective_user.id)
    name = update.effective_user.first_name or "User"
    data = load_data()
    u = data["users"].get(uid, {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0})
    text = (
        f"👤 My Profile\n"
        f"{SEP}\n\n"
        f"Name: {name}\n"
        f"ID: {uid}\n"
        f"Balance: ₹{u.get('balance', 0.0):.2f}\n\n"
        f"{SEP}\n"
        f"📊 Task Summary\n"
        f"Total Tasks: {u.get('total_tasks', 0)}\n"
        f"✅ Accepted: {u.get('accepted', 0)}\n"
        f"❌ Rejected: {u.get('rejected', 0)}"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, context): return
    uid = str(update.effective_user.id)
    data = load_data()
    proofs = [p for p in data.get("saved_proofs", []) if str(p.get("user_id")) == uid]
    rejections = [r for r in data.get("rejections", []) if str(r.get("user_id")) == uid]
    if not proofs and not rejections:
        await update.message.reply_text("📭 You haven't done any tasks yet.", reply_markup=MAIN_KEYBOARD)
        return
    text = f"📜 Task History\n{SEP}\n\n"
    combined = []
    for p in proofs:
        combined.append((p.get("timestamp", 0), "✅", p.get("app_name", "N/A"), p.get("reviewer_name", "N/A")))
    for r in rejections:
        combined.append((r.get("timestamp", 0), "❌", r.get("app_name", "N/A"), r.get("reason", "Rejected")))
    combined.sort(key=lambda x: x[0], reverse=True)
    for ts, icon, app, detail in combined[:15]:
        text += f"{icon} 📱 {app}\n   {detail}\n   🕒 {fmt_dt(ts)}\n\n"
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


# ═══════════════════════════════════════════════════
#   WITHDRAWAL
# ═══════════════════════════════════════════════════

async def withdrawal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, context): return ConversationHandler.END
    uid = str(update.effective_user.id)
    data = load_data()
    u = data["users"].get(uid, {"balance": 0.0})
    bal = u.get("balance", 0.0)
    if bal < 10:
        await update.message.reply_text(
            f"💰 Withdrawal\n{SEP}\n\n❌ Minimum is ₹10.\n💵 Your balance: ₹{bal:.2f}",
            reply_markup=MAIN_KEYBOARD
        )
        return ConversationHandler.END
    text = f"💰 Withdrawal\n{SEP}\n\nAvailable: ₹{bal:.2f}\n\nChoose a method:"
    keyboard = [[
        InlineKeyboardButton("💳 UPI", callback_data="withdraw_upi"),
        InlineKeyboardButton("🎗️ VSV", callback_data="withdraw_vsv")
    ]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return WITHDRAW_METHOD


async def withdraw_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "withdraw_upi":
        await query.edit_message_text("💳 Send your UPI ID:")
        return WITHDRAW_UPI
    else:
        await query.edit_message_text(
            f"🎗️ VSV Withdrawal\n{SEP}\n\nMessage the owner directly:\n👉 @Aarvii_005"
        )
        return ConversationHandler.END


async def withdraw_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["withdraw_upi"] = update.message.text.strip()
    await update.message.reply_text("💵 Send the amount (Min ₹10):")
    return WITHDRAW_AMT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    if amount < 10:
        await update.message.reply_text("❌ Minimum is ₹10.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    uid = str(update.effective_user.id)
    data = load_data()
    if amount > data["users"].get(uid, {}).get("balance", 0.0):
        await update.message.reply_text("❌ Insufficient balance.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    data["users"][uid]["balance"] -= amount
    upi = context.user_data.get("withdraw_upi")
    data["withdrawals"].append({
        "user_id": uid,
        "username": update.effective_user.username or "N/A",
        "amount": amount, "method": "UPI", "upi_id": upi,
        "status": "pending",
        "requested_at": time.time()
    })
    save_data(data)

    admin_text = (
        f"💰 New Withdrawal Request\n"
        f"{SEP}\n\n"
        f"👤 @{update.effective_user.username or 'N/A'}\n"
        f"🆔 {uid}\n"
        f"💵 ₹{amount:.2f}\n"
        f"🏦 UPI\n"
        f"📧 {upi}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"wdstatus_approve_{uid}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"wdstatus_reject_{uid}")
    ]])

    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=admin_text, reply_markup=keyboard)
        except Exception as e:
            print(f"Send admin {aid} error: {e}")

    await update.message.reply_text(
        f"✅ Withdrawal request submitted.\n{SEP}\nPlease wait for approval.",
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END


async def withdrawal_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("Only admin.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split("_")
    action, uid = parts[1], parts[2]
    data = load_data()
    target = None
    for w in reversed(data["withdrawals"]):
        if w["user_id"] == uid and w["status"] == "pending":
            target = w
            break
    if not target:
        await query.edit_message_text(f"⚠️ No pending withdrawal for {uid}.")
        return

    if action == "approve":
        target["status"] = "approved"
        target["approved_at"] = time.time()
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"✅ Withdrawal Approved\n{SEP}\n\n₹{target['amount']:.2f} has been processed.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception: pass
        await query.edit_message_text(f"✅ ₹{target['amount']:.2f} approved for {uid}.")
    else:
        target["status"] = "rejected"
        if uid in data["users"]:
            data["users"][uid]["balance"] += target["amount"]
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"❌ Withdrawal Rejected\n{SEP}\n\n₹{target['amount']:.2f} refunded to your balance.\nContact @Aarvii_005.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception: pass
        await query.edit_message_text(f"❌ Rejected. ₹{target['amount']:.2f} refunded to {uid}.")


# ═══════════════════════════════════════════════════
#   GET COMMENT (single)
# ═══════════════════════════════════════════════════

async def get_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, context): return ConversationHandler.END
    data = load_data()
    uid = str(update.effective_user.id)

    if is_banned(data, uid):
        await update.message.reply_text("🚫 You have been banned.")
        return ConversationHandler.END

    hrs = auto_ban_hours_left(data, uid)
    if hrs > 0:
        await update.message.reply_text(
            f"🚫 You are banned for {hrs} more hour(s).\n\n"
            f"Reason: Repeated fake proofs.\n"
            f"Contact: @Aarvii_005"
        )
        return ConversationHandler.END

    apps = list(data.get("apps", {}).keys())
    if not apps:
        await update.message.reply_text("📭 No tasks available right now.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"📱 {app}", callback_data=f"getapp_{app}")] for app in apps]
    await update.message.reply_text(
        f"📋 Available Apps\n{SEP}\n\nSelect an app:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


async def user_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data_check = load_data()
    channel = data_check.get("force_join_channel")
    if channel and not is_admin(update.effective_user.id):
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=update.effective_user.id)
            if member.status not in ("member", "administrator", "creator"):
                await query.edit_message_text(
                    f"⚠️ You have not joined the channel.\n\n"
                    f"👉 Join: {channel}\n\n"
                    f"Then send /start."
                )
                return
        except Exception as e:
            print(f"force join recheck error: {e}")

    app_name = query.data.replace("getapp_", "")
    uid = str(update.effective_user.id)
    data = load_data()

    last = data.setdefault("last_request", {})
    now = time.time()
    if uid in last and now - last[uid] < 120:
        remaining = int(120 - (now - last[uid]))
        await query.edit_message_text(
            f"⏳ Please wait!\n\nTry again in {remaining} seconds."
        )
        return

    hrs = auto_ban_hours_left(data, uid)
    if hrs > 0:
        await query.edit_message_text(
            f"🚫 You are banned for {hrs} more hour(s).\n\nContact: @Aarvii_005"
        )
        return

    if app_name not in data["apps"] or not data["apps"][app_name]:
        await query.edit_message_text("📭 No comment available for this app.")
        return

    if uid not in data["history"]:
        data["history"][uid] = {}

    if app_name in data["history"][uid] and data["history"][uid][app_name]:
        has_pending = uid in data.get("pending_proofs", {}) and app_name in data["pending_proofs"].get(uid, {})
        has_submitted = uid in data.get("proofs", {}) and app_name in data["proofs"].get(uid, {})

        if not has_pending and not has_submitted:
            del data["history"][uid][app_name]
            if "attempts" in data and uid in data["attempts"]:
                data["attempts"][uid].pop(app_name, None)
            save_data(data)
        else:
            await query.edit_message_text(
                "⚠️ You already received a comment for this app.\n\n"
                "Need another? Contact: @Aarvii_005"
            )
            return

    available = [c for c in data["apps"][app_name] if c not in data["history"][uid].get(app_name, [])]
    if not available:
        await query.edit_message_text("📭 No unique comment available.")
        return

    usage = data.setdefault("comment_usage", {}).setdefault(app_name, {})
    min_usage = min(usage.get(c, 0) for c in available)
    lowest_used = [c for c in available if usage.get(c, 0) == min_usage]
    chosen = random.choice(lowest_used)
    usage[chosen] = usage.get(chosen, 0) + 1

    data["history"][uid][app_name] = [chosen]

    if "attempts" not in data: data["attempts"] = {}
    if uid not in data["attempts"]: data["attempts"][uid] = {}
    data["attempts"][uid][app_name] = 3

    if "pending_proofs" not in data: data["pending_proofs"] = {}
    if uid not in data["pending_proofs"]: data["pending_proofs"][uid] = {}
    data["pending_proofs"][uid][app_name] = {
        "comment": chosen,
        "comments": [chosen],
        "bulk": False,
        "remaining": [chosen],
        "verified": [],
        "timestamp": now,
        "count": 1
    }

    last[uid] = now
    save_data(data)

    await query.edit_message_text(
        f"💬 Your Comment\n{SEP}\n\n`{chosen}`\n\nTap to copy.",
        parse_mode="Markdown"
    )
    await context.bot.send_message(
        chat_id=uid,
        text=(
            f"📸 Send your screenshot here.\n\n"
            f"⏰ You have 1 hour.\n"
            f"After 1 hour, the proof will not be accepted.\n"
            f"Contact: @Aarvii_005"
        )
    )
    context.user_data["awaiting_proof"] = True
    context.user_data["proof_app"] = app_name
    context.user_data["awaiting_bulk_proof"] = False

    jq = context.job_queue
    if jq:
        jq.run_once(proof_timeout_job, when=3600,
                    data={"user_id": uid, "app_name": app_name},
                    name=f"timeout_{uid}_{app_name}")
        jq.run_once(proof_reminder_job, when=1800,
                    data={"user_id": uid, "app_name": app_name},
                    name=f"reminder_{uid}_{app_name}")


async def proof_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    uid = job.data["user_id"]
    app_name = job.data["app_name"]
    data = load_data()
    pending = data.get("pending_proofs", {})
    if uid not in pending or app_name not in pending[uid]:
        return
    entry = pending[uid][app_name]
    if entry.get("bulk"):
        return  # handled by bulk timeout
    comment = entry["comment"]
    del pending[uid][app_name]
    if not pending[uid]:
        del pending[uid]
    if app_name in data["apps"] and comment not in data["apps"][app_name]:
        data["apps"][app_name].append(comment)
    if uid in data["history"] and app_name in data["history"][uid]:
        del data["history"][uid][app_name]
    if "attempts" in data and uid in data["attempts"] and app_name in data["attempts"][uid]:
        del data["attempts"][uid][app_name]
    save_data(data)
    try:
        await context.bot.send_message(
            chat_id=int(uid),
            text=(
                f"⏰ Time's up!\n{SEP}\n\n"
                f"You didn't submit the proof for {app_name} within 1 hour.\n\n"
                f"For a new comment, contact: @Aarvii_005"
            ),
            reply_markup=MAIN_KEYBOARD
        )
    except Exception: pass


async def proof_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    uid = job.data["user_id"]
    app_name = job.data["app_name"]
    data = load_data()
    pending = data.get("pending_proofs", {})
    if uid not in pending or app_name not in pending[uid]:
        return
    try:
        await context.bot.send_message(
            chat_id=int(uid),
            text=f"⏰ Only 30 minutes left to submit the proof for {app_name}!"
        )
    except Exception: pass


# ═══════════════════════════════════════════════════
#   BULK COMMENTS (user flow)
# ═══════════════════════════════════════════════════

async def bulk_comments_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, context): return ConversationHandler.END
    data = load_data()
    uid = str(update.effective_user.id)

    if is_banned(data, uid):
        await update.message.reply_text("🚫 You have been banned.")
        return ConversationHandler.END

    hrs = auto_ban_hours_left(data, uid)
    if hrs > 0:
        await update.message.reply_text(
            f"🚫 You are banned for {hrs} more hour(s).\n\nContact: @Aarvii_005"
        )
        return ConversationHandler.END

    # Check existing pending request
    for r in data.get("bulk_requests", []):
        if str(r.get("user_id")) == uid and r.get("status") == "pending":
            await update.message.reply_text(
                "⏳ You already have a pending bulk request. Please wait for admin approval.",
                reply_markup=MAIN_KEYBOARD
            )
            return ConversationHandler.END

    apps = list(data.get("apps", {}).keys())
    if not apps:
        await update.message.reply_text("📭 No apps available.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(f"📱 {app}", callback_data=f"bulkapp_{app}")] for app in apps]
    await update.message.reply_text(
        f"📦 Bulk Comments\n{SEP}\n\nSelect an app:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return BULK_SELECT_APP


async def bulk_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_name = query.data.replace("bulkapp_", "")
    context.user_data["bulk_request_app"] = app_name

    data = load_data()
    if app_name not in data["apps"]:
        await query.edit_message_text("❌ App not found.")
        return ConversationHandler.END

    # Count available
    used = set()
    for u, uh in data.get("history", {}).items():
        if app_name in uh and uh[app_name]:
            for c in uh[app_name]:
                used.add(c)
    available = len([c for c in data["apps"][app_name] if c not in used])

    await query.edit_message_text(
        f"📱 App: {app_name}\n"
        f"💬 Available comments: {available}\n\n"
        f"How many comments do you want? (2–10)\n\n"
        f"⚠️ Admin approval is required."
    )
    return BULK_ENTER_COUNT


async def bulk_count_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid number. Send a number between 2 and 10.")
        return BULK_ENTER_COUNT

    if count < 2 or count > 10:
        await update.message.reply_text("❌ Number must be between 2 and 10.")
        return BULK_ENTER_COUNT

    app_name = context.user_data.get("bulk_request_app")
    uid = str(update.effective_user.id)
    data = load_data()

    used = set()
    for u, uh in data.get("history", {}).items():
        if app_name in uh and uh[app_name]:
            for c in uh[app_name]:
                used.add(c)
    available = len([c for c in data["apps"][app_name] if c not in used])

    if count > available:
        await update.message.reply_text(
            f"❌ Only {available} comments available for {app_name}. Try a smaller number.",
            reply_markup=MAIN_KEYBOARD
        )
        return ConversationHandler.END

    # Create request
    req = {
        "user_id": uid,
        "username": update.effective_user.username or "N/A",
        "app_name": app_name,
        "count": count,
        "status": "pending",
        "requested_at": time.time()
    }
    data.setdefault("bulk_requests", []).append(req)
    save_data(data)

    # Notify all admins
    admin_text = (
        f"📥 New Bulk Request\n{SEP}\n\n"
        f"👤 @{update.effective_user.username or 'N/A'}\n"
        f"🆔 {uid}\n"
        f"📱 App: {app_name}\n"
        f"💬 Count: {count}\n"
        f"🕒 {fmt_dt(time.time())}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"bulk_approve_{uid}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"bulk_reject_{uid}")
    ]])
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=admin_text, reply_markup=kb)
        except Exception as e:
            print(f"bulk notify admin {aid} error: {e}")

    await update.message.reply_text(
        f"✅ Bulk request submitted.\n{SEP}\n\n"
        f"📱 App: {app_name}\n"
        f"💬 Count: {count}\n\n"
        f"⏳ Please wait for admin approval.",
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════
#   PHOTO HANDLER (single + bulk)
# ═══════════════════════════════════════════════════

async def handle_proof_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof") or context.user_data.get("awaiting_bulk_proof"):
        await update.message.reply_text("⚠️ Send only screenshots, not text.")
        return


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Bulk check first
    if context.user_data.get("awaiting_bulk_proof"):
        await handle_bulk_photo(update, context)
        return
    if context.user_data.get("awaiting_proof"):
        await handle_single_photo(update, context)
        return


async def _verify_with_ai(file_path, chosen_comment, app_name):
    """Returns (comment_matched, reviewer_name)."""
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    prompt = (
        f'Analyze this screenshot. '
        f'1. Does it contain this exact comment: "{chosen_comment}"? (comment_match) '
        f'2. Does it show a review for the app "{app_name}"? (app_match) '
        f'3. What is the reviewer name? '
        f'Reply STRICTLY as JSON with no markdown: '
        f'{{"comment_match": true, "app_match": true, "reviewer_name": "name"}}'
    )
    completion = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]}],
        temperature=0.1, max_completion_tokens=256,
    )
    response_text = completion.choices[0].message.content
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    result = json.loads(response_text)
    return result.get("comment_match", False), result.get("reviewer_name", "Unknown")


# ─── SINGLE ─────────────────────────────────────────

async def handle_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data_pre = load_data()
    channel = data_pre.get("force_join_channel")
    if channel and not is_admin(update.effective_user.id):
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=update.effective_user.id)
            if member.status not in ("member", "administrator", "creator"):
                await update.message.reply_text(
                    f"⚠️ Please join the channel first.\n\n👉 Join: {channel}",
                    reply_markup=MAIN_KEYBOARD
                )
                return
        except Exception as e:
            print(f"force join recheck error: {e}")

    photo_file = await update.message.photo[-1].get_file()
    file_unique_id = update.message.photo[-1].file_unique_id
    file_id = update.message.photo[-1].file_id
    app_name = context.user_data.get("proof_app", "Unknown")
    user = update.effective_user
    uid = str(user.id)
    data = load_data()

    used_ids = [x.get("file_unique_id") for x in data.get("used_screenshots", [])]
    if file_unique_id in used_ids:
        await update.message.reply_text("❌ This screenshot was already used.\n\nSend a new one.", reply_markup=MAIN_KEYBOARD)
        return

    pending = data.get("pending_proofs", {})
    if uid not in pending or app_name not in pending[uid]:
        await update.message.reply_text("⏰ Time's up or no active task.\n\nContact: @Aarvii_005", reply_markup=MAIN_KEYBOARD)
        context.user_data["awaiting_proof"] = False
        return

    entry = pending[uid][app_name]
    ts = entry.get("timestamp", 0)
    if time.time() - ts > 3600:
        comment = entry["comment"]
        del pending[uid][app_name]
        if not pending[uid]:
            del pending[uid]
        if app_name in data["apps"] and comment not in data["apps"][app_name]:
            data["apps"][app_name].append(comment)
        if uid in data["history"] and app_name in data["history"][uid]:
            del data["history"][uid][app_name]
        if "attempts" in data and uid in data["attempts"] and app_name in data["attempts"][uid]:
            del data["attempts"][uid][app_name]
        save_data(data)
        await update.message.reply_text("⏰ Time's up.\n\nContact: @Aarvii_005", reply_markup=MAIN_KEYBOARD)
        context.user_data["awaiting_proof"] = False
        return

    if uid in data["proofs"] and app_name in data["proofs"][uid]:
        await update.message.reply_text(
            "❌ You already submitted a proof for this app.\n\nContact: @Aarvii_005",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    await update.message.reply_text("⏳ Checking your proof. Please wait 5–10 seconds...")

    file_path = "temp_proof.jpg"
    await photo_file.download_to_drive(file_path)
    try:
        chosen_comment = data["history"][uid][app_name][0]
        comment_matched, reviewer_name = await asyncio.to_thread(
            _verify_with_ai, file_path, chosen_comment, app_name
        )

        if not comment_matched:
            if "attempts" not in data: data["attempts"] = {}
            if uid not in data["attempts"]: data["attempts"][uid] = {}
            if app_name not in data["attempts"][uid]:
                data["attempts"][uid][app_name] = 3

            data["attempts"][uid][app_name] -= 1
            remaining = data["attempts"][uid][app_name]

            data.setdefault("rejections", []).append({
                "user_id": uid,
                "username": user.username or "N/A",
                "app_name": app_name,
                "reason": "comment_mismatch",
                "timestamp": time.time()
            })

            if uid in data["users"]:
                data["users"][uid]["rejected"] = data["users"][uid].get("rejected", 0) + 1
                data["users"][uid]["total_tasks"] = data["users"][uid].get("total_tasks", 0) + 1
            else:
                data["users"][uid] = {"balance": 0.0, "total_tasks": 1, "accepted": 0, "rejected": 1}

            if remaining > 0:
                save_data(data)
                await update.message.reply_text(
                    f"❌ Proof Rejected\n\n"
                    f"{remaining} attempt(s) remaining.\n"
                    f"Send the screenshot again."
                )
            else:
                auto_ban = data.setdefault("auto_ban", {})
                if uid not in auto_ban:
                    auto_ban[uid] = {"failed_apps": [], "banned_until": None}
                if app_name not in auto_ban[uid]["failed_apps"]:
                    auto_ban[uid]["failed_apps"].append(app_name)

                banned_now = False
                if len(auto_ban[uid]["failed_apps"]) >= 3:
                    auto_ban[uid]["banned_until"] = time.time() + 86400
                    auto_ban[uid]["failed_apps"] = []
                    banned_now = True

                if uid in pending and app_name in pending[uid]:
                    del pending[uid][app_name]
                    if not pending[uid]:
                        del pending[uid]

                jq = context.job_queue
                if jq:
                    for j in jq.get_jobs_by_name(f"timeout_{uid}_{app_name}"):
                        j.schedule_removal()
                    for j in jq.get_jobs_by_name(f"reminder_{uid}_{app_name}"):
                        j.schedule_removal()

                save_data(data)

                if banned_now:
                    await update.message.reply_text(
                        f"❌ All attempts exhausted.\n\n"
                        f"🚫 You are banned for 24 hours.\n"
                        f"Reason: Repeated fake proofs.\n"
                        f"Contact: @Aarvii_005"
                    )
                else:
                    await update.message.reply_text(
                        f"❌ All attempts exhausted.\n\n"
                        f"You cannot submit screenshots for this app anymore.\n\n"
                        f"Try another app or contact: @Aarvii_005",
                        reply_markup=MAIN_KEYBOARD
                    )
                context.user_data["awaiting_proof"] = False
            return

        # VERIFIED
        if uid not in data["users"]:
            data["users"][uid] = {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0}
        data["users"][uid]["total_tasks"] = data["users"][uid].get("total_tasks", 0) + 1
        data["users"][uid]["accepted"] = data["users"][uid].get("accepted", 0) + 1

        if uid not in data["proofs"]:
            data["proofs"][uid] = {}
        data["proofs"][uid][app_name] = True

        data.setdefault("saved_proofs", []).append({
            "user_id": uid,
            "username": user.username or "N/A",
            "app_name": app_name,
            "reviewer_name": reviewer_name,
            "status": "approved",
            "timestamp": time.time()
        })

        data.setdefault("used_screenshots", []).append({
            "user_id": uid,
            "file_unique_id": file_unique_id,
            "file_id": file_id,
            "app_name": app_name,
            "timestamp": time.time()
        })

        auto_ban = data.setdefault("auto_ban", {})
        if uid in auto_ban:
            auto_ban[uid]["failed_apps"] = []

        if uid in pending and app_name in pending[uid]:
            del pending[uid][app_name]
            if not pending[uid]:
                del pending[uid]
        if "attempts" in data and uid in data["attempts"] and app_name in data["attempts"][uid]:
            del data["attempts"][uid][app_name]

        save_data(data)

        jq = context.job_queue
        if jq:
            for j in jq.get_jobs_by_name(f"timeout_{uid}_{app_name}"):
                j.schedule_removal()
            for j in jq.get_jobs_by_name(f"reminder_{uid}_{app_name}"):
                j.schedule_removal()

        admin_caption = (
            f"📥 New Proof Auto-Approved\n"
            f"{SEP}\n"
            f"👤 User: @{user.username or 'N/A'}\n"
            f"🆔 ID: {user.id}\n"
            f"📱 App: {app_name}\n"
            f"🔍 Reviewer: {reviewer_name}\n"
            f"🕒 {fmt_dt(time.time())}"
        )
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_photo(chat_id=aid, photo=file_id, caption=admin_caption)
            except Exception as e:
                print(f"Admin {aid} send error: {e}")

        await update.message.reply_text(
            f"✅ PROOF CHECKED SUCCESSFULLY\n"
            f"{SEP}\n\n"
            f"📱 App: {app_name}\n"
            f"🔍 Reviewer: {reviewer_name}\n\n"
            f"⚠️ Note: Fake screenshots will get your account banned.",
            reply_markup=MAIN_KEYBOARD
        )

    except Exception as e:
        print(f"Verification error: {e}")
        await update.message.reply_text("❌ Could not verify the proof. Please try again.", reply_markup=MAIN_KEYBOARD)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    context.user_data["awaiting_proof"] = False


# ─── BULK ───────────────────────────────────────────

async def handle_bulk_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data_pre = load_data()
    channel = data_pre.get("force_join_channel")
    if channel and not is_admin(update.effective_user.id):
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=update.effective_user.id)
            if member.status not in ("member", "administrator", "creator"):
                await update.message.reply_text(
                    f"⚠️ Please join the channel first.\n\n👉 Join: {channel}",
                    reply_markup=MAIN_KEYBOARD
                )
                return
        except Exception:
            pass

    app_name = context.user_data.get("bulk_app")
    if not app_name:
        return

    photo_file = await update.message.photo[-1].get_file()
    file_unique_id = update.message.photo[-1].file_unique_id
    file_id = update.message.photo[-1].file_id
    user = update.effective_user
    uid = str(user.id)
    data = load_data()

    # Duplicate check
    used_ids = [x.get("file_unique_id") for x in data.get("used_screenshots", [])]
    if file_unique_id in used_ids:
        await update.message.reply_text("❌ This screenshot was already used. Send a new one.")
        return

    pending = data.get("pending_proofs", {})
    if uid not in pending or app_name not in pending[uid]:
        await update.message.reply_text("⏰ No active bulk task. Contact: @Aarvii_005", reply_markup=MAIN_KEYBOARD)
        context.user_data["awaiting_bulk_proof"] = False
        return

    entry = pending[uid][app_name]
    if not entry.get("bulk"):
        context.user_data["awaiting_bulk_proof"] = False
        return

    if time.time() - entry.get("timestamp", 0) > 3600:
        await update.message.reply_text("⏰ Bulk task timed out.", reply_markup=MAIN_KEYBOARD)
        context.user_data["awaiting_bulk_proof"] = False
        return

    remaining = entry.get("remaining", [])
    if not remaining:
        await update.message.reply_text("✅ All comments already verified.", reply_markup=MAIN_KEYBOARD)
        context.user_data["awaiting_bulk_proof"] = False
        return

    await update.message.reply_text(f"⏳ Verifying screenshot ({len(entry.get('verified', []))}/{entry.get('count', 0)} done)...")

    file_path = "temp_bulk_proof.jpg"
    await photo_file.download_to_drive(file_path)

    try:
        matched_comment = None
        reviewer_name = "Unknown"

        for c in remaining:
            try:
                matched, name = await asyncio.to_thread(_verify_with_ai, file_path, c, app_name)
                if matched:
                    matched_comment = c
                    reviewer_name = name
                    break
            except Exception as e:
                print(f"bulk verify error: {e}")
                continue

        if not matched_comment:
            # Add rejection
            data.setdefault("rejections", []).append({
                "user_id": uid,
                "username": user.username or "N/A",
                "app_name": app_name,
                "reason": "bulk_comment_mismatch",
                "timestamp": time.time()
            })
            if uid in data["users"]:
                data["users"][uid]["rejected"] = data["users"][uid].get("rejected", 0) + 1
            save_data(data)
            await update.message.reply_text(
                "❌ Rejected — screenshot does not contain any of your assigned comments.\n\n"
                "Try again with the correct screenshot."
            )
            return

        # Verified one
        entry["remaining"].remove(matched_comment)
        entry.setdefault("verified", []).append(matched_comment)

        data.setdefault("used_screenshots", []).append({
            "user_id": uid,
            "file_unique_id": file_unique_id,
            "file_id": file_id,
            "app_name": app_name,
            "timestamp": time.time()
        })

        if uid not in data["users"]:
            data["users"][uid] = {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0}
        data["users"][uid]["total_tasks"] = data["users"][uid].get("total_tasks", 0) + 1
        data["users"][uid]["accepted"] = data["users"][uid].get("accepted", 0) + 1

        data.setdefault("saved_proofs", []).append({
            "user_id": uid,
            "username": user.username or "N/A",
            "app_name": app_name,
            "reviewer_name": reviewer_name,
            "status": "approved",
            "bulk": True,
            "timestamp": time.time()
        })

        remaining_now = len(entry["remaining"])
        save_data(data)

        # Notify admins
        cap = (
            f"📥 Bulk Proof Verified\n{SEP}\n"
            f"👤 @{user.username or 'N/A'}\n"
            f"🆔 {uid}\n"
            f"📱 {app_name}\n"
            f"🔍 {reviewer_name}\n"
            f"🕒 {fmt_dt(time.time())}"
        )
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_photo(chat_id=aid, photo=file_id, caption=cap)
            except Exception:
                pass

        if remaining_now == 0:
            # Complete
            if uid in pending and app_name in pending[uid]:
                del pending[uid][app_name]
                if not pending[uid]:
                    del pending[uid]

            # Mark request completed
            for r in data.get("bulk_requests", []):
                if str(r.get("user_id")) == uid and r.get("app_name") == app_name and r.get("status") == "approved":
                    r["status"] = "completed"
                    break
            save_data(data)

            jq = context.job_queue
            if jq:
                for j in jq.get_jobs_by_name(f"bulk_timeout_{uid}_{app_name}"):
                    j.schedule_removal()

            await update.message.reply_text(
                f"✅ PROOF CHECKED SUCCESSFULLY\n"
                f"{SEP}\n\n"
                f"🎉 All {entry.get('count')} comments verified!\n"
                f"📱 App: {app_name}\n\n"
                f"⚠️ Note: Fake screenshots will get your account banned.",
                reply_markup=MAIN_KEYBOARD
            )
            context.user_data["awaiting_bulk_proof"] = False
        else:
            await update.message.reply_text(
                f"✅ PROOF CHECKED SUCCESSFULLY\n"
                f"{SEP}\n\n"
                f"📱 App: {app_name}\n"
                f"🔍 Reviewer: {reviewer_name}\n\n"
                f"📸 {remaining_now} screenshot(s) remaining.\n"
                f"Send the next one."
            )

    except Exception as e:
        print(f"bulk verification error: {e}")
        await update.message.reply_text("❌ Verification failed. Try again.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


# ═══════════════════════════════════════════════════
#   CANCEL
# ═══════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════
#   LAUNCH
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_error_handler(error_handler)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('DTX', admin_entry),
            MessageHandler(filters.Regex('^Admin Panel$'), admin_panel_entry),
            MessageHandler(filters.Regex('^Bulk Requests$'), bulk_requests_entry),
            MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry),
            MessageHandler(filters.Regex('^Live Apps$'), live_apps_entry),
            MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs),
            MessageHandler(filters.Regex('^Stats$'), show_stats),
            MessageHandler(filters.Regex('^Broadcast$'), broadcast_entry),
            MessageHandler(filters.Regex('^Add/Remove Bal$'), admin_bal_entry),
            MessageHandler(filters.Regex('^Set Force Join$'), force_join_entry),
            MessageHandler(filters.Regex('^Ban User$'), ban_entry),
            MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry),
            MessageHandler(filters.Regex('^Bulk Comments$'), bulk_comments_entry),
            MessageHandler(filters.Regex('^Withdrawal$'), withdrawal_entry),
        ],
        states={
            ADMIN_MENU: [
                MessageHandler(filters.Regex('^Admin Panel$'), admin_panel_entry),
                MessageHandler(filters.Regex('^Bulk Requests$'), bulk_requests_entry),
                MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry),
                MessageHandler(filters.Regex('^Live Apps$'), live_apps_entry),
                MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs),
                MessageHandler(filters.Regex('^Stats$'), show_stats),
                MessageHandler(filters.Regex('^Broadcast$'), broadcast_entry),
                MessageHandler(filters.Regex('^Add/Remove Bal$'), admin_bal_entry),
                MessageHandler(filters.Regex('^Set Force Join$'), force_join_entry),
                MessageHandler(filters.Regex('^Ban User$'), ban_entry),
            ],
            ADD_APP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app)],
            ADD_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_comments)],
            BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_msg)],
            ADMIN_BAL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bal_user)],
            ADMIN_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bal_amount)],
            WITHDRAW_METHOD: [CallbackQueryHandler(withdraw_method_callback, pattern="^withdraw_")],
            WITHDRAW_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi_id)],
            WITHDRAW_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            FORCE_JOIN_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, force_join_set)],
            BAN_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_user)],
            APP_ADD_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, app_add_comments)],
            USER_HIST_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_hist_input)],
            BULK_SELECT_APP: [CallbackQueryHandler(bulk_app_callback, pattern="^bulkapp_")],
            BULK_ENTER_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_count_input)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('DTX', admin_entry),
            MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry),
            MessageHandler(filters.Regex('^Bulk Comments$'), bulk_comments_entry),
            MessageHandler(filters.Regex('^My Profile$'), show_profile),
            MessageHandler(filters.Regex('^History$'), show_history),
            MessageHandler(filters.Regex('^Withdrawal$'), withdrawal_entry),
            MessageHandler(filters.Regex('^Admin Panel$'), admin_panel_entry),
            MessageHandler(filters.Regex('^Bulk Requests$'), bulk_requests_entry),
            MessageHandler(filters.Regex('^Live Apps$'), live_apps_entry),
            MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs),
            MessageHandler(filters.Regex('^Stats$'), show_stats),
            MessageHandler(filters.Regex('^Broadcast$'), broadcast_entry),
            MessageHandler(filters.Regex('^Add/Remove Bal$'), admin_bal_entry),
            MessageHandler(filters.Regex('^Set Force Join$'), force_join_entry),
            MessageHandler(filters.Regex('^Ban User$'), ban_entry),
            MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry),
        ],
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)

    # Panel + bulk callbacks
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^apanel_"))
    app.add_handler(CallbackQueryHandler(bulk_decision_callback, pattern="^bulk_(approve|reject)_"))
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(user_app_callback, pattern="^getapp_"))
    app.add_handler(CallbackQueryHandler(withdrawal_status_callback, pattern="^wdstatus_"))
    app.add_handler(CallbackQueryHandler(manage_app_callback, pattern="^manageapp_"))
    app.add_handler(CallbackQueryHandler(add_cmt_to_app_callback, pattern="^addcmt_"))
    app.add_handler(CallbackQueryHandler(remove_cmt_callback, pattern="^rmcmt_"))
    app.add_handler(CallbackQueryHandler(delete_cmt_callback, pattern="^delcmt_"))
    app.add_handler(CallbackQueryHandler(delete_app_callback, pattern="^delapp_"))

    app.add_handler(MessageHandler(filters.Regex('^My Profile$'), show_profile))
    app.add_handler(MessageHandler(filters.Regex('^History$'), show_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_proof_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("🤖 Bot is running (polling)...")
    app.run_polling(drop_pending_updates=True)
