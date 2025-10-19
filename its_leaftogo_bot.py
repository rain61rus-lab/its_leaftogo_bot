ypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = [
        "Привет! Это бот заявок ИТС.",
        "Отправь текст заявки (можно с фото/видео).",
        "Команды:",
        "/my — мои заявки",
        "/new <текст> — создать заявку",
    ]
    if is_admin(uid):
        txt += ["Админ:", "/admin — панель", "/all — все новые заявки"]
    await update.message.reply_text("\n".join(txt))

async def cmd_new(update:Update, context:ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Напиши: /new описание проблемы")
        return
    descr = " ".join(args)
    tid = await add_ticket(update.effective_chat.id, update.effective_user.id, update.effective_user.username, descr, [])
    await update.message.reply_text(f"🆕 Заявка №{tid} создана.")

async def text_as_ticket(update:Update, context:ContextTypes.DEFAULT_TYPE):
    descr = update.message.text.strip()
    if descr.startswith("/"):
        return
    tid = await add_ticket(update.effective_chat.id, update.effective_user.id, update.effective_user.username, descr, [])
    await update.message.reply_text(f"🆕 Заявка №{tid} создана.")

async def my_tickets(update:Update, context:ContextTypes.DEFAULT_TYPE):
    rows = await list_tickets(user_id=update.effective_user.id, limit=20)
    if not rows:
        await update.message.reply_text("У тебя пока нет заявок.")
        return
    for r in rows:
        await update.message.reply_text(fmt_ticket(r))

async def admin_panel(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = await list_tickets(status="new", limit=20)
    if not rows:
        await update.message.reply_text("Новых заявок нет.")
        return
    for r in rows:
        kb = kb_ticket_admin(r[0], True, False)
        await update.message.reply_text(fmt_ticket(r), reply_markup=kb)

async def all_new(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = await list_tickets(status="new", limit=50)
    if not rows:
        await update.message.reply_text("Новых заявок нет.")
        return
    text = "\n\n".join(fmt_ticket(r) for r in rows)
    await update.message.reply_text(text or "—")

async def on_buttons(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    isadm = is_admin(uid)
    action, sid = data.split(":")
    tid = int(sid)
    row = await get_ticket(tid)
    if not row:
        await q.edit_message_text("Заявка не найдена.")
        return

    if action == "assign_me" and isadm:
        await update_ticket(tid, assignee_id=uid, assignee_name=q.from_user.username or str(uid))
    elif action == "to_work" and (isadm or (row[7] == uid)):
        await update_ticket(tid, status="in_work")
    elif action == "done" and (isadm or (row[7] == uid)):
        await update_ticket(tid, status="done")
    elif action == "cancel" and isadm:
        await update_ticket(tid, status="canceled")

    row = await get_ticket(tid)
    kb = kb_ticket_admin(tid, isadm, (row[7] == uid))
    await q.edit_message_text(fmt_ticket(row), reply_markup=kb)

async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("my", my_tickets))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("all", all_new))
    app.add_handler(CallbackQueryHandler(on_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_as_ticket))

    print("ITS bot running…")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main()):ContextT
