accepted"] = data["users"][uid].get("accepted", 0) + 1

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
