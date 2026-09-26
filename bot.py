
import logging
import os
import base64
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from groq import Groq
from PIL import Image

# --- Read secrets from Railway environment variables ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
PORT = int(os.environ.get("PORT", 8080))

# --- Validate that everything is set ---
if not TELEGRAM_TOKEN:
    raise ValueError("Missing TELEGRAM_TOKEN environment variable.")
if not GROQ_API_KEY:
    raise ValueError("Missing GROQ_API_KEY environment variable.")
if not RAILWAY_PUBLIC_DOMAIN:
    raise ValueError("Missing RAILWAY_PUBLIC_DOMAIN environment variable.")

# --- Groq Client Initialization ---
client = Groq(api_key=GROQ_API_KEY)
VISION_MODEL = "qwen/qwen3.8-27b"
TEXT_MODEL = "openai/gpt-oss-120b"  # <-- Updated working model

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a text message or a photo. I can chat and read text from images."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Keep your answers brief and direct."},
                {"role": "user", "content": user_text}
            ],
            temperature=0.3,  # <-- Lowered for faster, more focused replies
        )
        await update.message.reply_text(completion.choices[0].message.content)
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
            "Extract all text from this image. "
            "If the text contains a question or request, respond to it. "
            "Otherwise, just return the extracted text."
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
            temperature=0.3,
            max_completion_tokens=1024,
        )
        await update.message.reply_text(completion.choices[0].message.content)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
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
