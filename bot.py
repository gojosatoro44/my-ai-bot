import logging
import os
import json
import random
import base64
import asyncio
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, ConversationHandler, CallbackQueryHandler
from groq import Groq
from pymongo import MongoClient

# ═══════════════════════════════════════════════════
# 👑  ROYAL BOT — LONG POLLING EDITION
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

# ─── MongoDB Realm ───
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    print("👑 MongoDB Kingdom Connected Successfully!")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    raise e

db = mongo_client["telegram_bot"]
collection = db["data"]

def load_data():
    try:
        doc = collection.find_one({"_id": "config"})
        if not doc:
            return {"apps": {}, "history": {}, "proofs": {}, "saved_proofs": [], "users": {}, "withdrawals": []}
        doc.pop("_id", None)
        if "users" not in doc: doc["users"] = {}
        if "withdrawals" not in doc: doc["withdrawals"] = []
        if "saved_proofs" not in doc: doc["saved_proofs"] = []
        return doc
    except Exception as e:
        print(f"Error loading data: {e}")
        return {"apps": {}, "history": {}, "proofs": {}, "saved_proofs": [], "users": {}, "withdrawals": []}

def save_data(data):
    try:
        data["_id"] = "config"
        collection.replace_one({"_id": "config"}, data, upsert=True)
    except Exception as e:
        print(f"Error saving data: {e}")

client = Groq(api_key=GROQ_API_KEY)
VISION_MODEL = "qwen/qwen3.8-27b"

# ─── Royal Keyboards ───
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["Get Comment", "My Profile"], ["Withdrawal"]],
    resize_keyboard=True
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Add Comment", "Saved Proof"],
        ["Stats", "Broadcast"],
        ["Add/Remove Bal"]
    ],
    resize_keyboard=True
)

# ─── States ───
(ADMIN_MENU, ADD_APP, ADD_COMMENTS, BROADCAST_MSG, ADMIN_BAL_ID, ADMIN_BAL_AMT,
 WITHDRAW_METHOD, WITHDRAW_UPI, WITHDRAW_AMT) = range(9)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

LINE = "━━━━━━━━━━━━━━━━━━━━━━"
SMALL_LINE = "━━━━━━━━━━━━━"

# ─── Error Handler ───
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception while handling an update:", exc_info=context.error)
    if ADMIN_ID != 0:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ *Royal Alert*\n{SMALL_LINE}\n`{context.error}`", parse_mode="Markdown")
        except Exception:
            pass

# ─── /start ───
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    context.user_data["proof_app"] = None
    name = update.effective_user.first_name or "Honored Guest"
    text = (
        f"👑 *Welcome, {name}*\n"
        f"{LINE}\n\n"
        f"✨ You have entered the *Royal Task Chamber*.\n"
        f"💎 Complete tasks elegantly. Earn rewards gracefully.\n\n"
        f"🎯 *Use the Royal Menu below to begin your journey.*"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

# ═══════════════════════════════════════════════════
# 👑  ROYAL ADMIN CHAMBER
# ═══════════════════════════════════════════════════

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Access Denied* — Reserved for the Crown.", parse_mode="Markdown")
        return ConversationHandler.END
    text = f"👑 *Royal Admin Chamber*\n{LINE}\n\nWelcome back, Your Majesty. 🎩"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def add_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏷️ *Please enter the App Name:*", parse_mode="Markdown")
    return ADD_APP

async def add_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_app_name"] = update.message.text.strip()
    text = f"✅ App Registered: *{context.user_data['temp_app_name']}*\n{SMALL_LINE}\n✍️ Now send the comments, separated by commas."
    await update.message.reply_text(text, parse_mode="Markdown")
    return ADD_COMMENTS

async def add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comments_list = [c.strip() for c in update.message.text.split(",") if c.strip()]
    app_name = context.user_data.get("temp_app_name")
    data = load_data()
    if app_name not in data["apps"]: data["apps"][app_name] = []
    data["apps"][app_name].extend(comments_list)
    save_data(data)
    text = f"💎 *Comments Enshrined*\n{SMALL_LINE}\n📱 App: *{app_name}*\n📝 Added: *{len(comments_list)}*"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

async def show_saved_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    proofs = data.get("saved_proofs", [])
    if not proofs:
        await update.message.reply_text("📜 *The Royal Ledger is empty.*", parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = f"📜 *Royal Proof Ledger* — Last 10\n{LINE}\n\n"
    for i, p in enumerate(proofs[-10:], 1):
        status_emoji = {"approved": "✅", "rejected": "❌", "pending": "⏳"}.get(p["status"], "❓")
        text += f"{status_emoji} *{i}. {p['app_name']}*\n   👤 {p['reviewer_name']}\n   📱 @{p.get('username', 'N/A')}\n   🆔 `{p['user_id']}`\n\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    users = data.get("users", {})
    proofs = data.get("saved_proofs", [])
    approved = sum(1 for p in proofs if p["status"] == "approved")
    rejected = sum(1 for p in proofs if p["status"] == "rejected")
    pending = sum(1 for p in proofs if p["status"] == "pending")
    total_balance = sum(u.get("balance", 0) for u in users.values())
    total_withdrawn = sum(w["amount"] for w in data.get("withdrawals", []) if w.get("status") == "approved")
    text = (
        f"📊 *Royal Kingdom Statistics*\n{LINE}\n\n"
        f"👥 *Subjects:* {len(users)}\n"
        f"📝 *Total Proofs:* {len(proofs)}\n\n"
        f"✅ *Approved:* {approved}\n"
        f"❌ *Rejected:* {rejected}\n"
        f"⏳ *Pending:* {pending}\n\n"
        f"{SMALL_LINE}\n"
        f"💰 *Active Balance:* ₹{total_balance:.2f}\n"
        f"💸 *Withdrawn:* ₹{total_withdrawn:.2f}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 *Send the royal decree to broadcast:*", parse_mode="Markdown")
    return BROADCAST_MSG

async def broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    data = load_data()
    users = list(data.get("users", {}).keys())
    await update.message.reply_text(f"⏳ *Dispatching to {len(users)} subjects...*", parse_mode="Markdown")
    count = 0
    royal_msg = f"📢 *Royal Announcement*\n{LINE}\n\n{msg}"
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=int(user_id), text=royal_msg, parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ *Delivered to {count} subjects.*", parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def admin_bal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖️ *Send the User ID to add/remove balance:*", parse_mode="Markdown")
    return ADMIN_BAL_ID

async def admin_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bal_user_id"] = update.message.text.strip()
    await update.message.reply_text("💵 *Send amount (e.g., 50 to add, -50 to remove):*", parse_mode="Markdown")
    return ADMIN_BAL_AMT

async def admin_bal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ *Invalid amount.*", parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    user_id = context.user_data.get("bal_user_id")
    data = load_data()
    if user_id not in data["users"]:
        data["users"][user_id] = {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0, "pending": 0}
    data["users"][user_id]["balance"] += amount
    save_data(data)
    action = "Added to" if amount > 0 else "Removed from"
    text = f"⚖️ *Royal Treasury Updated*\n{SMALL_LINE}\n👤 `{user_id}`\n💵 ₹{abs(amount):.2f} {action} balance\n💰 *New Balance:* ₹{data['users'][user_id]['balance']:.2f}"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# ═══════════════════════════════════════════════════
# 👤  ROYAL PROFILE
# ═══════════════════════════════════════════════════

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name or "Honored Guest"
    data = load_data()
    user = data["users"].get(user_id, {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0, "pending": 0})
    text = (
        f"👤 *Royal Profile*\n{LINE}\n\n"
        f"🎩 *Name:* {user_name}\n"
        f"🆔 *ID:* `{user_id}`\n"
        f"💰 *Balance:* ₹{user.get('balance', 0.0):.2f}\n\n"
        f"{SMALL_LINE}\n"
        f"📊 *Task Chronicles*\n"
        f"🎯 Total: *{user.get('total_tasks', 0)}*\n"
        f"✅ Accepted: *{user.get('accepted', 0)}*\n"
        f"❌ Rejected: *{user.get('rejected', 0)}*\n"
        f"⏳ Pending: *{user.get('pending', 0)}*\n"
        f"{SMALL_LINE}\n\n"
        f"✨ _Continue thy noble work._"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

# ═══════════════════════════════════════════════════
# 💰  ROYAL WITHDRAWAL
# ═══════════════════════════════════════════════════

async def withdrawal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    user = data["users"].get(user_id, {"balance": 0.0})
    balance = user.get("balance", 0.0)
    if balance < 10:
        text = f"💰 *Royal Treasury*\n{SMALL_LINE}\n❌ Minimum withdrawal is ₹10.\n💵 Balance: ₹{balance:.2f}"
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    text = f"💰 *Royal Treasury*\n{SMALL_LINE}\n💵 Available: ₹{balance:.2f}\n\n🏦 *Choose method:*"
    keyboard = [[InlineKeyboardButton("💳 UPI", callback_data="withdraw_upi"), InlineKeyboardButton("🎗️ VSV", callback_data="withdraw_vsv")]]
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return WITHDRAW_METHOD

async def withdraw_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "withdraw_upi":
        await query.edit_message_text("💳 *Please send your UPI ID:*", parse_mode="Markdown")
        return WITHDRAW_UPI
    elif query.data == "withdraw_vsv":
        text = f"🎗️ *VSV Withdrawal*\n{SMALL_LINE}\nKindly message the Royal Owner:\n👑 @dtxzahid"
        await query.edit_message_text(text, parse_mode="Markdown")
        return ConversationHandler.END

async def withdraw_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["withdraw_upi"] = update.message.text.strip()
    await update.message.reply_text("💵 *Now send the amount (Min ₹10):*", parse_mode="Markdown")
    return WITHDRAW_AMT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ *Invalid amount.*", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    if amount < 10:
        await update.message.reply_text("❌ *Minimum is ₹10.*", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    user_id = str(update.effective_user.id)
    data = load_data()
    user = data["users"].get(user_id, {"balance": 0.0})
    if amount > user.get("balance", 0.0):
        await update.message.reply_text("❌ *Insufficient balance.*", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    data["users"][user_id]["balance"] -= amount
    upi_id = context.user_data.get("withdraw_upi")
    data["withdrawals"].append({"user_id": user_id, "username": update.effective_user.username or "N/A", "amount": amount, "method": "UPI", "upi_id": upi_id, "status": "pending"})
    save_data(data)
    admin_text = f"💰 *Royal Withdrawal Request*\n{LINE}\n\n👤 @{update.effective_user.username or 'N/A'}\n🆔 `{user_id}`\n💵 ₹{amount:.2f}\n🏦 UPI\n📧 `{upi_id}`"
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="Markdown")
    except Exception as e:
        print(f"Error: {e}")
    await update.message.reply_text(f"✅ *Withdrawal Submitted*\n{SMALL_LINE}\nAwaiting royal approval. ✨", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# ═══════════════════════════════════════════════════
# 💎  ROYAL TASK CHAMBER
# ═══════════════════════════════════════════════════

async def get_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = list(data.get("apps", {}).keys())
    if not apps:
        await update.message.reply_text("📭 *No tasks available.*", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"💎 {app}", callback_data=f"getapp_{app}")] for app in apps]
    text = f"💎 *Royal Task Chamber*\n{SMALL_LINE}\n\n✨ _Select an app:_"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def user_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_name = query.data.replace("getapp_", "")
    user_id = str(update.effective_user.id)
    data = load_data()
    if app_name not in data["apps"] or not data["apps"][app_name]:
        await query.edit_message_text("📭 *No comments found.*", parse_mode="Markdown")
        return
    if user_id not in data["history"]: data["history"][user_id] = {}
    if app_name in data["history"][user_id] and len(data["history"][user_id][app_name]) >= 1:
        await query.edit_message_text("👑 *Royal Notice*\n━━━━━━━━━━━━━\n\nYou already received a comment for this app.\n✨ _One comment per app, per subject._", parse_mode="Markdown")
        return
    available = [c for c in data["apps"][app_name] if c not in data["history"][user_id].get(app_name, [])]
    if not available:
        await query.edit_message_text("📭 *No more unique comments.*", parse_mode="Markdown")
        return
    chosen = random.choice(available)
    data["history"][user_id][app_name] = [chosen]
    save_data(data)
    comment_text = f"💎 *Your Royal Comment*\n{LINE}\n\n`{chosen}`\n\n{SMALL_LINE}\n✨ _Tap to copy._"
    await query.edit_message_text(comment_text, parse_mode="Markdown")
    await context.bot.send_message(chat_id=user_id, text=f"📸 *Please share a screenshot of your review.*\n{SMALL_LINE}\n👑 _Awaiting thy proof._", parse_mode="Markdown")
    context.user_data["awaiting_proof"] = True
    context.user_data["proof_app"] = app_name

# ═══════════════════════════════════════════════════
# 📸  PROOF HANDLING
# ═══════════════════════════════════════════════════

async def handle_proof_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        await update.message.reply_text("⚠️ *Only screenshots are accepted.*\n📸 _Please send the proof image._", parse_mode="Markdown")
        return

async def handle_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        photo_file = await update.message.photo[-1].get_file()
        app_name = context.user_data.get("proof_app", "Unknown")
        user = update.effective_user
        user_id = str(user.id)
        data = load_data()
        if user_id in data["proofs"] and app_name in data["proofs"][user_id]:
            await update.message.reply_text("❌ *Proof already submitted.*\n━━━━━━━━━━━━━\nContact: 👑 @dtxzahid", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
            context.user_data["awaiting_proof"] = False
            return
        await update.message.reply_text("⏳ *The Royal Eye is verifying...*\n✨ _A moment please._", parse_mode="Markdown")
        file_path = "temp_proof.jpg"
        await photo_file.download_to_drive(file_path)
        try:
            with open(file_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode("utf-8")
            chosen_comment = data["history"][user_id][app_name][0]
            prompt = f'Analyze screenshot. 1. Does it show a review for "{app_name}"? 2. Does it match "{chosen_comment}"? 3. Reviewer name? Reply STRICTLY as JSON: {{"app_match": true, "comment_match": true, "reviewer_name": "name"}}'
            completion = client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
                temperature=0.1, max_completion_tokens=256,
            )
            response_text = completion.choices[0].message.content
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            result = json.loads(response_text)
            if not result.get("app_match") or not result.get("comment_match"):
                await update.message.reply_text("❌ *Rejected by Royal Eye*\n━━━━━━━━━━━━━\nApp or comment doesn't match.\nContact: 👑 @dtxzahid", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
                context.user_data["awaiting_proof"] = False
                return
            reviewer_name = result.get("reviewer_name", "Unknown")
            if user_id not in data["users"]:
                data["users"][user_id] = {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0, "pending": 0}
            data["users"][user_id]["total_tasks"] += 1
            data["users"][user_id]["pending"] += 1
            data["proofs"][user_id] = {app_name: True}
            data["saved_proofs"].append({"user_id": user_id, "username": user.username or "N/A", "app_name": app_name, "reviewer_name": 
