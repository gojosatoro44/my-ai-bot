import logging
import os
import json
import random
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, ConversationHandler, CallbackQueryHandler

# --- Read secrets from Railway environment variables ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
PORT = int(os.environ.get("PORT", 8080))

# --- Validate that everything is set ---
if not TELEGRAM_TOKEN:
    raise ValueError("Missing TELEGRAM_TOKEN environment variable.")
if not RAILWAY_PUBLIC_DOMAIN:
    raise ValueError("Missing RAILWAY_PUBLIC_DOMAIN environment variable.")
if ADMIN_ID == 0:
    print("WARNING: ADMIN_ID is not set. Admin panel will not be accessible. Please add it in Railway Variables.")

# --- Data Storage ---
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            # Ensure 'proofs' key exists
            if "proofs" not in data:
                data["proofs"] = {}
            return data
    return {"apps": {}, "history": {}, "proofs": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# --- Keyboards ---
MAIN_KEYBOARD = ReplyKeyboardMarkup([["Get Comment"]], resize_keyboard=True)
ADMIN_KEYBOARD = ReplyKeyboardMarkup([["Add Comment"]], resize_keyboard=True)

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
    await update.message.reply_text(
        "Welcome! Use the button below to get a comment.",
        reply_markup=MAIN_KEYBOARD
    )

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
    await update.message.reply_text(f"App Name saved: {app_name}.\nNow send the comments, separated by commas (e.g., Comment1,Comment2,Comment3):")
    return ADD_COMMENTS

async def add_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comments_text = update.message.text
    comments_list = [c.strip() for c in comments_text.split(",") if c.strip()]
    
    app_name = context.user_data.get("temp_app_name")
    data = load_data()
    
    if app_name not in data["apps"]:
        data["apps"][app_name] = []
    
    data["apps"][app_name].extend(comments_list)
    save_data(data)
    
    await update.message.reply_text("User Comment Added Successful", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# --- User Get Comment Logic ---
async def get_comment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    apps = list(data.get("apps", {}).keys())
    
    if not apps:
        await update.message.reply_text("No Task Available", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton(app, callback_data=f"getapp_{app}")] for app in apps]
    await update.message.reply_text(
        "Please select an app:", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

# --- Callback Handler for Inline App Buttons ---
async def user_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    app_name = query.data.replace("getapp_", "")
    user_id = str(update.effective_user.id)
    data = load_data()

    if app_name not in data["apps"] or not data["apps"][app_name]:
        await query.edit_message_text("No comments found for this app.")
        return

    if user_id not in data["history"]:
        data["history"][user_id] = {}
    
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
    
    await context.bot.send_message(
        chat_id=user_id,
        text="Please Share Screenshot Of Review To Us 📸"
    )
    
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

        # --- OPTION 3: Check if proof already submitted for this app ---
        if user_id in data["proofs"] and app_name in data["proofs"][user_id]:
            await update.message.reply_text(
                "❌ You have already submitted a proof for this app. You can only submit one proof per app.\n\n"
                "If you uploaded the wrong screenshot and need to submit a new one, please contact the owner directly: @dtxzahid",
                reply_markup=MAIN_KEYBOARD
            )
            context.user_data["awaiting_proof"] = False
            context.user_data["proof_app"] = None
            return

        # Save the proof to history
        if user_id not in data["proofs"]:
            data["proofs"][user_id] = {}
        data["proofs"][user_id][app_name] = True
        save_data(data)

        # --- OPTION 1: Add Approve/Reject Buttons for Admin ---
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"proofstatus_approve_{user_id}_{app_name}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"proofstatus_reject_{user_id}_{app_name}")
            ]
        ])

        try:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_file.file_id,
                caption=f"📥 New Proof Received\n\n👤 User: {user.first_name} (@{user.username})\n🆔 ID: {user.id}\n📱 App: {app_name}",
                reply_markup=admin_keyboard
            )
        except Exception as e:
            print(f"Error sending proof to admin: {e}")

        await update.message.reply_text(
            "Proof Submitted Successfully! Thanks for your effort. This proof has been sent to our owner for verification. "
            "After verification, you will receive a notification in the bot.\n\n"
            "Note: Uploading fake/meaningless screenshots may lead to an account ban.",
            reply_markup=MAIN_KEYBOARD
        )
        
        context.user_data["awaiting_proof"] = False
        context.user_data["proof_app"] = None
        return

# --- Admin Proof Review Logic ---
async def proof_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # Security check: Only Admin can press these buttons
    if update.effective_user.id != ADMIN_ID:
        await query.answer("Only the owner can do this.", show_alert=True)
        return
        
    await query.answer()
    
    parts = query.data.split("_")
    action = parts[1]       # approve or reject
    user_id = int(parts[2]) # The user's telegram ID
    app_name = parts[3]     # The app name

    if action == "approve":
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Your proof for {app_name} has been APPROVED! Thank you for your effort.",
            reply_markup=MAIN_KEYBOARD
        )
        await query.edit_message_caption(caption=f"✅ Proof for {app_name} from user {user_id} has been APPROVED.")
        
    elif action == "reject":
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ Your proof for {app_name} has been REJECTED. Please contact @dtxzahid if you think this is a mistake.",
            reply_markup=MAIN_KEYBOARD
        )
        await query.edit_message_caption(caption=f"❌ Proof for {app_name} from user {user_id} has been REJECTED.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# --- Main Application Setup ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('DTX', admin_entry),
            MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry),
            MessageHandler(filters.Regex('^Get Comment$'), get_comment_entry),
        ],
        states={
            ADMIN_MENU: [MessageHandler(filters.Regex('^Add Comment$'), add_comment_entry)],
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
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=webhook_url,
    )
