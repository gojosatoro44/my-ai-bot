import logging
import os
import base64
import json
import random
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, ConversationHandler
from groq import Groq
from PIL import Image

# --- Read secrets from Railway environment variables ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # <-- You must set this in Railway
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
PORT = int(os.environ.get("PORT", 8080))

# --- Validate that everything is set ---
if not TELEGRAM_TOKEN:
    raise ValueError("Missing TELEGRAM_TOKEN environment variable.")
if not GROQ_API_KEY:
    raise ValueError("Missing GROQ_API_KEY environment variable.")
if not RAILWAY_PUBLIC_DOMAIN:
    raise ValueError("Missing RAILWAY_PUBLIC_DOMAIN environment variable.")
if ADMIN_ID == 0:
    print("WARNING: ADMIN_ID is not set. Admin panel will not be accessible.")

# --- Groq Client Initialization ---
client = Groq(api_key=GROQ_API_KEY)
VISION_MODEL = "qwen/qwen3.8-27b"
TEXT_MODEL = "openai/gpt-oss-120b"

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
        "Hi! Send me a text message or a photo. I can chat and read text from images.",
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
    await update.message.reply_text("Please enter the App Name you want a comment for:")
    return USER_APP

async def user_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_name = update.message.text.strip()
    user_id = str(update.effective_user.id)
    data = load_data()
    
    if app_name not in data["apps"] or not data["apps"][app_name]:
        await update.message.reply_text("No comments found for this app.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    
    # Initialize history for this user if not exists
    if user_id not in data["history"]:
        data["history"][user_id] = {}
    if app_name not in data["history"][user_id]:
        data["history"][user_id][app_name] = []
    
    # Filter out already used comments
    all_comments = data["apps"][app_name]
    used_comments = data["history"][user_id][app_name]
    available_comments = [c for c in all_comments if c not in used_comments]
    
    if not available_comments:
        await update.message.reply_text("You have already received all available comments for this app.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    
    # Pick a random available comment
    chosen_comment = random.choice(available_comments)
    
    # Save to history
    data["history"][user_id][app_name].append(chosen_comment)
    save_data(data)
    
    # Send in mono text
    await update.message.reply_text(f"`{chosen_comment}`", parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# --- AI Handlers (Text & Photo) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Keep your answers brief and direct."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.3,
        )
        await update.message.reply_text(completion.choices[0].message.content, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file = await update.message.photo[-1].get_file()
    file_path = "temp_photo.jpg"
    await photo_file.download_to_drive(file_path)
    try:
        with open(file_path, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode("utf-8")

        prompt = (
            "Look at this screenshot carefully. Find the name of the person who wrote the review. "
            "Return ONLY the reviewer's name. Do not include stars, dates, or the review text. "
            "If you cannot find a reviewer name, reply exactly with: 'No reviewer name found'."
        )

        completion = client.chat.completions.create(
            model=VISION_MODEL,
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
            temperature=0.1,
            max_completion_tokens=1024,
        )
        await update.message.reply_text(completion.choices[0].message.content, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# --- Main Application Setup ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Admin & User Comment Conversation Handler
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
    
    # AI Handlers (only triggered if not in a conversation)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"

    print(f"Starting bot on port {PORT} with webhook {webhook_url}...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=webhook_url,
        )
