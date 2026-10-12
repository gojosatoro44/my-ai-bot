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
    "used_screenshots": [], "pending_proofs": {}, "force_join_channel": None,
    "attempts": {},
    "comment_usage": {},
    "last_request": {},
    "auto_ban": {}
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

(
    ADMIN_MENU, ADD_APP, ADD_COMMENTS, BROADCAST_MSG, ADMIN_BAL_ID, ADMIN_BAL_AMT,
    WITHDRAW_METHOD, WITHDRAW_UPI, WITHDRAW_AMT,
    FORCE_JOIN_SET, BAN_USER_ID, APP_ADD_COMMENTS, APP_REMOVE_COMMENT
) = range(13)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
SEP = "──────────────────────"

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception:", exc_info=context.error)
    if ADMIN_ID != 0:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Bot Error:\n\n{context.error}")
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
        f"⚠️ Bot use karne ke liye pehle channel join karo.\n\n"
        f"👉 Join: {channel}\n\n"
        f"Join karne ke baad 'I Joined' button dabao."
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
        await query.edit_message_text("✅ Verified. /start bhejo.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id=channel, user_id=update.effective_user.id)
        if member.status in ("member", "administrator", "creator"):
            await query.edit_message_text("✅ Verified! Ab bot use kar sakte ho.\n\n/start bhejo.")
            return
    except Exception as e:
        print(f"verify error: {e}")
    await query.answer("❌ Aapne abhi channel join nahi kiya.", show_alert=True)

# ═══════════════════════════════════════════════════
#   START
# ═══════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    context.user_data["proof_app"] = None

    data = load_data()
    if is_banned(data, update.effective_user.id):
        await update.message.reply_text("🚫 Aap ban ho chuke ho.\nContact: @dtxzahid")
        return

    if not await ensure_joined(update, context):
        return

    name = update.effective_user.first_name or "dost"
    text = (
        f"👋 Welcome {name}!\n"
        f"{SEP}\n\n"
        f"Ye bot se aap review ke liye comment le sakte ho aur proof bhi submit kar sakte ho.\n\n"
        f"Neeche se option choose karo."
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

async def add_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 App Name bhejo:")
    return ADD_APP

async def add_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_app_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ App: {context.user_data['temp_app_name']}\n\n"
        f"Ab comments comma se separate karke bhejo.\n"
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
        f"✅ Comments save ho gaye.\n{SEP}\n📱 App: {app_name}\n💬 Added: {len(comments)}",
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END

async def live_apps_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = data.get("apps", {})
    if not apps:
        await update.message.reply_text("📭 Abhi koi app nahi hai.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = f"📱 Live Apps\n{SEP}\n\nKisi app pe tap karo:"
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
        await query.edit_message_text("❌ App exist nahi karta.")
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
        f"Action choose karo:"
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
    await query.edit_message_text(f"*{app_name}* ke liye naye comments bhejo comma se:", parse_mode="Markdown")
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
        f"✅ {app_name} me {len(comments)} comments add ho gaye.",
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
        await query.edit_message_text("Koi comment nahi hai.")
        return
    keyboard = []
    for i, c in enumerate(comments[:20]):
        short = c[:40] + ("..." if len(c) > 40 else "")
        keyboard.append([InlineKeyboardButton(f"❌ {short}", callback_data=f"delcmt_{app_name}_{i}")])
    keyboard.append([InlineKeyboardButton("↩️ Back", callback_data=f"manageapp_{app_name}")])
    await query.edit_message_text(
        f"*{app_name}* se comment delete karne ke liye tap karo:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete_cmt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_", 2)
    app_name = parts[1]
    index = int(parts[2])
    data = load_data()
    if app_name in data["apps"] and 0 <= index < len(data["apps"][app_name]):
        removed = data["apps"][app_name].pop(index)
        save_data(data)
        await query.edit_message_text(f"✅ Remove: {removed[:60]}")
    else:
        await query.edit_message_text("❌ Comment nahi mila.")

async def delete_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_name = query.data.replace("delapp_", "")
    data = load_data()
    if app_name in data["apps"]:
        del data["apps"][app_name]
        save_data(data)
        await query.edit_message_text(f"🗑️ Delete: {app_name}")
    else:
        await query.edit_message_text("❌ App nahi mila.")

async def show_saved_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    proofs = data.get("saved_proofs", [])
    if not proofs:
        await update.message.reply_text("📭 Abhi koi saved proof nahi.", reply_markup=ADMIN_KEYBOARD)
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

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    users = data.get("users", {})
    proofs = data.get("saved_proofs", [])
    rejections = data.get("rejections", [])
    approved = len(proofs)
    rejected = len(rejections)
    total_balance = sum(u.get("balance", 0) for u in users.values())
    total_withdrawn = sum(w.get("amount", 0) for w in data.get("withdrawals", []) if w.get("status") == "approved")
    pending_count = sum(len(v) for v in data.get("pending_proofs", {}).values())

    text = (
        f"📊 Bot Statistics\n"
        f"{SEP}\n\n"
        f"👥 Total Users: {len(users)}\n"
        f"📝 Approved Proofs: {approved}\n"
        f"❌ Rejected Proofs: {rejected}\n"
        f"⏳ Active Tasks: {pending_count}\n\n"
        f"{SEP}\n"
        f"💰 Active Balance: ₹{total_balance:.2f}\n"
        f"💸 Total Withdrawn: ₹{total_withdrawn:.2f}"
    )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 Broadcast message bhejo:")
    return BROADCAST_MSG

async def broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    data = load_data()
    users = list(data.get("users", {}).keys())
    await update.message.reply_text(f"⏳ {len(users)} users ko bhej raha hoon...")
    count = 0
    final_msg = f"📢 Announcement\n{SEP}\n\n{msg}"
    for uid in users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=final_msg)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ {count} users ko deliver ho gaya.", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def admin_bal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖️ User ID bhejo:")
    return ADMIN_BAL_ID

async def admin_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bal_user_id"] = update.message.text.strip()
    await update.message.reply_text("💵 Amount bhejo (e.g., 50 add, -50 remove):")
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
        f"✅ Balance update.\n{SEP}\n👤 {uid}\n💵 ₹{abs(amount):.2f} {action}\n💰 New: ₹{data['users'][uid]['balance']:.2f}",
        reply_markup=ADMIN_KEYBOARD
    )
    return ADMIN_MENU

async def force_join_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    current = data.get("force_join_channel") or "Not set"
    await update.message.reply_text(
        f"📢 Current Force Join: {current}\n\n"
        f"Channel username bhejo (e.g., @mychannel) ya `off` likho.",
        parse_mode="Markdown"
    )
    return FORCE_JOIN_SET

async def force_join_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    data = load_data()
    if val.lower() == "off":
        data["force_join_channel"] = None
        save_data(data)
        await update.message.reply_text("✅ Force join off.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    if not val.startswith("@"):
        val = "@" + val
    data["force_join_channel"] = val
    save_data(data)
    await update.message.reply_text(f"✅ Force join set: {val}", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def ban_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 User ID bhejo ban karne ke liye:")
    return BAN_USER_ID

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text.strip()
    data = load_data()
    banned = data.get("banned_users", [])
    if uid not in [str(x) for x in banned]:
        banned.append(uid)
        data["banned_users"] = banned
        save_data(data)
        await update.message.reply_text(f"✅ User {uid} ban ho gaya.", reply_markup=ADMIN_KEYBOARD)
    else:
        await update.message.reply_text(f"⚠️ User {uid} already banned.", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

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
        f"👤 Meri Profile\n"
        f"{SEP}\n\n"
        f"Naam: {name}\n"
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
        await update.message.reply_text("📭 Abhi tak koi task nahi kiya.", reply_markup=MAIN_KEYBOARD)
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
            f"💰 Withdrawal\n{SEP}\n\n❌ Minimum ₹10 hai.\n💵 Aapka balance: ₹{bal:.2f}",
            reply_markup=MAIN_KEYBOARD
        )
        return ConversationHandler.END
    text = f"💰 Withdrawal\n{SEP}\n\nAvailable: ₹{bal:.2f}\n\nMethod choose karo:"
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
        await query.edit_message_text("💳 Apna UPI ID bhejo:")
        return WITHDRAW_UPI
    else:
        await query.edit_message_text(
            f"🎗️ VSV Withdrawal\n{SEP}\n\nOwner ko direct message karo:\n👉 @dtxzahid"
        )
        return ConversationHandler.END

async def withdraw_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["withdraw_upi"] = update.message.text.strip()
    await update.message.reply_text("💵 Amount bhejo (Min ₹10):")
    return WITHDRAW_AMT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    if amount < 10:
        await update.message.reply_text("❌ Minimum ₹10 hai.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    uid = str(update.effective_user.id)
    data = load_data()
    if amount > data["users"].get(uid, {}).get("balance", 0.0):
        await update.message.reply_text("❌ Balance kam hai.", reply_markup=MAIN_KEYBOARD)
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
        f"✅ Withdrawal request submit ho gayi.\n{SEP}\nApproval ka wait karo.",
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
        await query.edit_message_text(f"⚠️ {uid} ke liye koi pending withdrawal nahi.")
        return

    if action == "approve":
        target["status"] = "approved"
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"✅ Withdrawal Approve\n{SEP}\n\n₹{target['amount']:.2f} process ho gaya.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception: pass
        await query.edit_message_text(f"✅ ₹{target['amount']:.2f} approve for {uid}.")
    else:
        target["status"] = "rejected"
        if uid in data["users"]:
            data["users"][uid]["balance"] += target["amount"]
        save_data(data)
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"❌ Withdrawal Reject\n{SEP}\n\n₹{target['amount']:.2f} aapke balance me wapas.\nContact @dtxzahid.",
                reply_markup=MAIN_KEYBOARD
            )
        except Exception: pass
        await query.edit_message_text(f"❌ Rejected. ₹{target['amount']:.2f} refund {uid}.")

# ═══════════════════════════════════════════════════
#   GET COMMENT  (with stale history fix)
# ═══════════════════════════════════════════════════

async def get_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, context): return ConversationHandler.END
    data = load_data()
    uid = str(update.effective_user.id)

    if is_banned(data, uid):
        await update.message.reply_text("🚫 Aap ban ho chuke ho.")
        return ConversationHandler.END

    hrs = auto_ban_hours_left(data, uid)
    if hrs > 0:
        await update.message.reply_text(
            f"🚫 Aapko {hrs} ghante ke liye ban kiya gaya hai.\n\n"
            f"Wajah: Baar baar fake proofs.\n"
            f"Contact: @dtxzahid"
        )
        return ConversationHandler.END

    apps = list(data.get("apps", {}).keys())
    if not apps:
        await update.message.reply_text("📭 Abhi koi task available nahi hai.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"📱 {app}", callback_data=f"getapp_{app}")] for app in apps]
    await update.message.reply_text(
        f"📋 Available Apps\n{SEP}\n\nKoi app select karo:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def user_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_name = query.data.replace("getapp_", "")
    uid = str(update.effective_user.id)
    data = load_data()

    # Rate limit
    last = data.setdefault("last_request", {})
    now = time.time()
    if uid in last and now - last[uid] < 120:
        remaining = int(120 - (now - last[uid]))
        await query.edit_message_text(
            f"⏳ Thoda ruko!\n\n{remaining} seconds ke baad dobara try karo."
        )
        return

    # Auto-ban check
    hrs = auto_ban_hours_left(data, uid)
    if hrs > 0:
        await query.edit_message_text(
            f"🚫 Aapko {hrs} ghante ke liye ban kiya gaya hai.\n\nContact: @dtxzahid"
        )
        return

    if app_name not in data["apps"] or not data["apps"][app_name]:
        await query.edit_message_text("📭 Is app ke liye comment nahi hai.")
        return

    if uid not in data["history"]:
        data["history"][uid] = {}

    # ═══ FIX: Check if history entry is stale ═══
    if app_name in data["history"][uid] and data["history"][uid][app_name]:
        has_pending = uid in data.get("pending_proofs", {}) and app_name in data["pending_proofs"].get(uid, {})
        has_submitted = uid in data.get("proofs", {}) and app_name in data["proofs"].get(uid, {})

        if not has_pending and not has_submitted:
            # Stale entry — clear it and continue
            del data["history"][uid][app_name]
            if "attempts" in data and uid in data["attempts"]:
                data["attempts"][uid].pop(app_name, None)
            save_data(data)
        else:
            await query.edit_message_text(
                "⚠️ Aapne is app ka comment already le liya hai.\n\n"
                "Aur comment chahiye toh contact karo: @DTXZAHID"
            )
            return

    available = [c for c in data["apps"][app_name] if c not in data["history"][uid].get(app_name, [])]
    if not available:
        await query.edit_message_text("📭 Aur unique comment available nahi hai.")
        return

    # Comment rotation
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
        "timestamp": now
    }

    last[uid] = now
    save_data(data)

    await query.edit_message_text(
        f"💬 Aapka Comment\n{SEP}\n\n`{chosen}`\n\nCopy karne ke liye upar tap karo.",
        parse_mode="Markdown"
    )
    await context.bot.send_message(
        chat_id=uid,
        text=(
            f"📸 Screenshot yaha bhejo.\n\n"
            f"⏰ Aapke paas 1 ghante ka time hai.\n"
            f"1 ghante ke baad proof accept nahi hoga.\n"
            f"Zarurat ho toh contact karo: @dtxzahid"
        )
    )
    context.user_data["awaiting_proof"] = True
    context.user_data["proof_app"] = app_name

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
                f"⏰ Time khatam!\n{SEP}\n\n"
                f"Aapne 1 ghante me {app_name} ka proof nahi bheja.\n\n"
                f"Naya comment ke liye contact karo: @dtxzahid"
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
            text=f"⏰ Sirf 30 minute bache hain {app_name} ka proof bhejne ke liye!"
        )
    except Exception: pass

# ═══════════════════════════════════════════════════
#   PROOF HANDLING
# ═══════════════════════════════════════════════════

async def handle_proof_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        await update.message.reply_text("⚠️ Sirf screenshot bhejo, text nahi.")
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

    used_ids = [x.get("file_unique_id") for x in data.get("used_screenshots", [])]
    if file_unique_id in used_ids:
        await update.message.reply_text(
            "❌ Ye screenshot already use ho chuka hai.\n\nNaya screenshot bhejo.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    pending = data.get("pending_proofs", {})
    if uid not in pending or app_name not in pending[uid]:
        await update.message.reply_text(
            "⏰ Time khatam ya koi active task nahi.\n\nContact: @dtxzahid",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    ts = pending[uid][app_name].get("timestamp", 0)
    if time.time() - ts > 3600:
        comment = pending[uid][app_name]["comment"]
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
        await update.message.reply_text(
            "⏰ Time khatam.\n\nContact: @dtxzahid",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    if uid in data["proofs"] and app_name in data["proofs"][uid]:
        await update.message.reply_text(
            "❌ Aapne is app ka proof already submit kar diya hai.\n\n"
            "Galat screenshot tha toh contact: @dtxzahid",
            reply_markup=MAIN_KEYBOARD
        )
        context.user_data["awaiting_proof"] = False
        return

    await update.message.reply_text(
        "⏳ Proof check kiya ja raha hai. 5-10 second wait karo..."
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

        # REJECTED
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
                    f"{remaining} Attempts Baaki Hain\n"
                    f"Dobara Screenshot Bhejo"
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
                        f"❌ Saare Attempts Khatam\n\n"
                        f"🚫 Aapko 24 ghante ke liye ban kiya gaya hai.\n"
                        f"Wajah: Baar baar fake proofs.\n"
                        f"Contact: @dtxzahid"
                    )
                else:
                    await update.message.reply_text(
                        f"❌ Saare Attempts Khatam\n\n"
                        f"Ab aap is app ke liye screenshot nahi bhej sakte.\n\n"
                        f"Dusra app try karo ya contact: @dtxzahid",
                        reply_markup=MAIN_KEYBOARD
                    )
                context.user_data["awaiting_proof"] = False
            return

        # VERIFIED
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
            f"Aapka proof accept ho gaya.\n\n"
            f"⚠️ Note: Fake screenshots se account ban ho sakta hai.",
            reply_markup=MAIN_KEYBOARD
        )

    except Exception as e:
        print(f"Verification error: {e}")
        await update.message.reply_text("❌ Proof verify nahi hua. Dobara try karo.", reply_markup=MAIN_KEYBOARD)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    context.user_data["awaiting_proof"] = False

# ═══════════════════════════════════════════════════
#   CANCEL
# ═══════════════════════════════════════════════════

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancel kar diya.", reply_markup=MAIN_KEYBOARD)
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_proof_photo))

    print("🤖 Bot is running (polling)...")
    app.run_polling(drop_pending_updates=True)
