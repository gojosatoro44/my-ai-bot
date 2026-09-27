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
#   AI TASK BOT — Clean Edition
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
    print("✅ Database connected successfully!")
except Exception as e:
    print(f"❌ Database connection error: {e}")
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

# --- Keyboards ---
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

# --- States ---
(ADMIN_MENU, ADD_APP, ADD_COMMENTS, BROADCAST_MSG, ADMIN_BAL_ID, ADMIN_BAL_AMT,
 WITHDRAW_METHOD, WITHDRAW_UPI, WITHDRAW_AMT) = range(9)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

SEP = "──────────────────────"

# --- Error Handler (plain text to avoid parsing issues) ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception while handling an update:", exc_info=context.error)
    if ADMIN_ID != 0:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Bot Error:\n\n{context.error}"
            )
        except Exception:
            pass

# --- /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    context.user_data["proof_app"] = None
    name = update.effective_user.first_name or "there"
    text = (
        f"👋 *Hey {name}!*\n"
        f"{SEP}\n\n"
        f"Welcome to the Task Bot.\n"
        f"Complete simple tasks and earn rewards.\n\n"
        f"*Get started by selecting an option below.*"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

# ═══════════════════════════════════════════════════
#   ADMIN PANEL
# ═══════════════════════════════════════════════════

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return ConversationHandler.END
    text = f"🛠️ *Admin Panel*\n{SEP}\n\nWhat would you like to do?"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def add_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 *Enter the App Name:*", parse_mode="Markdown")
    return ADD_APP

async def add_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["temp_app_name"] = update.message.text.strip()
    text = (
        f"✅ App saved: *{context.user_data['temp_app_name']}*\n\n"
        f"Now send the comments separated by commas.\n"
        f"Example: `Nice app,Good UI,Love it`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return ADD_COMMENTS

async def add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comments_list = [c.strip() for c in update.message.text.split(",") if c.strip()]
    app_name = context.user_data.get("temp_app_name")
    data = load_data()
    if app_name not in data["apps"]: data["apps"][app_name] = []
    data["apps"][app_name].extend(comments_list)
    save_data(data)
    text = (
        f"✅ *Comments saved successfully*\n"
        f"{SEP}\n"
        f"📱 App: *{app_name}*\n"
        f"💬 Comments added: *{len(comments_list)}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# --- Saved Proofs (FIX #2: plain text to avoid Markdown errors) ---
async def show_saved_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    proofs = data.get("saved_proofs", [])
    if not proofs:
        await update.message.reply_text("📭 No saved proofs yet.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = f"📋 Saved Proofs — Last 10\n{SEP}\n\n"
    for i, p in enumerate(proofs[-10:], 1):
        status_emoji = {"approved": "✅", "rejected": "❌", "pending": "⏳"}.get(p["status"], "❓")
        text += (
            f"{status_emoji} {i}. {p['app_name']}\n"
            f"   👤 Reviewer: {p['reviewer_name']}\n"
            f"   📱 User: @{p.get('username', 'N/A')}\n"
            f"   🆔 ID: {p['user_id']}\n\n"
        )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# --- Stats (FIX #2: plain text to avoid Markdown errors) ---
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
        f"📊 Bot Statistics\n"
        f"{SEP}\n\n"
        f"👥 Total Users: {len(users)}\n"
        f"📝 Total Proofs: {len(proofs)}\n\n"
        f"✅ Approved: {approved}\n"
        f"❌ Rejected: {rejected}\n"
        f"⏳ Pending: {pending}\n\n"
        f"{SEP}\n"
        f"💰 Active Balance: ₹{total_balance:.2f}\n"
        f"💸 Total Withdrawn: ₹{total_withdrawn:.2f}"
    )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 *Send the message to broadcast:*", parse_mode="Markdown")
    return BROADCAST_MSG

async def broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    data = load_data()
    users = list(data.get("users", {}).keys())
    await update.message.reply_text(f"⏳ Sending to {len(users)} users...")
    count = 0
    final_msg = f"📢 Announcement\n{SEP}\n\n{msg}"
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=int(user_id), text=final_msg)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Delivered to {count} users.", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def admin_bal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖️ *Send the User ID:*", parse_mode="Markdown")
    return ADMIN_BAL_ID

async def admin_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bal_user_id"] = update.message.text.strip()
    await update.message.reply_text("💵 *Send amount (e.g., 50 to add, -50 to remove):*", parse_mode="Markdown")
    return ADMIN_BAL_AMT

async def admin_bal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    user_id = context.user_data.get("bal_user_id")
    data = load_data()
    if user_id not in data["users"]:
        data["users"][user_id] = {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0, "pending": 0}
    data["users"][user_id]["balance"] += amount
    save_data(data)
    action = "added to" if amount > 0 else "removed from"
    text = (
        f"✅ Balance Updated\n"
        f"{SEP}\n"
        f"👤 User ID: {user_id}\n"
        f"💵 ₹{abs(amount):.2f} {action} balance\n"
        f"💰 New Balance: ₹{data['users'][user_id]['balance']:.2f}"
    )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# ═══════════════════════════════════════════════════
#   USER PROFILE
# ═══════════════════════════════════════════════════

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name or "User"
    data = load_data()
    user = data["users"].get(user_id, {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0, "pending": 0})
    text = (
        f"👤 My Profile\n"
        f"{SEP}\n\n"
        f"Name: {user_name}\n"
        f"ID: {user_id}\n"
        f"Balance: ₹{user.get('balance', 0.0):.2f}\n\n"
        f"{SEP}\n"
        f"📊 Task Summary\n"
        f"Total Tasks: {user.get('total_tasks', 0)}\n"
        f"✅ Accepted: {user.get('accepted', 0)}\n"
        f"❌ Rejected: {user.get('rejected', 0)}\n"
        f"⏳ Pending: {user.get('pending', 0)}"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

# ═══════════════════════════════════════════════════
#   WITHDRAWAL SYSTEM (FIX #1: Approve/Reject buttons)
# ═══════════════════════════════════════════════════

async def withdrawal_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    user = data["users"].get(user_id, {"balance": 0.0})
    balance = user.get("balance", 0.0)
    if balance < 10:
        text = (
            f"💰 Withdrawal\n"
            f"{SEP}\n\n"
            f"❌ Minimum withdrawal is ₹10.\n"
            f"💵 Your balance: ₹{balance:.2f}"
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    text = (
        f"💰 Withdrawal\n"
        f"{SEP}\n\n"
        f"Available Balance: ₹{balance:.2f}\n\n"
        f"Choose your withdrawal method:"
    )
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
        await query.edit_message_text("💳 *Please send your UPI ID:*", parse_mode="Markdown")
        return WITHDRAW_UPI
    elif query.data == "withdraw_vsv":
        text = (
            f"🎗️ VSV Withdrawal\n"
            f"{SEP}\n\n"
            f"Please message the owner directly:\n"
            f"👉 @dtxzahid"
        )
        await query.edit_message_text(text)
        return ConversationHandler.END

async def withdraw_upi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["withdraw_upi"] = update.message.text.strip()
    await update.message.reply_text("💵 *Now send the amount to withdraw (Min ₹10):*", parse_mode="Markdown")
    return WITHDRAW_AMT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    if amount < 10:
        await update.message.reply_text("❌ Minimum withdrawal is ₹10.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    user_id = str(update.effective_user.id)
    data = load_data()
    user = data["users"].get(user_id, {"balance": 0.0})
    if amount > user.get("balance", 0.0):
        await update.message.reply_text("❌ Insufficient balance.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    data["users"][user_id]["balance"] -= amount
    upi_id = context.user_data.get("withdraw_upi")
    data["withdrawals"].append({
        "user_id": user_id,
        "username": update.effective_user.username or "N/A",
        "amount": amount,
        "method": "UPI",
        "upi_id": upi_id,
        "status": "pending"
    })
    save_data(data)

    # FIX #1: Send with Approve/Reject buttons
    admin_text = (
        f"💰 New Withdrawal Request\n"
        f"{SEP}\n\n"
        f"👤 User: @{update.effective_user.username or 'N/A'}\n"
        f"🆔 ID: {user_id}\n"
        f"💵 Amount: ₹{amount:.2f}\n"
        f"🏦 Method: UPI\n"
        f"📧 UPI ID: {upi_id}"
    )
    admin_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"wdstatus_approve_{user_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"wdstatus_reject_{user_id}")
    ]])
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, reply_markup=admin_keyboard)
    except Exception as e:
        print(f"Error: {e}")

    text = (
        f"✅ Withdrawal Request Submitted\n"
        f"{SEP}\n\n"
        f"Your request is pending approval.\n"
        f"You will be notified once processed."
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# --- Withdrawal Approve/Reject handler ---
async def withdrawal_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("Only the admin can do this.", show_alert=True)
        return
    await query.answer()

    parts = query.data.split("_")
    action = parts[1]
    user_id = parts[2]

    data = load_data()
    # Find the latest pending withdrawal for this user
    target = None
    for w in reversed(data["withdrawals"]):
        if w["user_id"] == user_id and w["status"] == "pending":
            target = w
            break

    if not target:
        await query.edit_message_text(text=f"⚠️ No pending withdrawal found for user {user_id}.")
        return

    if action == "approve":
        target["status"] = "approved"
        save_data(data)
        user_msg = (
            f"✅ Withdrawal Approved\n"
            f"{SEP}\n\n"
            f"Amount: ₹{target['amount']:.2f}\n"
            f"Method: {target['method']}\n\n"
            f"Your payment has been processed."
        )
        try:
            await context.bot.send_message(chat_id=int(user_id), text=user_msg, reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            print(f"Error: {e}")
        await query.edit_message_text(text=f"✅ Withdrawal of ₹{target['amount']:.2f} for user {user_id} has been APPROVED.")

    elif action == "reject":
        target["status"] = "rejected"
        # Refund the amount back to user balance
        if user_id in data["users"]:
            data["users"][user_id]["balance"] += target["amount"]
        save_data(data)
        user_msg = (
            f"❌ Withdrawal Rejected\n"
            f"{SEP}\n\n"
            f"Amount: ₹{target['amount']:.2f} has been refunded to your balance.\n\n"
            f"Contact @dtxzahid if you have any questions."
        )
        try:
            await context.bot.send_message(chat_id=int(user_id), text=user_msg, reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            print(f"Error: {e}")
        await query.edit_message_text(text=f"❌ Withdrawal of ₹{target['amount']:.2f} for user {user_id} has been REJECTED. Amount refunded.")

# ═══════════════════════════════════════════════════
#   GET COMMENT
# ═══════════════════════════════════════════════════

async def get_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = list(data.get("apps", {}).keys())
    if not apps:
        await update.message.reply_text("📭 No tasks available right now.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(f"📱 {app}", callback_data=f"getapp_{app}")] for app in apps]
    text = f"📋 Available Apps\n{SEP}\n\nSelect an app to receive a comment:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def user_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_name = query.data.replace("getapp_", "")
    user_id = str(update.effective_user.id)
    data = load_data()
    if app_name not in data["apps"] or not data["apps"][app_name]:
        await query.edit_message_text("📭 No comments found for this app.")
        return
    if user_id not in data["history"]: data["history"][user_id] = {}

    # FIX #3: Updated message for already-received comment
    if app_name in data["history"][user_id] and len(data["history"][user_id][app_name]) >= 1:
        await query.edit_message_text(
            "⚠️ You have already received a comment for this app.\n\n"
            "If you want more comments then contact admin @DTXZAHID"
        )
        return

    available = [c for c in data["apps"][app_name] if c not in data["history"][user_id].get(app_name, [])]
    if not available:
        await query.edit_message_text("📭 No more unique comments available.")
        return

    chosen = random.choice(available)
    data["history"][user_id][app_name] = [chosen]
    save_data(data)

    comment_text = (
        f"💬 Your Comment\n"
        f"{SEP}\n\n"
        f"`{chosen}`\n\n"
        f"Tap the comment above to copy it."
    )
    await query.edit_message_text(comment_text, parse_mode="Markdown")
    await context.bot.send_message(
        chat_id=user_id,
        text="📸 Please share a screenshot of your review."
    )
    context.user_data["awaiting_proof"] = True
    context.user_data["proof_app"] = app_name

# ═══════════════════════════════════════════════════
#   PROOF HANDLING
# ═══════════════════════════════════════════════════

async def handle_proof_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        await update.message.reply_text("⚠️ Please send a screenshot (image), not text.")
        return

async def handle_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        photo_file = await update.message.photo[-1].get_file()
        app_name = context.user_data.get("proof_app", "Unknown")
        user = update.effective_user
        user_id = str(user.id)
        data = load_data()

        if user_id in data["proofs"] and app_name in data["proofs"][user_id]:
            await update.message.reply_text(
                "❌ You have already submitted a proof for this app.\n\n"
                "If you uploaded the wrong screenshot, contact: @dtxzahid",
                reply_markup=MAIN_KEYBOARD
            )
            context.user_data["awaiting_proof"] = False
            return

        # FIX #4: New "checking" message
        await update.message.reply_text(
            "⏳ Proof is being checked by our admin. Please wait for 5-10 seconds..."
        )

        file_path = "temp_proof.jpg"
        await photo_file.download_to_drive(file_path)
        try:
            with open(file_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode("utf-8")
            chosen_comment = data["history"][user_id][app_name][0]

            # FIX #6: New verification logic — comment match is what matters
            prompt = f'Analyze this screenshot. 1. Does it contain this exact comment: "{chosen_comment}"? (comment_match) 2. Does it show a review for the app "{app_name}"? (app_match) 3. What is the reviewer name? Reply STRICTLY as JSON with no markdown: {{"comment_match": true, "app_match": true, "reviewer_name": "name"}}'

            completion = client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
                temperature=0.1, max_completion_tokens=256,
            )
            response_text = completion.choices[0].message.content
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            result = json.loads(response_text)

            # New logic: verified if comment matches (regardless of app)
            comment_matched = result.get("comment_match", False)

            if not comment_matched:
                # FIX #5: New rejection message (no mention of AI)
                await update.message.reply_text(
                    f"❌ The proof is fake or invalid so it's rejected.\n\n"
                    f"If you think the proof is real then you can contact admin @DTXZAHID",
                    reply_markup=MAIN_KEYBOARD
                )
                context.user_data["awaiting_proof"] = False
                return

            # Verified — proceed to save
            reviewer_name = result.get("reviewer_name", "Unknown")
            if user_id not in data["users"]:
                data["users"][user_id] = {"balance": 0.0, "total_tasks": 0, "accepted": 0, "rejected": 0, "pending": 0}
            data["users"][user_id]["total_tasks"] += 1
            data["users"][user_id]["pending"] += 1
            data["proofs"][user_id] = {app_name: True}
            data["saved_proofs"].append({
                "user_id": user_id,
                "username": user.username or "N/A",
                "app_name": app_name,
                "reviewer_name": reviewer_name,
                "status": "pending"
            })
            save_data(data)

            admin_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"proofstatus_approve_{user_id}_{app_name}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"proofstatus_reject_{user_id}_{app_name}")
            ]])
            try:
                await context.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=photo_file.file_id,
                    caption=(
                        f"📥 New Proof Submitted\n"
                        f"{SEP}\n"
                        f"👤 User: @{user.username} ({user.id})\n"
                        f"📱 App: {app_name}\n"
                        f"🔍 Reviewer: {reviewer_name}"
                    ),
                    reply_markup=admin_keyboard
                )
            except Exception as e:
                print(f"Error sending proof to admin: {e}")

            text = (
                f"✅ Proof Submitted Successfully\n"
                f"{SEP}\n\n"
                f"Your proof has been sent for verification.\n"
                f"You'll be notified once reviewed.\n\n"
                f"⚠️ Note: Fake or meaningless screenshots may lead to account suspension."
            )
            await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            print(f"Verification Error: {e}")
            await update.message.reply_text(
                "❌ Error verifying proof. Please try again later.",
                reply_markup=MAIN_KEYBOARD
            )
        finally:
            if os.path.exists(file_path): os.remove(file_path)
        context.user_data["awaiting_proof"] = False

# ═══════════════════════════════════════════════════
#   ADMIN PROOF REVIEW
# ═══════════════════════════════════════════════════

async def proof_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("Only the admin can do this.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split("_")
    action, user_id, app_name = parts[1], parts[2], parts[3]

    data = load_data()
    for p in data["saved_proofs"]:
        if p["user_id"] == user_id and p["app_name"] == app_name and p["status"] == "pending":
            p["status"] = "approved" if action == "approve" else "rejected"
            break

    if user_id in data["users"]:
        data["users"][user_id]["pending"] -= 1
        if action == "approve":
            data["users"][user_id]["accepted"] += 1
            data["users"][user_id]["balance"] += 10.0
        else:
            data["users"][user_id]["rejected"] += 1
    save_data(data)

    if action == "approve":
        user_msg = (
            f"🎉 Proof Approved\n"
            f"{SEP}\n\n"
            f"Your proof for {app_name} has been approved.\n"
            f"💰 ₹10 has been added to your balance."
        )
        await context.bot.send_message(chat_id=int(user_id), text=user_msg, reply_markup=MAIN_KEYBOARD)
        await query.edit_message_caption(caption=f"✅ Approved — {app_name}")
    elif action == "reject":
        user_msg = (
            f"❌ Proof Rejected\n"
            f"{SEP}\n\n"
            f"Your proof for {app_name} was not accepted.\n\n"
            f"Contact @dtxzahid if you have any questions."
        )
        await context.bot.send_message(chat_id=int(user_id), text=user_msg, reply_markup=MAIN_KEYBOARD)
        await query.edit_message_caption(caption=f"❌ Rejected — {app_name}")

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
            MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry),
            MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs),
            MessageHandler(filters.Regex('^Stats$'), show_stats),
            MessageHandler(filters.Regex('^Broadcast$'), broadcast_entry),
            MessageHandler(filters.Regex('^Add/Remove Bal$'), admin_bal_entry),
            MessageHandler(filters.Regex('^Withdrawal$'), withdrawal_entry),
        ],
        states={
            ADMIN_MENU: [
                MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry),
                MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs),
                MessageHandler(filters.Regex('^Stats$'), show_stats),
                MessageHandler(filters.Regex('^Broadcast$'), broadcast_entry),
                MessageHandler(filters.Regex('^Add/Remove Bal$'), admin_bal_entry),
            ],
            ADD_APP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app)],
            ADD_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_comments)],
            BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_msg)],
            ADMIN_BAL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bal_user)],
            ADMIN_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bal_amount)],
            WITHDRAW_METHOD: [CallbackQueryHandler(withdraw_method_callback, pattern="^withdraw_")],
            WITHDRAW_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi_id)],
            WITHDRAW_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('DTX', admin_entry),
            MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry),
            MessageHandler(filters.Regex('^My Profile$'), show_profile),
            MessageHandler(filters.Regex('^Withdrawal$'), withdrawal_entry),
            MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs),
            MessageHandler(filters.Regex('^Stats$'), show_stats),
            MessageHandler(filters.Regex('^Broadcast$'), broadcast_entry),
            MessageHandler(filters.Regex('^Add/Remove Bal$'), admin_bal_entry),
            MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry),
        ],
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(user_app_callback, pattern="^getapp_"))
    app.add_handler(CallbackQueryHandler(proof_status_callback, pattern="^proofstatus_"))
    app.add_handler(CallbackQueryHandler(withdrawal_status_callback, pattern="^wdstatus_"))
    app.add_handler(MessageHandler(filters.Regex('^My Profile$'), show_profile))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_proof_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_proof_photo))

    print("🤖 Bot is now polling Telegram for messages...")
    print("✨ Bot is live and listening.")
    app.run_polling(drop_pending_updates=True)
