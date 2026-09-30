import os
import time
import json
import random
import base64
import logging
import asyncio
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, KeyboardButton, Bot
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telegram.constants import ParseMode, ChatMemberStatus

from pymongo import MongoClient
from groq import Groq
from PIL import Image

# ═══════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════
SEP = "──────────────────────"
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MONGODB_URI = os.environ.get("MONGODB_URI", "")

DEFAULT_DATA = {
    "apps": {},
    "history": {},
    "proofs": {},
    "saved_proofs": [],
    "users": {},
    "withdrawals": [],
    "banned_users": [],
    "rejections": [],
    "used_screenshots": [],
    "pending_proofs": {},
    "force_join_channel": None,
    "attempts": {},
    "comment_usage": {},
    "last_request": {},
    "auto_ban": {}
}

# Conversation States
(
    AWAITING_PROOF, ADMIN_MENU, ADD_APP, ADD_COMMENTS, BROADCAST_MSG, ADMIN_BAL_ID, ADMIN_BAL_AMT,
    WITHDRAW_METHOD, WITHDRAW_UPI, WITHDRAW_AMT,
    FORCE_JOIN_SET, BAN_USER_ID, APP_ADD_COMMENTS
) = range(13)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# DATABASE SETUP
# ═══════════════════════════════════════════════════
mongo_client = None
db = None
collection = None

def connect_db():
    global mongo_client, db, collection
    mongo_client = MongoClient(MONGODB_URI)
    try:
        mongo_client.admin.command('ping')
        logging.info("✅ Database connected.")
    except Exception as e:
        logging.error(f"❌ DB error: {e}")
        raise
    db = mongo_client["telegram_bot"]
    collection = db["data"]

def load_data():
    try:
        doc = collection.find_one({"_id": "config"})
        if not doc:
            data = DEFAULT_DATA.copy()
            save_data(data)
            return data
        for key in DEFAULT_DATA:
            if key not in doc:
                doc[key] = DEFAULT_DATA[key]
        return doc
    except Exception as e:
        logging.error(f"Error loading data: {e}")
        return DEFAULT_DATA.copy()

def save_data(data):
    try:
        collection.update_one({"_id": "config"}, {"$set": data}, upsert=True)
    except Exception as e:
        logging.error(f"Error saving data: {e}")

# ═══════════════════════════════════════════════════
# GROQ AI VISION
# ═══════════════════════════════════════════════════
groq_client = Groq(api_key=GROQ_API_KEY)

async def analyze_screenshot(image_path, comment, app_name):
    with open(image_path, "rb") as f:
        image_data = f.read()
        
    base64_image = base64.b64encode(image_data).decode('utf-8')
    
    prompt = f'''Analyze this screenshot. 1. Does it contain this exact comment: "{comment}"? (comment_match) 2. Does it show a review for the app "{app_name}"? (app_match) 3. What is the reviewer name? Reply STRICTLY as JSON with no markdown: {{"comment_match": true, "app_match": true, "reviewer_name": "name"}}'''
    
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                        },
                    ],
                }
            ],
            model="qwen/qwen3.8-27b",
            temperature=0.1,
            max_tokens=500,
        )
        response_text = chat_completion.choices[0].message.content
        response_text = response_text.strip().strip('`').strip()
        if response_text.startswith('json'):
            response_text = response_text[4:].strip()
        return json.loads(response_text)
    except Exception as e:
        logging.error(f"Groq API Error: {e}")
        return None

# ═══════════════════════════════════════════════════
# USER HANDLERS
# ═══════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    context.user_data["current_app"] = None
    
    uid = str(update.effective_user.id)
    data = load_data()
    
    if uid in data.get("banned_users", []):
        await update.message.reply_text(f"🚫 Aap ban ho chuke ho. Contact: @dtxzahid")
        return ConversationHandler.END
        
    ban_info = data.get("auto_ban", {}).get(uid)
    if ban_info and ban_info.get("banned_until", 0) > time.time():
        await update.message.reply_text(f"🚫 Aap ban ho chuke ho. Contact: @dtxzahid")
        return ConversationHandler.END
    
    force_join = data.get("force_join_channel")
    if force_join:
        try:
            member = await context.bot.get_chat_member(force_join, uid)
            if member.status not in ["member", "administrator", "creator"]:
                keyboard = [
                    [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{force_join.lstrip('@')}")],
                    [InlineKeyboardButton("✅ I Joined", callback_data="check_join")]
                ]
                await update.message.reply_text(f"👋 Welcome {update.effective_user.first_name}!\n\nIs bot ko use karne ke liye pehle channel join karo.", reply_markup=InlineKeyboardMarkup(keyboard))
                return ConversationHandler.END
        except Exception as e:
            logging.error(f"Force join check error: {e}")
            
    keyboard = ReplyKeyboardMarkup(
        [
            ["Get Comment", "My Profile"],
            ["Withdrawal", "History"]
        ],
        resize_keyboard=True
    )
    msg = (
        f"👋 Welcome {update.effective_user.first_name}!\n"
        f"{SEP}\n\n"
        f"Ye bot se aap review ke liye comment le sakte ho aur proof bhi submit kar sakte ho.\n\n"
        f"Neeche se option choose karo."
    )
    await update.message.reply_text(msg, reply_markup=keyboard)
    return ConversationHandler.END

async def check_join_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = load_data()
    force_join = data.get("force_join_channel")
    
    if force_join:
        try:
            member = await context.bot.get_chat_member(force_join, uid)
            if member.status in ["member", "administrator", "creator"]:
                await query.delete_message()
                keyboard = ReplyKeyboardMarkup(
                    [
                        ["Get Comment", "My Profile"],
                        ["Withdrawal", "History"]
                    ],
                    resize_keyboard=True
                )
                msg = (
                    f"👋 Welcome {update.effective_user.first_name}!\n"
                    f"{SEP}\n\n"
                    f"Ye bot se aap review ke liye comment le sakte ho aur proof bhi submit kar sakte ho.\n\n"
                    f"Neeche se option choose karo."
                )
                await context.bot.send_message(chat_id=uid, text=msg, reply_markup=keyboard)
                return ConversationHandler.END
        except Exception as e:
            logging.error(e)
            
    await query.edit_message_text("Aapne abhi tak join nahi kiya hai.")
    return ConversationHandler.END

async def get_comment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    
    if uid in data.get("banned_users", []):
        await update.message.reply_text("🚫 Aap ban ho chuke ho. Contact: @dtxzahid")
        return ConversationHandler.END
        
    ban_info = data.get("auto_ban", {}).get(uid)
    if ban_info and ban_info.get("banned_until", 0) > time.time():
        hours_left = (ban_info["banned_until"] - time.time()) / 3600
        await update.message.reply_text(f"🚫 Aapko {hours_left:.1f} ghante ke liye ban kiya gaya hai.")
        return ConversationHandler.END

    last_req = data.get("last_request", {}).get(uid, 0)
    if time.time() - last_req < 120:
        rem = int(120 - (time.time() - last_req))
        await update.message.reply_text(f"⏳ Thoda ruko! {rem} seconds ke baad dobara try karo.")
        return ConversationHandler.END

    apps = data.get("apps", {})
    if not apps:
        await update.message.reply_text("⚠️ Koi app available nahi hai.")
        return ConversationHandler.END

    keyboard = []
    for app in apps:
        keyboard.append([InlineKeyboardButton(f"📱 {app}", callback_data=f"app_{app}")])
    
    await update.message.reply_text("👇 App choose karo:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_PROOF

async def app_select_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = load_data()
    app_name = query.data.replace("app_", "")
    
    user_history = data.get("history", {}).get(uid, {})
    if app_name in user_history:
        pending = data.get("pending_proofs", {}).get(uid, {})
        submitted = data.get("proofs", {}).get(uid, {})
        if app_name not in pending and app_name not in submitted:
            del data["history"][uid][app_name]
            save_data(data)
        else:
            await query.edit_message_text(f"⚠️ Aapne is app ka comment already le liya hai.\n\nAur comment chahiye toh contact karo: @DTXZAHID")
            return ConversationHandler.END

    comments = data.get("apps", {}).get(app_name, [])
    if not comments:
        await query.edit_message_text("⚠️ Is app ke liye comments khatam ho gaye hain.")
        return ConversationHandler.END

    usage_map = data.get("comment_usage", {}).get(app_name, {})
    min_usage = min([usage_map.get(c, 0) for c in comments])
    best_comments = [c for c in comments if usage_map.get(c, 0) == min_usage]
    chosen_comment = random.choice(best_comments)

    if "comment_usage" not in data: data["comment_usage"] = {}
    if app_name not in data["comment_usage"]: data["comment_usage"][app_name] = {}
    data["comment_usage"][app_name][chosen_comment] = usage_map.get(chosen_comment, 0) + 1

    if "history" not in data: data["history"] = {}
    if uid not in data["history"]: data["history"][uid] = {}
    data["history"][uid][app_name] = chosen_comment

    if "attempts" not in data: data["attempts"] = {}
    if uid not in data["attempts"]: data["attempts"][uid] = {}
    data["attempts"][uid][app_name] = 3

    if "pending_proofs" not in data: data["pending_proofs"] = {}
    if uid not in data["pending_proofs"]: data["pending_proofs"][uid] = {}
    data["pending_proofs"][uid][app_name] = {
        "comment": chosen_comment,
        "timestamp": time.time()
    }

    if "last_request" not in data: data["last_request"] = {}
    data["last_request"][uid] = time.time()

    save_data(data)

    await query.edit_message_text(
        f"💬 Aapka Comment\n{SEP}\n\n`{chosen_comment}`\n\nCopy karne ke liye upar tap karo.",
        parse_mode=ParseMode.MARKDOWN
    )
    await query.message.reply_text(
        f"📸 Screenshot yaha bhejo.\n\n⏰ Aapke paas 1 ghante ka time hai.\n1 ghante ke baad proof accept nahi hoga.\nZarurat ho toh contact karo: @dtxzahid"
    )

    job_queue = context.job_queue
    job_queue.run_once(proof_timeout_job, 3600, chat_id=query.message.chat_id, user_id=update.effective_user.id, name=f"timeout_{uid}_{app_name}", data={"uid": uid, "app": app_name})
    job_queue.run_once(proof_reminder_job, 1800, chat_id=query.message.chat_id, user_id=update.effective_user.id, name=f"rem_{uid}_{app_name}", data={"uid": uid, "app": app_name})

    context.user_data["awaiting_proof"] = True
    context.user_data["current_app"] = app_name
    return AWAITING_PROOF

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_proof"):
        await update.message.reply_text("⚠️ Sirf screenshot bhejo jab aap task kar rahe ho.")
        return ConversationHandler.END

    uid = str(update.effective_user.id)
    data = load_data()
    app_name = context.user_data.get("current_app")

    photo = update.message.photo[-1]
    file_unique_id = photo.file_unique_id

    if file_unique_id in data.get("used_screenshots", []):
        await update.message.reply_text("❌ Ye screenshot already use ho chuka hai.")
        return AWAITING_PROOF

    pending = data.get("pending_proofs", {}).get(uid, {})
    if app_name not in pending:
        await update.message.reply_text("⏰ Time khatam ya koi active task nahi.")
        context.user_data["awaiting_proof"] = False
        return ConversationHandler.END

    if time.time() - pending[app_name]["timestamp"] > 3600:
        await update.message.reply_text("⏰ Time khatam.")
        del data["pending_proofs"][uid][app_name]
        save_data(data)
        context.user_data["awaiting_proof"] = False
        return ConversationHandler.END

    submitted = data.get("proofs", {}).get(uid, {})
    if app_name in submitted:
        await update.message.reply_text("⚠️ Aap already iska proof submit kar chuke ho.")
        context.user_data["awaiting_proof"] = False
        return ConversationHandler.END

    chosen_comment = pending[app_name]["comment"]
    
    file = await context.bot.get_file(photo.file_id)
    image_path = "temp_proof.jpg"
    await file.download_to_drive(image_path)

    result = await analyze_screenshot(image_path, chosen_comment, app_name)
    
    try:
        os.remove(image_path)
    except:
        pass

    if not result:
        await update.message.reply_text("⚠️ AI error. Dobara try karo ya admin se contact karo.")
        return AWAITING_PROOF

    if not result.get("comment_match"):
        data["attempts"][uid][app_name] -= 1
        rem = data["attempts"][uid][app_name]
        
        if "rejections" not in data: data["rejections"] = []
        data["rejections"].append({
            "user_id": uid,
            "username": update.effective_user.username or "None",
            "app_name": app_name,
            "reason": "AI: Comment match nahi hua",
            "timestamp": time.time()
        })
        
        if "users" not in data: data["users"] = {}
        if uid not in data["users"]: data["users"][uid] = {"balance": 0, "total_tasks": 0, "accepted": 0, "rejected": 0}
        data["users"][uid]["rejected"] += 1
        data["users"][uid]["total_tasks"] += 1

        if rem > 0:
            await update.message.reply_text(f"❌ Proof Rejected\n\n{rem} Attempts Baaki Hain\nDobara Screenshot Bhejo")
            save_data(data)
            return AWAITING_PROOF
        else:
            if "auto_ban" not in data: data["auto_ban"] = {}
            if uid not in data["auto_ban"]: data["auto_ban"][uid] = {"failed_apps": [], "banned_until": 0}
            if app_name not in data["auto_ban"][uid]["failed_apps"]:
                data["auto_ban"][uid]["failed_apps"].append(app_name)
            
            if len(data["auto_ban"][uid]["failed_apps"]) >= 3:
                data["auto_ban"][uid]["banned_until"] = time.time() + 86400
                del data["pending_proofs"][uid][app_name]
                if app_name in data.get("history", {}).get(uid, {}):
                    del data["history"][uid][app_name]
                save_data(data)
                cancel_user_jobs(context.job_queue, uid, app_name)
                context.user_data["awaiting_proof"] = False
                await update.message.reply_text(f"❌ Saare Attempts Khatam\n\n🚫 Aapko 24 ghante ke liye ban kiya gaya hai.\nWajah: Baar baar fake proofs.\nContact: @dtxzahid")
                return ConversationHandler.END
            else:
                del data["pending_proofs"][uid][app_name]
                if app_name in data.get("history", {}).get(uid, {}):
                    del data["history"][uid][app_name]
                save_data(data)
                cancel_user_jobs(context.job_queue, uid, app_name)
                context.user_data["awaiting_proof"] = False
                await update.message.reply_text(f"❌ Saare Attempts Khatam\n\nAb aap is app ke liye screenshot nahi bhej sakte.\n\nDusra app try karo ya contact: @dtxzahid")
                return ConversationHandler.END
    else:
        if "users" not in data: data["users"] = {}
        if uid not in data["users"]: data["users"][uid] = {"balance": 0, "total_tasks": 0, "accepted": 0, "rejected": 0}
        data["users"][uid]["accepted"] += 1
        data["users"][uid]["total_tasks"] += 1
        
        if "proofs" not in data: data["proofs"] = {}
        if uid not in data["proofs"]: data["proofs"][uid] = {}
        data["proofs"][uid][app_name] = True
        
        if "saved_proofs" not in data: data["saved_proofs"] = []
        data["saved_proofs"].append({
            "user_id": uid,
            "username": update.effective_user.username or "None",
            "app_name": app_name,
            "reviewer_name": result.get("reviewer_name", "Unknown"),
            "status": "approved",
            "timestamp": time.time()
        })
        
        if "used_screenshots" not in data: data["used_screenshots"] = []
        data["used_screenshots"].append(file_unique_id)
        
        if "auto_ban" in data and uid in data["auto_ban"]:
            data["auto_ban"][uid]["failed_apps"] = []
            
        del data["pending_proofs"][uid][app_name]
        if app_name in data.get("history", {}).get(uid, {}):
            del data["history"][uid][app_name]
            
        save_data(data)
        cancel_user_jobs(context.job_queue, uid, app_name)
        context.user_data["awaiting_proof"] = False
        
        try:
            admin_caption = (
                f"📥 New Proof Auto-Approved\n{SEP}\n"
                f"👤 User: @{update.effective_user.username or 'None'}\n"
                f"🆔 ID: {uid}\n"
                f"📱 App: {app_name}\n"
                f"🔍 Reviewer: {result.get('reviewer_name', 'Unknown')}"
            )
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=admin_caption)
        except Exception as e:
            logging.error(f"Admin notify error: {e}")
            
        await update.message.reply_text(
            f"✅ Proof Verified\n{SEP}\n\nAapka proof accept ho gaya.\n\n⚠️ Note: Fake screenshots se account ban ho sakta hai."
        )
        return ConversationHandler.END

async def global_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_proof"):
        await update.message.reply_text("⚠️ Sirf screenshot bhejo jab aap task kar rahe ho.")

async def text_while_awaiting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ Sirf screenshot bhejo, text nahi.")
    return AWAITING_PROOF

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    user = data.get("users", {}).get(uid, {})
    
    msg = (
        f"👤 {update.effective_user.first_name}\n"
        f"{SEP}\n"
        f"🆔 ID: {uid}\n"
        f"💰 Balance: ₹{user.get('balance', 0)}\n"
        f"📝 Total Tasks: {user.get('total_tasks', 0)}\n"
        f"✅ Accepted: {user.get('accepted', 0)}\n"
        f"❌ Rejected: {user.get('rejected', 0)}"
    )
    await update.message.reply_text(msg)
    return ConversationHandler.END

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    
    tasks = []
    for p in data.get("saved_proofs", []):
        if p.get("user_id") == uid:
            tasks.append({"type": "approved", "app": p.get("app_name"), "time": p.get("timestamp")})
    for r in data.get("rejections", []):
        if r.get("user_id") == uid:
            tasks.append({"type": "rejected", "app": r.get("app_name"), "time": r.get("timestamp")})
            
    tasks.sort(key=lambda x: x.get("time", 0), reverse=True)
    tasks = tasks[:10]
    
    if not tasks:
        await update.message.reply_text("⚠️ Koi history nahi hai.")
        return ConversationHandler.END
        
    msg = f"📜 History (Last 10)\n{SEP}\n"
    for t in tasks:
        icon = "✅" if t["type"] == "approved" else "❌"
        msg += f"{icon} {t['app']}\n"
        
    await update.message.reply_text(msg)
    return ConversationHandler.END

async def withdrawal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    
    force_join = data.get("force_join_channel")
    if force_join:
        try:
            member = await context.bot.get_chat_member(force_join, uid)
            if member.status not in ["member", "administrator", "creator"]:
                keyboard = [
                    [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{force_join.lstrip('@')}")],
                    [InlineKeyboardButton("✅ I Joined", callback_data="check_join_wd")]
                ]
                if update.message:
                    await update.message.reply_text("Is feature ko use karne ke liye pehle channel join karo.", reply_markup=InlineKeyboardMarkup(keyboard))
                else:
                    await update.callback_query.edit_message_text("Is feature ko use karne ke liye pehle channel join karo.", reply_markup=InlineKeyboardMarkup(keyboard))
                return ConversationHandler.END
        except Exception as e:
            logging.error(f"Force join check error: {e}")

    user_data = data.get("users", {}).get(uid, {})
    balance = user_data.get("balance", 0)
    if balance < 10:
        msg = f"❌ Minimum ₹10 hai. 💵 Aapka balance: ₹{balance}"
        if update.message:
            await update.message.reply_text(msg)
        else:
            await update.callback_query.edit_message_text(msg)
        return ConversationHandler.END
        
    keyboard = [
        [InlineKeyboardButton("💳 UPI", callback_data="withdraw_upi")],
        [InlineKeyboardButton("🎗️ VSV", callback_data="withdraw_vsv")]
    ]
    msg = "Withdrawal method choose karo:"
    if update.message:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    return WITHDRAW_METHOD

async def withdraw_method_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "withdraw_vsv":
        await query.edit_message_text(f"🎗️ VSV Withdrawal\n{SEP}\n\nOwner ko direct message karo:\n👉 @dtxzahid")
        return ConversationHandler.END
    elif data == "withdraw_upi":
        await query.edit_message_text("💳 Apna UPI ID bhejo:")
        context.user_data["wd_method"] = "UPI"
        return WITHDRAW_UPI
    elif data == "check_join_wd":
        uid = str(update.effective_user.id)
        data_dict = load_data()
        force_join = data_dict.get("force_join_channel")
        if force_join:
            try:
                member = await context.bot.get_chat_member(force_join, uid)
                if member.status in ["member", "administrator", "creator"]:
                    return await withdrawal_start(update, context)
            except:
                pass
        await query.edit_message_text("Aapne abhi tak join nahi kiya hai.")
        return ConversationHandler.END

async def withdraw_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upi_id = update.message.text
    context.user_data["wd_upi"] = upi_id
    await update.message.reply_text("💵 Amount kitna chahiye? (Numbers me bhejo)")
    return WITHDRAW_AMT

async def withdraw_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
    except:
        await update.message.reply_text("⚠️ Galat amount. Sirf numbers bhejo.")
        return WITHDRAW_AMT

    uid = str(update.effective_user.id)
    data = load_data()
    balance = data.get("users", {}).get(uid, {}).get("balance", 0)
    
    if amount < 10:
        await update.message.reply_text("❌ Minimum ₹10 hai.")
        return ConversationHandler.END
    if amount > balance:
        await update.message.reply_text(f"❌ Aapke paas ₹{balance} hi hain.")
        return ConversationHandler.END
        
    data["users"][uid]["balance"] -= amount
    
    wd_info = {
        "user_id": uid,
        "username": update.effective_user.username or "None",
        "amount": amount,
        "method": "UPI",
        "upi_id": context.user_data.get("wd_upi"),
        "status": "pending",
        "timestamp": time.time()
    }
    if "withdrawals" not in data: data["withdrawals"] = []
    data["withdrawals"].append(wd_info)
    save_data(data)
    
    try:
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"wd_admin_approve_{len(data['withdrawals'])-1}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"wd_admin_reject_{len(data['withdrawals'])-1}")
            ]
        ]
        msg = (
            f"💸 Withdrawal Request\n{SEP}\n"
            f"👤 @{wd_info['username']} ({uid})\n"
            f"💰 ₹{amount}\n"
            f"💳 {wd_info['upi_id']}"
        )
        await context.bot.send_message(ADMIN_ID, msg, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logging.error(f"Admin notify error: {e}")

    await update.message.reply_text("✅ Withdrawal request admin ko bhej di gayi hai.")
    return ConversationHandler.END

async def withdraw_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END
        
    parts = query.data.split("_")
    action = parts[2]
    idx = int(parts[3])
    
    data = load_data()
    if idx >= len(data.get("withdrawals", [])):
        await query.edit_message_text("⚠️ Invalid withdrawal.")
        return ConversationHandler.END
        
    wd = data["withdrawals"][idx]
    
    if action == "approve":
        wd["status"] = "approved"
        save_data(data)
        await query.edit_message_text(query.message.text + "\n\n✅ Approved")
        try:
            await context.bot.send_message(wd["user_id"], f"✅ Aapka ₹{wd['amount']} ka withdrawal approve ho gaya hai.")
        except:
            pass
    elif action == "reject":
        wd["status"] = "rejected"
        data["users"][wd["user_id"]]["balance"] += wd["amount"]
        save_data(data)
        await query.edit_message_text(query.message.text + "\n\n❌ Rejected")
        try:
            await context.bot.send_message(wd["user_id"], f"❌ Aapka ₹{wd['amount']} ka withdrawal reject ho gaya hai. Balance wapas add kar diya gaya hai.")
        except:
            pass
            
    return ConversationHandler.END

# ═══════════════════════════════════════════════════
# ADMIN HANDLERS
# ═══════════════════════════════════════════════════
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END
        
    keyboard = ReplyKeyboardMarkup(
        [
            ["Add Comment", "Live Apps"],
            ["Saved Proof", "Stats"],
            ["Broadcast", "Add/Remove Bal"],
            ["Set Force Join", "Ban User"]
        ],
        resize_keyboard=True
    )
    await update.message.reply_text("🔥 Admin Panel", reply_markup=keyboard)
    return ADMIN_MENU

async def admin_add_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 App ka naam bhejo:")
    return ADD_APP

async def add_app_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_name = update.message.text
    context.user_data["admin_app_name"] = app_name
    await update.message.reply_text("📝 Comments bhejo (comma-separated):")
    return ADD_COMMENTS

async def add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    comments = [c.strip() for c in text.split(",") if c.strip()]
    app_name = context.user_data.get("admin_app_name")
    
    data = load_data()
    if "apps" not in data: data["apps"] = {}
    if app_name not in data["apps"]: data["apps"][app_name] = []
    
    data["apps"][app_name].extend(comments)
    save_data(data)
    
    await update.message.reply_text(f"✅ {len(comments)} comments add ho gaye {app_name} me.")
    return ADMIN_MENU

async def admin_live_apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = data.get("apps", {})
    if not apps:
        await update.message.reply_text("⚠️ Koi app nahi hai.")
        return ADMIN_MENU
        
    keyboard = []
    for app, comments in apps.items():
        usage_map = data.get("comment_usage", {}).get(app, {})
        used_count = sum(1 for c in comments if usage_map.get(c, 0) > 0)
        left = len(comments) - used_count
        keyboard.append([InlineKeyboardButton(f"📱 {app} ({left} left / {used_count} used)", callback_data=f"admin_app_{app}")])
        
    await update.message.reply_text("👇 Live Apps:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MENU

async def admin_app_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not query.data.startswith("admin_app_"):
        return ConversationHandler.END
        
    app_name = query.data.replace("admin_app_", "")
    context.user_data["admin_current_app"] = app_name
    
    data = load_data()
    comments = data.get("apps", {}).get(app_name, [])
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Comments", callback_data=f"add_coms_{app_name}")],
        [InlineKeyboardButton("➖ Remove a Comment", callback_data=f"rem_com_{app_name}")],
        [InlineKeyboardButton("🗑️ Delete App", callback_data=f"del_app_{app_name}")]
    ]
    
    await query.edit_message_text(
        f"📱 {app_name}\nTotal: {len(comments)}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_MENU

async def admin_app_sub_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_str = query.data
    
    if data_str.startswith("add_coms_"):
        app_name = data_str.replace("add_coms_", "")
        context.user_data["admin_app_add"] = app_name
        await query.edit_message_text(f"📝 {app_name} ke liye naye comments bhejo (comma-separated):")
        return APP_ADD_COMMENTS
        
    elif data_str.startswith("rem_com_"):
        app_name = data_str.replace("rem_com_", "")
        data = load_data()
        comments = data.get("apps", {}).get(app_name, [])
        
        keyboard = []
        for c in comments[:20]:
            keyboard.append([InlineKeyboardButton(f"❌ {c}", callback_data=f"del_c_{app_name}_{comments.index(c)}")])
        if not keyboard:
            await query.edit_message_text("⚠️ Koi comment nahi hai.")
            return ADMIN_MENU
            
        await query.edit_message_text("👇 Delete karne ke liye choose karo:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ADMIN_MENU
        
    elif data_str.startswith("del_app_"):
        app_name = data_str.replace("del_app_", "")
        data = load_data()
        if app_name in data.get("apps", {}):
            del data["apps"][app_name]
            if app_name in data.get("comment_usage", {}):
                del data["comment_usage"][app_name]
            save_data(data)
            await query.edit_message_text(f"✅ {app_name} delete ho gaya.")
        return ADMIN_MENU
        
    elif data_str.startswith("del_c_"):
        parts = data_str.split("_")
        app_name = parts[2]
        idx = int(parts[3])
        
        data = load_data()
        if app_name in data.get("apps", {}):
            comments = data["apps"][app_name]
            if idx < len(comments):
                removed = comments.pop(idx)
                if app_name in data.get("comment_usage", {}) and removed in data["comment_usage"][app_name]:
                    del data["comment_usage"][app_name][removed]
                save_data(data)
                await query.edit_message_text(f"✅ Comment delete ho gaya.")
        return ADMIN_MENU

async def app_add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    comments = [c.strip() for c in text.split(",") if c.strip()]
    app_name = context.user_data.get("admin_app_add")
    
    data = load_data()
    if "apps" not in data: data["apps"] = {}
    if app_name not in data["apps"]: data["apps"][app_name] = []
    data["apps"][app_name].extend(comments)
    save_data(data)
    
    await update.message.reply_text(f"✅ {len(comments)} comments add ho gaye.")
    return ADMIN_MENU

async def admin_saved_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    proofs = data.get("saved_proofs", [])[-10:][::-1]
    if not proofs:
        await update.message.reply_text("⚠️ Koi saved proof nahi hai.")
        return ADMIN_MENU
        
    msg = "📥 Last 10 Saved Proofs:\n" + SEP + "\n"
    for p in proofs:
        msg += f"📱 {p.get('app_name')} | 👤 {p.get('reviewer_name')}\n@{p.get('username')} | ID: {p.get('user_id')}\n\n"
        
    await update.message.reply_text(msg)
    return ADMIN_MENU

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    users = len(data.get("users", {}))
    approved = len(data.get("saved_proofs", []))
    rejected = len(data.get("rejections", []))
    active_tasks = sum(len(v) for v in data.get("pending_proofs", {}).values())
    
    bal = 0
    for u in data.get("users", {}).values():
        bal += u.get("balance", 0)
        
    withdrawn = sum(w.get("amount", 0) for w in data.get("withdrawals", []) if w.get("status") == "approved")
    
    msg = (
        f"📊 Bot Stats\n{SEP}\n"
        f"👥 Total Users: {users}\n"
        f"📝 Approved Proofs: {approved}\n"
        f"❌ Rejected Proofs: {rejected}\n"
        f"⏳ Active Tasks: {active_tasks}\n"
        f"💰 Active Balance: ₹{bal}\n"
        f"💸 Total Withdrawn: ₹{withdrawn}"
    )
    await update.message.reply_text(msg)
    return ADMIN_MENU

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📢 Broadcast message bhejo:")
    return BROADCAST_MSG

async def broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    data = load_data()
    users = list(data.get("users", {}).keys())
    
    await update.message.reply_text(f"📢 Broadcasting to {len(users)} users...")
    count = 0
    for uid in users:
        try:
            await context.bot.send_message(uid, f"📢 Announcement\n{SEP}\n\n{msg}")
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
            
    await update.message.reply_text(f"✅ Broadcast complete. Sent to {count} users.")
    return ADMIN_MENU

async def admin_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆔 User ID bhejo:")
    return ADMIN_BAL_ID

async def admin_bal_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text
    context.user_data["bal_uid"] = uid
    await update.message.reply_text("💵 Amount bhejo (+ to add, - to remove):")
    return ADMIN_BAL_AMT

async def admin_bal_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
    except:
        await update.message.reply_text("⚠️ Galat amount.")
        return ADMIN_BAL_AMT
        
    uid = context.user_data.get("bal_uid")
    data = load_data()
    
    if "users" not in data: data["users"] = {}
    if uid not in data["users"]: 
        data["users"][uid] = {"balance": 0, "total_tasks": 0, "accepted": 0, "rejected": 0}
        
    data["users"][uid]["balance"] += amt
    new_bal = data["users"][uid]["balance"]
    save_data(data)
    
    action = "added" if amt > 0 else "removed"
    await update.message.reply_text(
        f"✅ Balance update.\n{SEP}\n👤 {uid}\n💵 ₹{abs(amt)} {action}\n💰 New: ₹{new_bal}"
    )
    return ADMIN_MENU

async def admin_force_join_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    current = data.get("force_join_channel")
    msg = f"Current Force Join: {current or 'None'}\n\nNaya channel bhejo (@channel) ya 'off' likho:"
    await update.message.reply_text(msg)
    return FORCE_JOIN_SET

async def force_join_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    data = load_data()
    if text.lower() == "off":
        data["force_join_channel"] = None
        await update.message.reply_text("✅ Force join disable ho gaya.")
    else:
        data["force_join_channel"] = text
        await update.message.reply_text(f"✅ Force join set to {text}")
    save_data(data)
    return ADMIN_MENU

async def admin_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆔 Ban karne ke liye User ID bhejo:")
    return BAN_USER_ID

async def ban_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.text
    data = load_data()
    if "banned_users" not in data: data["banned_users"] = []
    if uid not in data["banned_users"]:
        data["banned_users"].append(uid)
        save_data(data)
        await update.message.reply_text(f"✅ {uid} ko ban kar diya gaya.")
    else:
        await update.message.reply_text("⚠️ User already banned.")
    return ADMIN_MENU

# ═══════════════════════════════════════════════════
# JOB QUEUE HANDLERS
# ═══════════════════════════════════════════════════
def cancel_user_jobs(job_queue, uid, app_name):
    for job in job_queue.get_jobs_by_name(f"timeout_{uid}_{app_name}"):
        job.schedule_removal()
    for job in job_queue.get_jobs_by_name(f"rem_{uid}_{app_name}"):
        job.schedule_removal()

async def proof_timeout_job(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    uid = context.job.data["uid"]
    app_name = context.job.data["app"]
    
    pending = data.get("pending_proofs", {}).get(uid, {})
    if app_name in pending:
        usage_map = data.get("comment_usage", {}).get(app_name, {})
        comment = pending[app_name]["comment"]
        if comment in usage_map:
            usage_map[comment] = max(0, usage_map[comment] - 1)
        
        del data["pending_proofs"][uid][app_name]
        if app_name in data.get("history", {}).get(uid, {}):
            del data["history"][uid][app_name]
        if app_name in data.get("attempts", {}).get(uid, {}):
            del data["attempts"][uid][app_name]
            
        save_data(data)
        
        try:
            await context.bot.send_message(
                chat_id=context.job.chat_id,
                text=f"⏰ Time khatam!\n{SEP}\n\nAapne 1 ghante me {app_name} ka proof nahi bheja.\n\nNaya comment ke liye contact karo: @dtxzahid"
            )
        except:
            pass

async def proof_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    uid = context.job.data["uid"]
    app_name = context.job.data["app"]
    
    pending = data.get("pending_proofs", {}).get(uid, {})
    if app_name in pending:
        try:
            await context.bot.send_message(
                chat_id=context.job.chat_id,
                text=f"⏰ Sirf 30 minute bache hain {app_name} ka proof bhejne ke liye!"
            )
        except:
            pass

# ═══════════════════════════════════════════════════
# FALLBACKS AND MISC
# ═══════════════════════════════════════════════════
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        if ADMIN_ID:
            await context.bot.send_message(ADMIN_ID, f"⚠️ Bot Error:\n\n{context.error}")
    except Exception as e:
        logging.error(f"Failed to send error to admin: {e}")

async def main_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Get Comment": return await get_comment_start(update, context)
    elif text == "My Profile": return await my_profile(update, context)
    elif text == "Withdrawal": return await withdrawal_start(update, context)
    elif text == "History": return await history(update, context)
    elif text == "Add Comment": return await admin_add_comment(update, context)
    elif text == "Live Apps": return await admin_live_apps(update, context)
    elif text == "Saved Proof": return await admin_saved_proofs(update, context)
    elif text == "Stats": return await admin_stats(update, context)
    elif text == "Broadcast": return await admin_broadcast_start(update, context)
    elif text == "Add/Remove Bal": return await admin_bal_start(update, context)
    elif text == "Set Force Join": return await admin_force_join_start(update, context)
    elif text == "Ban User": return await admin_ban_start(update, context)
    return ConversationHandler.END

# ═══════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════
def main() -> None:
    connect_db()
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("DTX", admin_start),
            CommandHandler("cancel", cancel),
            MessageHandler(filters.TEXT & filters.Regex("^(Get Comment|My Profile|Withdrawal|History|Add Comment|Live Apps|Saved Proof|Stats|Broadcast|Add/Remove Bal|Set Force Join|Ban User)$"), main_menu_router),
        ],
        states={
            AWAITING_PROOF: [
                CallbackQueryHandler(app_select_cb, pattern="^app_"), 
                MessageHandler(filters.PHOTO, handle_photo), 
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_while_awaiting)
            ],
            ADD_APP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app_name)],
            ADD_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_comments)],
            BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_msg)],
            ADMIN_BAL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bal_id)],
            ADMIN_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_bal_amt)],
            WITHDRAW_METHOD: [CallbackQueryHandler(withdraw_method_cb, pattern="^withdraw_|^check_join_wd$")],
            WITHDRAW_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi)],
            WITHDRAW_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amt)],
            FORCE_JOIN_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, force_join_set)],
            BAN_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_user_id)],
            APP_ADD_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, app_add_comments)],
            ADMIN_MENU: [
                CallbackQueryHandler(admin_app_action_cb, pattern="^admin_app_"), 
                CallbackQueryHandler(admin_app_sub_cb, pattern="^(add_coms_|rem_com_|del_app_|del_c_)")
            ]
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("DTX", admin_start),
            CommandHandler("cancel", cancel),
            MessageHandler(filters.TEXT & filters.Regex("^(Get Comment|My Profile|Withdrawal|History|Add Comment|Live Apps|Saved Proof|Stats|Broadcast|Add/Remove Bal|Set Force Join|Ban User)$"), main_menu_router),
            CallbackQueryHandler(check_join_cb, pattern="^check_join$"),
            CallbackQueryHandler(withdraw_admin_cb, pattern="^wd_admin_"),
        ],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.PHOTO, global_photo_handler))
    application.add_error_handler(error_handler)
    
    logging.info("✅ Bot started polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
