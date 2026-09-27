import logging
import os
import json
import random
import base64
import asyncio
import time
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

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0").strip())
except ValueError:
    ADMIN_ID = 0

if not TELEGRAM_TOKEN: raise ValueError("Missing TELEGRAM_TOKEN.")
if not GROQ_API_KEY: raise ValueError("Missing GROQ_API_KEY.")
if not MONGODB_URI: raise ValueError("Missing MONGODB_URI.")

# --- DB ---
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    print("✅ Database connected.")
except Exception as e:
    print(f"❌ DB error: {e}")
    raise e

db = mongo_client["telegram_bot"]
collection = db["data"]

DEFAULTS = {
    "apps": {}, "history": {}, "proofs": {}, "saved_proofs": [],
    "users": {}, "withdrawals": [], "banned_users": [], "rejections": [],
    "used_screenshots": [], "pending_proofs": {}, "force_join_channel": None
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

def is_banned(data, user_id):
    return str(user_id) in [str(x) for x in data.get("banned_users", [])]

def count_used(data, app_name):
    used = 0
    for uid, uh in data.get("history", {}).items():
        if app_name in uh and uh[app_name]:
            used += 1
    return used

# --- Keyboards ---
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["Get Comment", "My Profile"], ["Withdrawal", "History"]],
    resize_keyboard=True
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Add Comment", "Live Apps"],
        ["Saved Proof", "Stats"],
        ["Broadcast", "Add/Remove Bal"],
        ["Set Force Join", "Ban User"]
    ],
    resize_keyboard=True
)

# --- States ---
(
    ADMIN_MENU, ADD_APP, ADD_COMMENTS, BROADCAST_MSG, ADMIN_BAL_ID, ADMIN_BAL_AMT,
    WITHDRAW_METHOD, WITHDRAW_UPI, WITHDRAW_AMT,
    FORCE_JOIN_SET, BAN_USER_ID, APP_ADD_COMMENTS, APP_REMOVE_COMMENT
) = range(13)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
SEP = "──────────────────────"

# --- Error handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception:", exc_info=context.error)
    if ADMIN_ID != 0:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Bot Error:\n\n{context.error}")
        except Exception:
            pass

# ═══════════════════════════════════════════════════
#   FORCE JOIN CHECK
# ═══════════════════════════════════════════════════

async def ensure_joined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    data = load_data()
    channel = data.get("force_join_channel")
    if not channel:
        return True
    user_id = update.effective_user.id
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
        f"⚠️ You must join our channel to use this bot.\n\n"
        f"👉 Join: {channel}\n\n"
        f"After joining, tap 'I Joined' below."
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
        await query.edit_message_text("✅ Verified. Send /start to begin.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=update.effective_user.id)
        if member.status in ("member", "administrator", "creator"):
            await query.edit_message_text("✅ Verified! You can now use the bot.\n\nSend /start to begin.")
            return
    except Exception as e:
        print(f"verify error: {e}")
    await query.answer("❌ You haven't joined the channel yet.", show_alert=True)

# ═══════════════════════════════════════════════════
#   START
# ═══════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    context.user_data["proof_app"] = None

    data = load_data()
    if is_banned(data, update.effective_user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.\nContact @dtxzahid for support.")
        return

    if not await ensure_joined(update, context):
        return

    name = update.effective_user.first_name or "there"
    text = (
        f"👋 Hey {name}!\n"
        f"{SEP}\n\n"
        f"Welcome to the Task Bot.\n"
        f"Complete simple tasks and earn rewards.\n\n"
        f"Choose an option below to start."
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

# ═══════════════════════════════════════════════════
#   ADMIN PANEL
# ═══════════════════════════════════════════════════

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Not authorized.")
        return ConversationHandler.END
    await update.message.reply_text(
        f"🛠️ Admin Panel\n{SEP}\n\nChoose an action:",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU

# --- Add Comment ---
async def add_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Enter the App Name:")
    return ADD_APP

async def add_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_app_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ App: {context.user_data['temp_app_name']}\n\n"
        f"Now send comments separated by commas.\n"
        f"Example: `Nice app,Good UI,Love it`",
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
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END

# --- Live Apps ---
async def live_apps_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = data.get("apps", {})
    if not apps:
        await update.message.reply_text("📭 No apps yet.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = f"📱 Live Apps\n{SEP}\n\nTap an app to manage it:"
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
    if update.effective_user.id != ADMIN_ID:
        return
    app_name = query.data.replace("manageapp_", "")
    data = load_data()
    if app_name not in data["apps"]:
        await query.edit_message_text("❌ App no longer exists.")
        return
    total = len(data["apps"][app_name])
    used = count_used(data, app_name)
    left = total - used
    text = (
        f"📱 {app_name}\n"
        f"{SEP}\n"
        f"Total Comments: {total}\n"
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
    app_name = query.data.replace("addcmt_", "")
    context.user_data["app_to_add"] = app_name
    await query.edit_message_text(f"Send new comments for *{app_name}* separated by commas:", parse_mode="Markdown")
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
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END

async def remove_cmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_name = query.data.replace("rmcmt_", "")
    data = load_data()
    comments = data["apps"].get(app_name, [])
    if not comments:
        await query.edit_message_text("No comments to remove.")
        return
    keyboard = []
    for i, c in enumerate(comments[:20]):  # limit 20
        short = c[:40] + ("..." if len(c) > 40 else "")
        keyboard.append([InlineKeyboardButton(f"❌ {short}", callback_data=f"delcmt_{app_name}_{i}")])
    keyboard.append([InlineKeyboardButton("↩️ Back", callback_data=f"manageapp_{app_name}")])
    await query.edit_message_text(
        f"Tap a comment to delete it from *{app_name}*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete_cmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_", 2)
    # parts: ['delcmt', app_name, index]
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
    app_name = query.data.replace("delapp_", "")
    data = load_data()
    if app_name in data["apps"]:
        del data["apps"][app_name]
        save_data(data)
        await query.edit_message_text(f"🗑️ Deleted app: {app_name}")
    else:
        await query.edit_message_text("❌ App not found.")

# --- Saved Proofs ---
async def show_saved_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    proofs = data.get("saved_proofs", [])
    if not proofs:
        await update.message.reply_text("📭 No saved proofs yet.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = f"📋 Saved Proofs — Last 10\n{SEP}\n\n"
    for i, p in enumerate(proofs[-10:], 1):
        text += (
            f"{i}. {p.get('app_name', 'N/A')}\n"
            f"   👤 Reviewer: {p.get('reviewer_name', 'N/A')}\n"
            f"   📱 User: @{p.get('username', 'N/A')}\n"
            f"   🆔 ID: {p.get('user_id', 'N/A')}\n\n"
        )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# --- Stats ---
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    users = data.get("users", {})
    proofs = data.get("saved_proofs", [])
    rejections = data.get("rejections", [])
    approved = len(proofs)
    rejected = len(rejections)
    total_balance = sum(u.get("balance", 0) for u in users.values())
    total_withdrawn = sum(w.get("amount", 0) for w in data.get("withdrawals", []) if w.get("status") == "approved")
    text = (
        f"📊 Bot Statistics\n"
        f"{SEP}\n\n"
        f"👥 Total Users: {len(users)}\n"
        f"📝 Total Approved: {approved}\n"
        f"❌ Total Rejected: {rejected}\n\n"
        f"{SEP}\n"
        f"💰 Active Balance: ₹{total_balance:.2f}\n"
        f"💸 Total Withdrawn: ₹{total_withdrawn:.2f}"
    )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# --- Broadcast ---
async def broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 Send the message to broadcast:")
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

# --- Balance ---
async def admin_bal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖️ Send the User ID:")
    return ADMIN_BAL_ID

async def admin_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bal_user_id"] = update.message.text.strip()
    await update.message.reply_text("💵 Send amount (e.g., 50 to add, -50 to remove):")
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
    action = "added to" if amount > 0 else "removed from"
    await update.message.reply_text(
        f"✅ Balance updated.\n{SEP}\n👤 {uid}\n💵 ₹{abs(amount):.2f} {action} balance\n💰 New: ₹{data['users'][uid]['balance']:.2f}",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU

# --- Force Join Setter ---
async def force_join_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    current = data.get("force_join_channel") or "Not set"
    await update.message.reply_text(
        f"📢 Current Force Join: {current}\n\n"
        f"Send the channel username (e.g., @mychannel) or send `off` to disable.",
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
    await update.message.reply_text(f"✅ Force join set to {val}.", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# --- Ban User ---
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
        await update.message.reply_text(f"⚠️ User {uid} is already banned.", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# ═══════════════════════════════════════════════════
#   USER: PROFILE / HISTORY
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
    proofs = [p for p in data.get("saved_proofs", []) if p.get("user_id") == uid]
    rejections = [r for r in data.get("rejections", []) if r.get("user_id") == uid]
    if not proofs and not rejections:
        await update.message.reply_text("📭 No task history yet.", reply_markup=MAIN_KEYBOARD)
        return
    text = f"📜 Task History\n{SEP}\n\n"
    combined = []
    for p in proofs:
        combined.append(("✅", p.get("app_name", "N/A"), p.get("reviewer_name", "N/A")))
    for r in rejections:
        combined.append(("❌", r.get("app_name", "N/A"), r.get("reason", "Rejected")))
    for icon, app, detail in combined[-10:]:
        text += f"{icon} {app}\n   {detail}\n\n"
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
    text = f"💰 Withdrawal\n{SEP}\n\nAvailable: ₹{bal:.2f}\n\nChoose method:"
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
            f"🎗️ VSV Withdrawal\n{SEP}\n\nPlease message the owner directly:\n👉 @dtxzahid"
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
        "amount": amount, "method": "UPI", "upi_id": upi, "status": "pending"
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
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=keyboard)
    except Exception as e:
        print(f"Send admin error: {e}")

    await update.message.reply_text(
        f"✅ Withdrawal submitted.\n{SEP}\nPending approval.",
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END

async def withdrawal_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
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
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"✅ Withdrawal Approved\n{SEP}\n\n₹{target['amount']:.2f} has been processed.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception: pass
        await query.edit_message_text(f"✅ Approved ₹{target['amount']:.2f} for {uid}.")
    else:
        target["status"] = "rejected"
        if uid in data["users"]:
            data["users"][uid]["balance"] += target["amount"]
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"❌ Withdrawal Rejected\n{SEP}\n\n₹{target['amount']:.2f} refunded to your balance.\nContact @dtxzahid if you have questions.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception: pass
        await query.edit_message_text(f"❌ Rejected. ₹{target['amount']:.2f} refunded to {uid}.")

# ═══════════════════════════════════════════════════
#   GET COMMENT
# ═══════════════════════════════════════════════════

async def get_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, context): return ConversationHandler.END
    data = load_data()
    if is_banned(data, update.effective_user.id):
        await update.message.reply_text("🚫 You are banned.")
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
    app_name = query.data.replace("getapp_", "")
    uid = str(update.effective_user.id)
    data = load_data()

    if app_name not in data["apps"] or not data["apps"][app_name]:
        await query.edit_message_text("📭 No comments for this app.")
        return

    if uid not in data["history"]:
        data["history"][uid] = {}

    if app_name in data["history"][uid] and data["history"][uid][app_name]:
        await query.edit_message_text(
            "⚠️ You have already received a comment for this app.\n\n"
            "If you want more comments then contact admin @DTXZAHID"
        )
        return

    available = [c for c in data["apps"][app_name] if c not in data["history"][uid].get(app_name, [])]
    if not available:
        await query.edit_message_text("📭 No more unique comments available.")
        return

    chosen = random.choice(available)
    data["history"][uid][app_name] = [chosen]

    # Store pending proof for timeout
    if "pending_proofs" not in data:
        data["pending_proofs"] = {}
    if uid not in data["pending_proofs"]:
        data["pending_proofs"][uid] = {}
    data["pending_proofs"][uid][app_name] = {
        "comment": chosen,
        "timestamp": time.time()
    }
    save_data(data)

    await query.edit_message_text(
        f"💬 Your Comment\n{SEP}\n\n`{chosen}`\n\nTap above to copy.",
        parse_mode="Markdown"
    )
    await context.bot.send_message(
        chat_id=uid,
        text=(
            f"📸 Send the screenshot here.\n\n"
            f"⏰ You have 1 hour to submit.\n"
            f"After 1 hour, your proof will not be accepted.\n"
            f"Contact @dtxzahid if you need more time."
        )
    )
    context.user_data["awaiting_proof"] = True
    context.user_data["proof_app"] = app_name

    # Schedule timeout (1 hour) and reminder (30 min)
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
    comment = pending[uid][app_name]["comment"]
    del pending[uid][app_name]
    if not pending[uid]:
        del pending[uid]
    # Return comment to pool
    if app_name in data["apps"] and comment not in data["apps"][app_name]:
        data["apps"][app_name].append(comment)
    # Clear user history so they can get a new comment
    if uid in data["history"] and app_name in data["history"][uid]:
        del data["history"][uid][app_name]
    save_data(data)
    try:
        await context.bot.send_message(
            chat_id=int(uid),
            text=(
                f"⏰ Time exceeded.\n{SEP}\n\n"
                f"You did not submit your proof for {app_name} within 1 hour.\n\n"
                f"Contact @dtxzahid for a new comment."
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
            text=f"⏰ 30 minutes left to submit your proof for {app_name}!"
        )
    except Exception: pass

# ═══════════════════════════════════════════════════
#   PROOF HANDLING
# ═══════════════════════════════════════════════════

async def handle_proof_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        await update.message.reply_text("⚠️ Please send a screenshot (image), not text.")
        return

async def handle_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_proof"):
        return

    photo_file = await update.message.photo[-1].get_file()
    file_unique_id = update.message.photo[-1].file_unique_id
    app_name = context.user_data.get("proof_app", "Unknown")
    user = update.effective_user
    uid = str(user.id)
    data = load_data()

    # Duplicate check
    used_ids = [x.get("file_unique_id") for x in data.get("used_screenshots", [])]
    if file_unique_id in used_ids:
        await update.message.reply_text(
            "❌ This screenshot has already been used.\n\n"
            "Please submit a fresh screenshot.",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    # Check if still pending (timeout may have fired)
    pending = data.get("pending_proofs", {})
    if uid not in pending or app_name not in pending[uid]:
        await update.message.reply_text(
            "⏰ Time exceeded or no active task.\n\n"
            "Contact @dtxzahid for a new comment.",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    # Timestamp check
    ts = pending[uid][app_name].get("timestamp", 0)
    if time.time() - ts > 3600:
        # Expired
        comment = pending[uid][app_name]["comment"]
        del pending[uid][app_name]
        if not pending[uid]:
            del pending[uid]
        if app_name in data["apps"] and comment not in data["apps"][app_name]:
            data["apps"][app_name].append(comment)
        if uid in data["history"] and app_name in data["history"][uid]:
            del data["history"][uid][app_name]
        save_data(data)
        await update.message.reply_text(
            "⏰ Time exceeded.\n\nContact @dtxzahid for a new comment.",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    if uid in data["proofs"] and app_name in data["proofs"][uid]:
        await update.message.reply_text(
            "❌ You already submitted a proof for this app.\n\n"
            "Contact @dtxzahid if you uploaded the wrong screenshot.",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    await update.message.reply_text(
        "⏳ Proof is being checked by our admin. Please wait for 5-10 seconds..."
    )

    file_path = "temp_proof.jpg"
    await photo_file.download_to_drive(file_path)
    try:
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        chosen_comment = data["history"][uid][app_name][0]
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

        comment_matched = result.get("comment_match", False)

        if not comment_matched:
            # Log rejection
            data.setdefault("rejections", []).append({
                "user_id": uid,
                "username": user.username or "N/A",
                "app_name": app_name,
                "reason": "comment_mismatch",
                "timestamp": time.time()
            })
            # Cleanup pending
            if uid in pending and app_name in pending[uid]:
                del pending[uid][app_name]
                if not pending[uid]:
                    del pending[uid]
            if uid in data["users"]:
                data["users"][uid]["rejected"] = data["users"][uid].get("rejected", 0) + 1
                data["users"][uid]["total_tasks"] = data["users"][uid].get("total_tasks", 0) + 1
            save_data(data)
            await update.message.reply_text(
                f"❌ The proof is fake or invalid so it's rejected.\n\n"
                f"If you think the proof is real then you can contact admin @DTXZAHID",
                reply_markup=MAIN_KEYBOARD
            )
            context.user_data["awaiting_proof"] = False
            return

        # Verified — auto-approve
        reviewer_name = result.get("reviewer_name", "Unknown")

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
            "timestamp": time.time()
        })

        # Cleanup pending
        if uid in pending and app_name in pending[uid]:
            del pending[uid][app_name]
            if not pending[uid]:
                del pending[uid]

        save_data(data)

        # Cancel scheduled jobs
        jq = context.job_queue
        if jq:
            for j in jq.get_jobs_by_name(f"timeout_{uid}_{app_name}"):
                j.schedule_removal()
            for j in jq.get_jobs_by_name(f"reminder_{uid}_{app_name}"):
                j.schedule_removal()

        # Notify admin (no buttons)
        admin_caption = (
            f"📥 New Proof Auto-Approved\n"
            f"{SEP}\n"
            f"👤 User: @{user.username or 'N/A'}\n"
            f"🆔 ID: {user.id}\n"
            f"📱 App: {app_name}\n"
            f"🔍 Reviewer: {reviewer_name}"
        )
        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_file.file_id,
                caption=admin_caption
            )
        except Exception as e:
            print(f"Admin send error: {e}")

        await update.message.reply_text(
            f"✅ Proof Verified\n"
            f"{SEP}\n\n"
            f"Your proof has been accepted.\n"
            f"⚠️ Note: Fake screenshots may lead to account suspension.",
            reply_markup=MAIN_KEYBOARD
        )

    except Exception as e:
        print(f"Verification error: {e}")
        await update.message.reply_text("❌ Error verifying proof. Try again later.", reply_markup=MAIN_KEYBOARD)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    context.user_data["awaiting_proof"] = False

# ═══════════════════════════════════════════════════
#   CANCEL
# ═══════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.", reply_markup=MAIN_KEYBOARD)
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
            MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry),
            MessageHandler(filters.Regex('^Live Apps$'), live_apps_entry),
            MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs),
            MessageHandler(filters.Regex('^Stats$'), show_stats),
            MessageHandler(filters.Regex('^Broadcast$'), broadcast_entry),
            MessageHandler(filters.Regex('^Add/Remove Bal$'), admin_bal_entry),
            MessageHandler(filters.Regex('^Set Force Join$'), force_join_entry),
            MessageHandler(filters.Regex('^Ban User$'), ban_entry),
            MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry),
            MessageHandler(filters.Regex('^Withdrawal$'), withdrawal_entry),
        ],
        states={
            ADMIN_MENU: [
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
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('DTX', admin_entry),
            MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry),
            MessageHandler(filters.Regex('^My Profile$'), show_profile),
            MessageHandler(filters.Regex('^History$'), show_history),
            MessageHandler(filters.Regex('^Withdrawal$'), withdrawal_entry),
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

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(user_app_callback, pattern="^getapp_"))
    app.add_handler(CallbackQueryHandler(withdrawal_status_callback, pattern="^wdstatus_"))
    app.add_handler(CallbackQueryHandler(manage_app_callback, pattern="^manageapp_"))
    app.add_handler(CallbackQueryHandler(add_cmt_to_app_callback, pattern="^addcmt_"))
    app.add_handler(CallbackQueryHandler(remove_cmt_callback, pattern="^rmcmt_"))
    app.add_handler(CallbackQueryHandler(delete_cmt_callback, pattern="^delcmt_"))
    app.add_handler(CallbackQueryHandler(delete_app_callback, pattern="^delapp_"))

    # User text/photo
    app.add_handler(MessageHandler(filters.Regex('^My Profile$'), show_profile))
    app.add_handler(MessageHandler(filters.Regex('^History$'), show_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_proof_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_proof_photo))

    print("🤖 Bot is running (polling)...")
    app.run_polling(drop_pending_updates=True)
