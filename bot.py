import logging
import os
import json
import random
import base64
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, ConversationHandler, CallbackQueryHandler
from groq import Groq
from pymongo import MongoClient

# --- Read secrets from Railway environment variables ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MONGODB_URI = os.environ.get("MONGODB_URI")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
PORT = int(os.environ.get("PORT", 8080))

# --- Validate that everything is set ---
if not TELEGRAM_TOKEN: raise ValueError("Missing TELEGRAM_TOKEN.")
if not GROQ_API_KEY: raise ValueError("Missing GROQ_API_KEY.")
if not MONGODB_URI: raise ValueError("Missing MONGODB_URI. Please set it in Railway Variables.")
if not RAILWAY_PUBLIC_DOMAIN: raise ValueError("Missing RAILWAY_PUBLIC_DOMAIN.")

# --- MongoDB Setup & Connection Test ---
try:
    mongo_client = MongoClient(MONGODB_URI)
    # Ping the database to verify connection
    mongo_client.admin.command('ping')
    print("✅ MongoDB Connected Successfully!")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")
    raise e

db = mongo_client["telegram_bot"]
collection = db["data"]

def load_data():
    try:
        doc = collection.find_one({"_id": "config"})
        if not doc:
            return {"apps": {}, "history": {}, "proofs": {}, "saved_proofs": []}
        doc.pop("_id", None)
        return doc
    except Exception as e:
        print(f"Error loading data: {e}")
        return {"apps": {}, "history": {}, "proofs": {}, "saved_proofs": []}

def save_data(data):
    try:
        data["_id"] = "config"
        collection.replace_one({"_id": "config"}, data, upsert=True)
    except Exception as e:
        print(f"Error saving data: {e}")

# --- Groq Client Initialization ---
client = Groq(api_key=GROQ_API_KEY)
VISION_MODEL = "qwen/qwen3.8-27b"

# --- Keyboards ---
MAIN_KEYBOARD = ReplyKeyboardMarkup([["Get Comment"]], resize_keyboard=True)
ADMIN_KEYBOARD = ReplyKeyboardMarkup([["Add Comment"], ["Saved Proof"]], resize_keyboard=True)

# --- Conversation States ---
ADMIN_MENU, ADD_APP, ADD_COMMENTS = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# --- Basic Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_proof"] = False
    context.user_data["proof_app"] = None
    await update.message.reply_text("Welcome! Use the button below to get a comment.", reply_markup=MAIN_KEYBOARD)

# --- Admin Panel Logic ---
async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return ConversationHandler.END
    await update.message.reply_text("Admin Panel Opened. What would you like to do?", reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

async def add_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please enter the App Name:")
    return ADD_APP

async def add_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_name = update.message.text.strip()
    context.user_data["temp_app_name"] = app_name
    await update.message.reply_text(f"App Name saved: {app_name}.\nNow send the comments, separated by commas:")
    return ADD_COMMENTS

async def add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comments_text = update.message.text
    comments_list = [c.strip() for c in comments_text.split(",") if c.strip()]
    app_name = context.user_data.get("temp_app_name")
    data = load_data()
    if app_name not in data["apps"]: data["apps"][app_name] = []
    data["apps"][app_name].extend(comments_list)
    save_data(data)
    await update.message.reply_text("User Comment Added Successful", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

async def show_saved_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    proofs = data.get("saved_proofs", [])
    if not proofs:
        await update.message.reply_text("No saved proofs yet.", reply_markup=ADMIN_KEYBOARD)
        return ADMIN_MENU
    text = "📋 **Saved Proofs**\n\n"
    for i, p in enumerate(proofs[-10:], 1):
        text += f"{i}. App: {p['app_name']}\n   Reviewer: {p['reviewer_name']}\n   User ID: {p['user_id']}\n   Username: @{p.get('username', 'N/A')}\n\n"
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return ADMIN_MENU

# --- User Get Comment Logic ---
async def get_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = list(data.get("apps", {}).keys())
    if not apps:
        await update.message.reply_text("No Task Available", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(app, callback_data=f"getapp_{app}")] for app in apps]
    await update.message.reply_text("Please select an app:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def user_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    app_name = query.data.replace("getapp_", "")
    user_id = str(update.effective_user.id)
    data = load_data()
    if app_name not in data["apps"] or not data["apps"][app_name]:
        await query.edit_message_text("No comments found for this app.")
        return
    if user_id not in data["history"]: data["history"][user_id] = {}
    if app_name in data["history"][user_id] and len(data["history"][user_id][app_name]) >= 1:
        await query.edit_message_text("You have already received a comment for this app. You can only get 1 comment per app.")
        return
    all_comments = data["apps"][app_name]
    available_comments = [c for c in all_comments if c not in data["history"][user_id].get(app_name, [])]
    if not available_comments:
        await query.edit_message_text("No more unique comments available for this app.")
        return
    chosen_comment = random.choice(available_comments)
    data["history"][user_id][app_name] = [chosen_comment]
    save_data(data)
    await query.edit_message_text(f"Here is your comment:\n\n`{chosen_comment}`", parse_mode="Markdown")
    await context.bot.send_message(chat_id=user_id, text="Please Share Screenshot Of Review To Us 📸")
    context.user_data["awaiting_proof"] = True
    context.user_data["proof_app"] = app_name

# --- Proof Handling Logic ---
async def handle_proof_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        await update.message.reply_text("⚠️ Only proof is accepted. Please send the screenshot.")
        return

async def handle_proof_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_proof"):
        photo_file = await update.message.photo[-1].get_file()
        app_name = context.user_data.get("proof_app", "Unknown App")
        user = update.effective_user
        user_id = str(user.id)
        data = load_data()

        if user_id in data["proofs"] and app_name in data["proofs"][user_id]:
            await update.message.reply_text("❌ You have already submitted a proof for this app. You can only submit one proof per app.\n\nIf you uploaded the wrong screenshot, please contact: @dtxzahid", reply_markup=MAIN_KEYBOARD)
            context.user_data["awaiting_proof"] = False
            context.user_data["proof_app"] = None
            return

        await update.message.reply_text("⏳ Verifying your proof with AI. Please wait...")
        file_path = "temp_proof.jpg"
        await photo_file.download_to_drive(file_path)
        try:
            with open(file_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode("utf-8")
            chosen_comment = data["history"][user_id][app_name][0]
            prompt = f"""
            Analyze this screenshot carefully.
            1. Does it show a review for the app "{app_name}"?
            2. Does the review text match or contain this exact comment: "{chosen_comment}"?
            3. What is the name of the reviewer?
            Reply STRICTLY in this JSON format, without any markdown or extra text:
            {{
              "app_match": true or false,
              "comment_match": true or false,
              "reviewer_name": "extracted name here"
            }}
            """
            completion = client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
                temperature=0.1,
                max_completion_tokens=256,
            )
            response_text = completion.choices[0].message.content
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            result = json.loads(response_text)
            if not result.get("app_match") or not result.get("comment_match"):
                await update.message.reply_text("❌ Proof Rejected automatically.\n\nThe app name or comment in your screenshot does not match our records.\nPlease contact @dtxzahid if you think this is a mistake.", reply_markup=MAIN_KEYBOARD)
                context.user_data["awaiting_proof"] = False
                context.user_data["proof_app"] = None
                return
            reviewer_name = result.get("reviewer_name", "Unknown")
            if user_id not in data["proofs"]: data["proofs"][user_id] = {}
            data["proofs"][user_id][app_name] = True
            data["saved_proofs"].append({"app_name": app_name, "reviewer_name": reviewer_name, "user_id": user_id, "username": user.username or "N/A", "file_id": photo_file.file_id})
            save_data(data)
            admin_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"proofstatus_approve_{user_id}_{app_name}"), InlineKeyboardButton("❌ Reject", callback_data=f"proofstatus_reject_{user_id}_{app_name}")]])
            try:
                await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo_file.file_id, caption=f"📥 New Proof Received\n\n👤 User: {user.first_name} (@{user.username})\n🆔 ID: {user.id}\n📱 App: {app_name}\n🔍 Reviewer Name (AI): {reviewer_name}", reply_markup=admin_keyboard)
            except Exception as e:
                print(f"Error sending proof to admin: {e}")
            await update.message.reply_text("Proof Submitted Successfully! Thanks for your effort. This proof has been sent to our owner for verification. After verification, you will receive a notification in the bot.\n\nNote: Uploading fake/meaningless screenshots may lead to an account ban.", reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            print(f"AI Verification Error: {e}")
            await update.message.reply_text("❌ Error verifying proof. Please try again later.", reply_markup=MAIN_KEYBOARD)
        finally:
            if os.path.exists(file_path): os.remove(file_path)
        context.user_data["awaiting_proof"] = False
        context.user_data["proof_app"] = None

# --- Admin Proof Review Logic ---
async def proof_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("Only the owner can do this.", show_alert=True)
        return
    await query.answer()
    parts = query.data.split("_")
    action, user_id, app_name = parts[1], int(parts[2]), parts[3]
    if action == "approve":
        await context.bot.send_message(chat_id=user_id, text=f"✅ Your proof for {app_name} has been APPROVED! Thank you for your effort.", reply_markup=MAIN_KEYBOARD)
        await query.edit_message_caption(caption=f"✅ Proof for {app_name} from user {user_id} has been APPROVED.")
    elif action == "reject":
        await context.bot.send_message(chat_id=user_id, text=f"❌ Your proof for {app_name} has been REJECTED. Please contact @dtxzahid if you think this is a mistake.", reply_markup=MAIN_KEYBOARD)
        await query.edit_message_caption(caption=f"❌ Proof for {app_name} from user {user_id} has been REJECTED.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# --- Main Application Setup ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('DTX', admin_entry), MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry), MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry), MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs)],
        states={
            ADMIN_MENU: [MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry), MessageHandler(filters.Regex('^Saved Proof$'), show_saved_proofs)],
            ADD_APP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_app)],
            ADD_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_comments)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(user_app_callback, pattern="^getapp_"))
    app.add_handler(CallbackQueryHandler(proof_status_callback, pattern="^proofstatus_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_proof_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_proof_photo))
    webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
    print(f"Starting bot on port {PORT} with webhook {webhook_url}...")
    app.run_webhook(listen="0.0.0.0", port=PORT, url_path="/webhook", webhook_url=webhook_url)
