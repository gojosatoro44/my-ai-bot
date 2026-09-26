import logging
import os
import json
import random
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, ConversationHandler

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
            return json.load(f)
    return {"apps": {}, "history": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# --- Keyboards ---
MAIN_KEYBOARD = ReplyKeyboardMarkup([["Get Comment"]], resize_keyboard=True)
ADMIN_KEYBOARD = ReplyKeyboardMarkup([["Add Comment"]], resize_keyboard=True)

# --- Conversation States ---
ADMIN_MENU, ADD_APP, ADD_COMMENTS, USER_APP = range(4)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# --- Basic Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    # Create a keyboard with app names
    keyboard = [[app] for app in apps]
    await update.message.reply_text(
        "Please select an app:", 
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return USER_APP

async def user_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_name = update.message.text.strip()
    user_id = str(update.effective_user.id)
    data = load_data()
    
    if app_name not in data["apps"] or not data["apps"][app_name]:
        await update.message.reply_text("No comments found for this app.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    
    if user_id not in data["history"]:
        data["history"][user_id] = {}
    if app_name not in data["history"][user_id]:
        data["history"][user_id][app_name] = []
    
    all_comments = data["apps"][app_name]
    used_comments = data["history"][user_id][app_name]
    available_comments = [c for c in all_comments if c not in used_comments]
    
    if not available_comments:
        await update.message.reply_text("You have already received all available comments for this app.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    
    chosen_comment = random.choice(available_comments)
    data["history"][user_id][app_name].append(chosen_comment)
    save_data(data)
    
    await update.message.reply_text(f"`{chosen_comment}`", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

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
            USER_APP: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_app)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)

    # --- AI HANDLERS COMMENTED OUT ---
    # The AI code is still in the file, but not activated for users right now.
    # app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"

    print(f"Starting bot on port {PORT} with webhook {webhook_url}...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=webhook_url,
        )
